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
    PurchaseOrders, PurchaseOrderItems, Suppliers, Warehouses, Products, ProductVariants,
)
from router.auth import db_dependency, permission
from router.organizations import to_dict, paginate_rows
from router.audit_logs import log_action, snapshot
from router.notifications import notify
from router.subscriptions import check_limit
from router.inventory import validate_item, change_stock, default_cost, match_variant

router = APIRouter()

# draft -> ordered -> received      (draft / ordered -> cancelled)
EDITABLE = ('draft',)


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class POItemIn(BaseModel):
    product_id: int
    variant_id: Optional[int] = None
    quantity: int = Field(gt=0, le=1000000)
    unit_cost: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)   # empty = product cost


class POCreate(BaseModel):
    supplier_id: int
    warehouse_id: int
    expected_date: Optional[datetime] = None
    tax: Decimal = Field(default=Decimal('0'), ge=0, max_digits=14, decimal_places=2)
    note: Optional[str] = Field(default=None, max_length=500)
    items: list[POItemIn] = Field(min_length=1, max_length=200)


class POUpdate(BaseModel):
    supplier_id: Optional[int] = None
    warehouse_id: Optional[int] = None
    expected_date: Optional[datetime] = None
    tax: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    note: Optional[str] = Field(default=None, max_length=500)


class POItemsAdd(BaseModel):
    items: list[POItemIn] = Field(min_length=1, max_length=200)


class POItemUpdate(BaseModel):
    quantity: Optional[int] = Field(default=None, gt=0, le=1000000)
    unit_cost: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def get_po(db, member, po_id: int):
    po = db.query(PurchaseOrders).filter(
        PurchaseOrders.id == po_id, PurchaseOrders.organization_id == member['organization_id']
    ).first()
    if po is None:
        raise HTTPException(status_code=404, detail='Purchase Order Not Found')
    return po


def require_editable(po):
    if po.status not in EDITABLE:
        raise HTTPException(status_code=400, detail=f'Only draft purchase orders can be changed (this one is {po.status})')


def next_number(db, org_id: int):
    n = db.query(PurchaseOrders).filter(PurchaseOrders.organization_id == org_id).count() + 1
    year = datetime.now().year
    while True:
        number = f'PO-{year}-{n:05d}'
        if not db.query(PurchaseOrders.id).filter(
            PurchaseOrders.organization_id == org_id, PurchaseOrders.po_number == number
        ).first():
            return number
        n += 1


def check_supplier_warehouse(db, member, supplier_id, warehouse_id):
    org_id = member['organization_id']
    if supplier_id is not None:
        supplier = db.query(Suppliers).filter(Suppliers.id == supplier_id, Suppliers.organization_id == org_id).first()
        if supplier is None:
            raise HTTPException(status_code=404, detail='Supplier Not Found')
        if not supplier.is_active:
            raise HTTPException(status_code=400, detail='Supplier is inactive')
    if warehouse_id is not None:
        warehouse = db.query(Warehouses).filter(Warehouses.id == warehouse_id, Warehouses.organization_id == org_id).first()
        if warehouse is None:
            raise HTTPException(status_code=404, detail='Warehouse Not Found')
        if not warehouse.is_active:
            raise HTTPException(status_code=400, detail='Warehouse is inactive')


def add_line(db, member, po, item: POItemIn):
    product, variant = validate_item(db, member['organization_id'], item.product_id, item.variant_id)
    cost = item.unit_cost if item.unit_cost is not None else default_cost(product, variant)

    existing = db.query(PurchaseOrderItems).filter(
        PurchaseOrderItems.purchase_order_id == po.id,
        PurchaseOrderItems.product_id == product.id,
        match_variant(PurchaseOrderItems.variant_id, variant.id if variant else None),
    ).first()

    if existing:      # same product twice -> merge into one line
        existing.quantity += item.quantity
        existing.unit_cost = cost
        existing.line_total = cost * existing.quantity
    else:
        db.add(PurchaseOrderItems(
            purchase_order_id=po.id, product_id=product.id, variant_id=variant.id if variant else None,
            quantity=item.quantity, unit_cost=cost, line_total=cost * item.quantity,
        ))
    db.flush()


def recalc(db, po):
    db.flush()
    subtotal = db.query(func.coalesce(func.sum(PurchaseOrderItems.line_total), 0)).filter(
        PurchaseOrderItems.purchase_order_id == po.id
    ).scalar()
    po.subtotal = subtotal
    po.total = subtotal + (po.tax or 0)


def items_of(db, po_id: int):
    rows = db.query(PurchaseOrderItems, Products, ProductVariants).join(
        Products, Products.id == PurchaseOrderItems.product_id
    ).outerjoin(
        ProductVariants, ProductVariants.id == PurchaseOrderItems.variant_id
    ).filter(PurchaseOrderItems.purchase_order_id == po_id).order_by(PurchaseOrderItems.id).all()

    return [
        {
            **to_dict(item),
            'product_name': product.name,
            'variant_name': variant.name if variant else None,
            'sku': variant.sku if variant else product.sku,
        }
        for item, product, variant in rows
    ]


