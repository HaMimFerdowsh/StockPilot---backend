from datetime import datetime
from decimal import Decimal
from typing import Optional
from typing import Annotated
from fastapi import Depends
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, func

from models import (
    SalesOrders, SalesOrderItems, Customers, Warehouses, Products, ProductVariants,
)
from router.auth import db_dependency, permission
from router.organizations import to_dict, paginate_rows
from router.audit_logs import log_action, snapshot
from router.notifications import notify
from router.subscriptions import check_limit
from router.inventory import validate_item, change_stock, default_cost, default_price, match_variant

router = APIRouter()

# draft -> confirmed -> completed      (draft / confirmed -> cancelled)
EDITABLE = ('draft',)


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class SOItemIn(BaseModel):
    product_id: int
    variant_id: Optional[int] = None
    quantity: int = Field(gt=0, le=1000000)
    unit_price: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)   # empty = selling price


class SOCreate(BaseModel):
    customer_id: Optional[int] = None                    # empty = walk-in customer
    warehouse_id: int
    tax: Decimal = Field(default=Decimal('0'), ge=0, max_digits=14, decimal_places=2)
    discount: Decimal = Field(default=Decimal('0'), ge=0, max_digits=14, decimal_places=2)
    note: Optional[str] = Field(default=None, max_length=500)
    items: list[SOItemIn] = Field(min_length=1, max_length=200)


class SOUpdate(BaseModel):
    customer_id: Optional[int] = None
    warehouse_id: Optional[int] = None
    tax: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    discount: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    note: Optional[str] = Field(default=None, max_length=500)


class SOItemsAdd(BaseModel):
    items: list[SOItemIn] = Field(min_length=1, max_length=200)


class SOItemUpdate(BaseModel):
    quantity: Optional[int] = Field(default=None, gt=0, le=1000000)
    unit_price: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def get_so(db, member, so_id: int):
    so = db.query(SalesOrders).filter(
        SalesOrders.id == so_id, SalesOrders.organization_id == member['organization_id']
    ).first()
    if so is None:
        raise HTTPException(status_code=404, detail='Sales Order Not Found')
    return so


def require_editable(so):
    if so.status not in EDITABLE:
        raise HTTPException(status_code=400, detail=f'Only draft sales orders can be changed (this one is {so.status})')


def next_number(db, org_id: int):
    n = db.query(SalesOrders).filter(SalesOrders.organization_id == org_id).count() + 1
    year = datetime.now().year
    while True:
        number = f'SO-{year}-{n:05d}'
        if not db.query(SalesOrders.id).filter(
            SalesOrders.organization_id == org_id, SalesOrders.so_number == number
        ).first():
            return number
        n += 1


def check_customer_warehouse(db, member, customer_id, warehouse_id):
    org_id = member['organization_id']
    if customer_id is not None:
        customer = db.query(Customers).filter(Customers.id == customer_id, Customers.organization_id == org_id).first()
        if customer is None:
            raise HTTPException(status_code=404, detail='Customer Not Found')
        if not customer.is_active:
            raise HTTPException(status_code=400, detail='Customer is inactive')
    if warehouse_id is not None:
        warehouse = db.query(Warehouses).filter(Warehouses.id == warehouse_id, Warehouses.organization_id == org_id).first()
        if warehouse is None:
            raise HTTPException(status_code=404, detail='Warehouse Not Found')
        if not warehouse.is_active:
            raise HTTPException(status_code=400, detail='Warehouse is inactive')


def add_line(db, member, so, item: SOItemIn):
    product, variant = validate_item(db, member['organization_id'], item.product_id, item.variant_id)
    price = item.unit_price if item.unit_price is not None else default_price(product, variant)

    existing = db.query(SalesOrderItems).filter(
        SalesOrderItems.sales_order_id == so.id,
        SalesOrderItems.product_id == product.id,
        match_variant(SalesOrderItems.variant_id, variant.id if variant else None),
    ).first()

    if existing:      # same product twice -> merge into one line
        existing.quantity += item.quantity
        existing.unit_price = price
        existing.line_total = price * existing.quantity
    else:
        db.add(SalesOrderItems(
            sales_order_id=so.id, product_id=product.id, variant_id=variant.id if variant else None,
            quantity=item.quantity, unit_price=price, unit_cost=default_cost(product, variant),
            line_total=price * item.quantity,
        ))
    db.flush()


def recalc(db, so):
    db.flush()
    subtotal = db.query(func.coalesce(func.sum(SalesOrderItems.line_total), 0)).filter(
        SalesOrderItems.sales_order_id == so.id
    ).scalar()
    so.subtotal = subtotal
    so.total = max(subtotal + (so.tax or 0) - (so.discount or 0), Decimal('0'))


def items_of(db, so_id: int):
    rows = db.query(SalesOrderItems, Products, ProductVariants).join(
        Products, Products.id == SalesOrderItems.product_id
    ).outerjoin(
        ProductVariants, ProductVariants.id == SalesOrderItems.variant_id
    ).filter(SalesOrderItems.sales_order_id == so_id).order_by(SalesOrderItems.id).all()

    return [
        {
            **to_dict(item),
            'product_name': product.name,
            'variant_name': variant.name if variant else None,
            'sku': variant.sku if variant else product.sku,
        }
        for item, product, variant in rows
    ]