# ---------------------------------------------------------------
# Create / list / details / update
# ---------------------------------------------------------------
@router.post('/purchase-orders')
def create_purchase_order(member: Annotated[dict, Depends(permission('purchase:create'))], db: db_dependency, new_po: POCreate):

    org_id = member['organization_id']
    check_limit(db, org_id, 'orders')
    check_supplier_warehouse(db, member, new_po.supplier_id, new_po.warehouse_id)

    po = PurchaseOrders(
        organization_id=org_id,
        po_number=next_number(db, org_id),
        supplier_id=new_po.supplier_id,
        warehouse_id=new_po.warehouse_id,
        expected_date=new_po.expected_date,
        tax=new_po.tax,
        note=new_po.note,
        status='draft',
        created_by=member['id'],
    )
    db.add(po)
    db.flush()

    for item in new_po.items:
        add_line(db, member, po, item)
    recalc(db, po)

    notify(db, org_id, 'new_purchase', f'New purchase order {po.po_number}',
           f'Purchase order {po.po_number} was created. Total: {po.total}.',
           reference_type='purchase_order', reference_id=po.id)
    log_action(db, member, 'create', 'purchase_order', po.id, new=snapshot(po))
    db.commit()

    return JSONResponse(status_code=201, content={
        'message': 'Purchase order created successfully', 'purchase_order_id': po.id, 'po_number': po.po_number,
    })