def jsonable(data: dict):
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in data.items()}


# ---------------------------------------------------------------
# Create / list / details / update
# ---------------------------------------------------------------
@router.post('/sales-orders')
def create_sales_order(member: Annotated[dict, Depends(permission('sales:create'))], db: db_dependency, new_so: SOCreate):

    org_id = member['organization_id']
    check_limit(db, org_id, 'orders')
    check_customer_warehouse(db, member, new_so.customer_id, new_so.warehouse_id)

    so = SalesOrders(
        organization_id=org_id,
        so_number=next_number(db, org_id),
        customer_id=new_so.customer_id,
        warehouse_id=new_so.warehouse_id,
        tax=new_so.tax,
        discount=new_so.discount,
        note=new_so.note,
        status='draft',
        created_by=member['id'],
    )
    db.add(so)
    db.flush()

    for item in new_so.items:
        add_line(db, member, so, item)
    recalc(db, so)

    notify(db, org_id, 'new_sale', f'New sales order {so.so_number}',
           f'Sales order {so.so_number} was created. Total: {so.total}.',
           reference_type='sales_order', reference_id=so.id)
    log_action(db, member, 'create', 'sales_order', so.id, new=snapshot(so))
    db.commit()

    return JSONResponse(status_code=201, content={
        'message': 'Sales order created successfully', 'sales_order_id': so.id, 'so_number': so.so_number,
    })