@router.get('/purchase-orders')
def get_all_purchase_orders(
    member: Annotated[dict, Depends(permission('purchase:read'))],
    db: db_dependency,
    search: Optional[str] = None,                        # PO number / supplier name / ID
    status: Optional[str] = Query(None, pattern='^(draft|ordered|received|cancelled)$'),
    supplier_id: Optional[int] = None,
    warehouse_id: Optional[int] = None,
    from_date: Optional[datetime] = None,                # order_date range
    to_date: Optional[datetime] = None,
    sort_by: str = Query('order_date', pattern='^(order_date|total|status|po_number)$'),
    order: str = Query('desc', pattern='^(asc|desc)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    P = PurchaseOrders
    query = db.query(P, Suppliers.name).join(Suppliers, Suppliers.id == P.supplier_id).filter(
        P.organization_id == member['organization_id']
    )

    if search:
        like = f'%{search}%'
        cond = [P.po_number.ilike(like), Suppliers.name.ilike(like)]
        if search.isdigit():
            cond.append(P.id == int(search))
        query = query.filter(or_(*cond))
    if status:
        query = query.filter(P.status == status)
    if supplier_id:
        query = query.filter(P.supplier_id == supplier_id)
    if warehouse_id:
        query = query.filter(P.warehouse_id == warehouse_id)
    if from_date:
        query = query.filter(P.order_date >= from_date)
    if to_date:
        query = query.filter(P.order_date <= to_date)

    column = getattr(P, sort_by)
    query = query.order_by(column.asc() if order == 'asc' else column.desc(), P.id.desc())

    return paginate_rows(query, page, page_size, lambda r: {**to_dict(r[0]), 'supplier_name': r[1]})


@router.get('/purchase-orders/{po_id}')
def get_purchase_order_details(member: Annotated[dict, Depends(permission('purchase:read'))], db: db_dependency, po_id: int):

    po = get_po(db, member, po_id)
    supplier = db.query(Suppliers).filter(Suppliers.id == po.supplier_id).first()
    warehouse = db.query(Warehouses).filter(Warehouses.id == po.warehouse_id).first()

    return {
        **to_dict(po),
        'supplier_name': supplier.name if supplier else None,
        'warehouse_name': warehouse.name if warehouse else None,
        'items': items_of(db, po.id),
    }


@router.put('/purchase-orders/{po_id}')
def update_purchase_order(member: Annotated[dict, Depends(permission('purchase:update'))], db: db_dependency,
                          update_po: POUpdate, po_id: int):

    po = get_po(db, member, po_id)
    require_editable(po)
    old = snapshot(po)

    update_data = update_po.model_dump(exclude_unset=True)
    for field in ('supplier_id', 'warehouse_id', 'tax'):
        if field in update_data and update_data[field] is None:
            raise HTTPException(status_code=422, detail=f'{field} cannot be null')

    check_supplier_warehouse(db, member, update_data.get('supplier_id'), update_data.get('warehouse_id'))

    for key, value in update_data.items():
        setattr(po, key, value)
    recalc(db, po)

    log_action(db, member, 'update', 'purchase_order', po.id, old=old, new=snapshot(po))
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Purchase order updated successfully'})


# ---------------------------------------------------------------
# Items (draft only)
# ---------------------------------------------------------------
@router.post('/purchase-orders/{po_id}/items')
def add_purchase_order_items(member: Annotated[dict, Depends(permission('purchase:update'))], db: db_dependency,
                             body: POItemsAdd, po_id: int):

    po = get_po(db, member, po_id)
    require_editable(po)

    for item in body.items:
        add_line(db, member, po, item)
    recalc(db, po)

    log_action(db, member, 'add_items', 'purchase_order', po.id, new={'items': [i.model_dump() for i in body.items]})
    db.commit()
    return JSONResponse(status_code=201, content={'message': 'Items added successfully', 'total': float(po.total)})


@router.put('/purchase-orders/{po_id}/items/{item_id}')
def update_purchase_order_item(member: Annotated[dict, Depends(permission('purchase:update'))], db: db_dependency,
                               body: POItemUpdate, po_id: int, item_id: int):

    po = get_po(db, member, po_id)
    require_editable(po)

    item = db.query(PurchaseOrderItems).filter(
        PurchaseOrderItems.id == item_id, PurchaseOrderItems.purchase_order_id == po.id
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail='Item Not Found')

    data = body.model_dump(exclude_unset=True)
    if data.get('quantity') is not None:
        item.quantity = data['quantity']
    if data.get('unit_cost') is not None:
        item.unit_cost = data['unit_cost']
    item.line_total = item.unit_cost * item.quantity
    recalc(db, po)

    log_action(db, member, 'update_item', 'purchase_order', po.id, new={'item_id': item.id, **jsonable(data)})
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Item updated successfully', 'total': float(po.total)})


@router.delete('/purchase-orders/{po_id}/items/{item_id}')
def delete_purchase_order_item(member: Annotated[dict, Depends(permission('purchase:update'))], db: db_dependency,
                               po_id: int, item_id: int):

    po = get_po(db, member, po_id)
    require_editable(po)

    item = db.query(PurchaseOrderItems).filter(
        PurchaseOrderItems.id == item_id, PurchaseOrderItems.purchase_order_id == po.id
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail='Item Not Found')

    count = db.query(PurchaseOrderItems).filter(PurchaseOrderItems.purchase_order_id == po.id).count()
    if count <= 1:
        raise HTTPException(status_code=400, detail='A purchase order needs at least one item. Cancel it instead')

    db.delete(item)
    recalc(db, po)

    log_action(db, member, 'delete_item', 'purchase_order', po.id, old={'item_id': item_id})
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Item removed successfully', 'total': float(po.total)})


def jsonable(data: dict):
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in data.items()}


# ---------------------------------------------------------------
# Status: order / receive / cancel
# ---------------------------------------------------------------
def _status_notice(db, member, po, text: str):
    notify(db, member['organization_id'], 'order_status', f'Purchase order {po.po_number} {text}',
           f'Purchase order {po.po_number} is now {po.status}.',
           reference_type='purchase_order', reference_id=po.id)


@router.post('/purchase-orders/{po_id}/order')
def mark_as_ordered(member: Annotated[dict, Depends(permission('purchase:update'))], db: db_dependency, po_id: int):

    po = get_po(db, member, po_id)
    if po.status != 'draft':
        raise HTTPException(status_code=400, detail='Only draft purchase orders can be marked as ordered')

    po.status = 'ordered'
    _status_notice(db, member, po, 'was ordered')
    log_action(db, member, 'status_change', 'purchase_order', po.id, old={'status': 'draft'}, new={'status': 'ordered'})
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Purchase order marked as ordered'})


@router.post('/purchase-orders/{po_id}/receive')
def receive_purchase_order(member: Annotated[dict, Depends(permission('purchase:receive'))], db: db_dependency, po_id: int):

    po = get_po(db, member, po_id)
    if po.status not in ('draft', 'ordered'):
        raise HTTPException(status_code=400, detail=f'This purchase order is already {po.status}')

    items = db.query(PurchaseOrderItems).filter(PurchaseOrderItems.purchase_order_id == po.id).all()
    if not items:
        raise HTTPException(status_code=400, detail='Purchase order has no items')

    old_status = po.status
    # one commit at the end: if any item fails, nothing is received
    for item in items:
        product, variant = validate_item(db, member['organization_id'], item.product_id, item.variant_id)
        change_stock(db, member, po.warehouse_id, product, variant, item.quantity, 'in',
                     unit_cost=item.unit_cost, reference_type='purchase_order', reference_id=po.id,
                     note=f'Received {po.po_number}')

    po.status = 'received'
    po.received_date = datetime.now()
    _status_notice(db, member, po, 'was received')
    log_action(db, member, 'receive', 'purchase_order', po.id, old={'status': old_status}, new={'status': 'received'})
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Purchase order received and stock increased'})


@router.post('/purchase-orders/{po_id}/cancel')
def cancel_purchase_order(member: Annotated[dict, Depends(permission('purchase:cancel'))], db: db_dependency, po_id: int):

    po = get_po(db, member, po_id)
    if po.status not in ('draft', 'ordered'):
        raise HTTPException(status_code=400, detail=f'A {po.status} purchase order cannot be cancelled')

    old_status = po.status
    po.status = 'cancelled'
    _status_notice(db, member, po, 'was cancelled')
    log_action(db, member, 'cancel', 'purchase_order', po.id, old={'status': old_status}, new={'status': 'cancelled'})
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Purchase order cancelled successfully'})