@router.get('/sales-orders')
def get_all_sales_orders(
    member: Annotated[dict, Depends(permission('sales:read'))],
    db: db_dependency,
    search: Optional[str] = None,                        # SO number / customer name / ID
    status: Optional[str] = Query(None, pattern='^(draft|confirmed|completed|cancelled)$'),
    customer_id: Optional[int] = None,
    warehouse_id: Optional[int] = None,
    from_date: Optional[datetime] = None,                # order_date range
    to_date: Optional[datetime] = None,
    sort_by: str = Query('order_date', pattern='^(order_date|total|status|so_number)$'),
    order: str = Query('desc', pattern='^(asc|desc)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    S = SalesOrders
    query = db.query(S, Customers.name).outerjoin(Customers, Customers.id == S.customer_id).filter(
        S.organization_id == member['organization_id']
    )

    if search:
        like = f'%{search}%'
        cond = [S.so_number.ilike(like), Customers.name.ilike(like)]
        if search.isdigit():
            cond.append(S.id == int(search))
        query = query.filter(or_(*cond))
    if status:
        query = query.filter(S.status == status)
    if customer_id:
        query = query.filter(S.customer_id == customer_id)
    if warehouse_id:
        query = query.filter(S.warehouse_id == warehouse_id)
    if from_date:
        query = query.filter(S.order_date >= from_date)
    if to_date:
        query = query.filter(S.order_date <= to_date)

    column = getattr(S, sort_by)
    query = query.order_by(column.asc() if order == 'asc' else column.desc(), S.id.desc())

    return paginate_rows(query, page, page_size, lambda r: {**to_dict(r[0]), 'customer_name': r[1]})


@router.get('/sales-orders/{so_id}')
def get_sales_order_details(member: Annotated[dict, Depends(permission('sales:read'))], db: db_dependency, so_id: int):

    so = get_so(db, member, so_id)
    customer = db.query(Customers).filter(Customers.id == so.customer_id).first() if so.customer_id else None
    warehouse = db.query(Warehouses).filter(Warehouses.id == so.warehouse_id).first()

    return {
        **to_dict(so),
        'customer_name': customer.name if customer else None,
        'warehouse_name': warehouse.name if warehouse else None,
        'items': items_of(db, so.id),
    }


@router.put('/sales-orders/{so_id}')
def update_sales_order(member: Annotated[dict, Depends(permission('sales:update'))], db: db_dependency,
                       update_so: SOUpdate, so_id: int):

    so = get_so(db, member, so_id)
    require_editable(so)
    old = snapshot(so)

    update_data = update_so.model_dump(exclude_unset=True)
    for field in ('warehouse_id', 'tax', 'discount'):
        if field in update_data and update_data[field] is None:
            raise HTTPException(status_code=422, detail=f'{field} cannot be null')

    check_customer_warehouse(db, member, update_data.get('customer_id'), update_data.get('warehouse_id'))

    for key, value in update_data.items():
        setattr(so, key, value)
    recalc(db, so)

    log_action(db, member, 'update', 'sales_order', so.id, old=old, new=snapshot(so))
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Sales order updated successfully'})


# ---------------------------------------------------------------
# Items (draft only)
# ---------------------------------------------------------------
@router.post('/sales-orders/{so_id}/items')
def add_sales_order_items(member: Annotated[dict, Depends(permission('sales:update'))], db: db_dependency,
                          body: SOItemsAdd, so_id: int):

    so = get_so(db, member, so_id)
    require_editable(so)

    for item in body.items:
        add_line(db, member, so, item)
    recalc(db, so)

    log_action(db, member, 'add_items', 'sales_order', so.id, new={'items': [jsonable(i.model_dump()) for i in body.items]})
    db.commit()
    return JSONResponse(status_code=201, content={'message': 'Items added successfully', 'total': float(so.total)})


@router.put('/sales-orders/{so_id}/items/{item_id}')
def update_sales_order_item(member: Annotated[dict, Depends(permission('sales:update'))], db: db_dependency,
                            body: SOItemUpdate, so_id: int, item_id: int):

    so = get_so(db, member, so_id)
    require_editable(so)

    item = db.query(SalesOrderItems).filter(
        SalesOrderItems.id == item_id, SalesOrderItems.sales_order_id == so.id
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail='Item Not Found')

    data = body.model_dump(exclude_unset=True)
    if data.get('quantity') is not None:
        item.quantity = data['quantity']
    if data.get('unit_price') is not None:
        item.unit_price = data['unit_price']
    item.line_total = item.unit_price * item.quantity
    recalc(db, so)

    log_action(db, member, 'update_item', 'sales_order', so.id, new={'item_id': item.id, **jsonable(data)})
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Item updated successfully', 'total': float(so.total)})


@router.delete('/sales-orders/{so_id}/items/{item_id}')
def delete_sales_order_item(member: Annotated[dict, Depends(permission('sales:update'))], db: db_dependency,
                            so_id: int, item_id: int):

    so = get_so(db, member, so_id)
    require_editable(so)

    item = db.query(SalesOrderItems).filter(
        SalesOrderItems.id == item_id, SalesOrderItems.sales_order_id == so.id
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail='Item Not Found')

    count = db.query(SalesOrderItems).filter(SalesOrderItems.sales_order_id == so.id).count()
    if count <= 1:
        raise HTTPException(status_code=400, detail='A sales order needs at least one item. Cancel it instead')

    db.delete(item)
    recalc(db, so)

    log_action(db, member, 'delete_item', 'sales_order', so.id, old={'item_id': item_id})
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Item removed successfully', 'total': float(so.total)})


# ---------------------------------------------------------------
# Status: confirm / complete / cancel
# ---------------------------------------------------------------
def _status_notice(db, member, so, text: str):
    notify(db, member['organization_id'], 'order_status', f'Sales order {so.so_number} {text}',
           f'Sales order {so.so_number} is now {so.status}.',
           reference_type='sales_order', reference_id=so.id)


@router.post('/sales-orders/{so_id}/confirm')
def confirm_sales_order(member: Annotated[dict, Depends(permission('sales:update'))], db: db_dependency, so_id: int):

    so = get_so(db, member, so_id)
    if so.status != 'draft':
        raise HTTPException(status_code=400, detail='Only draft sales orders can be confirmed')

    so.status = 'confirmed'
    _status_notice(db, member, so, 'was confirmed')
    log_action(db, member, 'status_change', 'sales_order', so.id, old={'status': 'draft'}, new={'status': 'confirmed'})
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Sales order confirmed'})


@router.post('/sales-orders/{so_id}/complete')
def complete_sales_order(member: Annotated[dict, Depends(permission('sales:complete'))], db: db_dependency, so_id: int):

    so = get_so(db, member, so_id)
    if so.status not in ('draft', 'confirmed'):
        raise HTTPException(status_code=400, detail=f'This sales order is already {so.status}')

    items = db.query(SalesOrderItems).filter(SalesOrderItems.sales_order_id == so.id).all()
    if not items:
        raise HTTPException(status_code=400, detail='Sales order has no items')

    old_status = so.status
    # one commit at the end: if any item is short of stock, nothing is sold
    for item in items:
        product, variant = validate_item(db, member['organization_id'], item.product_id, item.variant_id)
        cost = default_cost(product, variant)
        item.unit_cost = cost                     # cost snapshot for profit & loss
        change_stock(db, member, so.warehouse_id, product, variant, -item.quantity, 'out',
                     unit_cost=cost, reference_type='sales_order', reference_id=so.id,
                     note=f'Sold {so.so_number}')

    so.status = 'completed'
    so.completed_date = datetime.now()
    _status_notice(db, member, so, 'was completed')
    log_action(db, member, 'complete', 'sales_order', so.id, old={'status': old_status}, new={'status': 'completed'})
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Sales order completed and stock decreased'})


@router.post('/sales-orders/{so_id}/cancel')
def cancel_sales_order(member: Annotated[dict, Depends(permission('sales:cancel'))], db: db_dependency, so_id: int):

    so = get_so(db, member, so_id)
    if so.status not in ('draft', 'confirmed'):
        raise HTTPException(status_code=400, detail=f'A {so.status} sales order cannot be cancelled')

    old_status = so.status
    so.status = 'cancelled'
    _status_notice(db, member, so, 'was cancelled')
    log_action(db, member, 'cancel', 'sales_order', so.id, old={'status': old_status}, new={'status': 'cancelled'})
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Sales order cancelled successfully'})