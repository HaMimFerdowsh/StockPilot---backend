from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, func
from typing import Annotated
from fastapi import Depends
from models import Inventory, StockMovements, Products, ProductVariants, Warehouses
from router.auth import db_dependency, permission
from router.organizations import paginate_rows
from router.audit_logs import log_action
from router.notifications import notify

router = APIRouter()


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class StockChange(BaseModel):
    warehouse_id: int
    product_id: int
    variant_id: Optional[int] = None
    quantity: int = Field(gt=0, le=1000000)
    unit_cost: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    note: Optional[str] = Field(default=None, max_length=200)


class StockAdjust(BaseModel):
    warehouse_id: int
    product_id: int
    variant_id: Optional[int] = None
    new_quantity: int = Field(ge=0, le=1000000)
    note: str = Field(min_length=3, max_length=200)          # reason is required


class StockTransfer(BaseModel):
    from_warehouse_id: int
    to_warehouse_id: int
    product_id: int
    variant_id: Optional[int] = None
    quantity: int = Field(gt=0, le=1000000)
    note: Optional[str] = Field(default=None, max_length=200)


# ---------------------------------------------------------------
# Helpers (purchase / sales / csv routers use these)
# ---------------------------------------------------------------
def default_cost(product, variant=None):
    if variant is not None and variant.cost_price is not None:
        return variant.cost_price
    return product.cost_price or Decimal('0')


def default_price(product, variant=None):
    if variant is not None and variant.selling_price is not None:
        return variant.selling_price
    return product.selling_price or Decimal('0')


def match_variant(column, variant_id):
    return column == variant_id if variant_id else column.is_(None)


def get_warehouse(db, org_id: int, warehouse_id: int):
    warehouse = db.query(Warehouses).filter(
        Warehouses.id == warehouse_id, Warehouses.organization_id == org_id
    ).first()
    if warehouse is None:
        raise HTTPException(status_code=404, detail='Warehouse Not Found')
    if not warehouse.is_active:
        raise HTTPException(status_code=400, detail='Warehouse is inactive')
    return warehouse


def validate_item(db, org_id: int, product_id: int, variant_id: Optional[int] = None):
    """Checks that the product (and variant) belong to this organization. Returns (product, variant)."""
    product = db.query(Products).filter(
        Products.id == product_id, Products.organization_id == org_id
    ).first()
    if product is None:
        raise HTTPException(status_code=404, detail='Product Not Found')
    if not product.is_active:
        raise HTTPException(status_code=400, detail=f'Product "{product.name}" is inactive')

    variant = None
    if variant_id:
        variant = db.query(ProductVariants).filter(
            ProductVariants.id == variant_id,
            ProductVariants.product_id == product.id,
            ProductVariants.organization_id == org_id,
        ).first()
        if variant is None:
            raise HTTPException(status_code=404, detail='Variant Not Found')
        if not variant.is_active:
            raise HTTPException(status_code=400, detail=f'Variant "{variant.name}" is inactive')
    elif product.has_variants:
        raise HTTPException(status_code=400, detail=f'Product "{product.name}" has variants. variant_id is required')

    return product, variant


def get_stock_row(db, org_id, warehouse_id, product_id, variant_id, lock=False):
    query = db.query(Inventory).filter(
        Inventory.organization_id == org_id,
        Inventory.warehouse_id == warehouse_id,
        Inventory.product_id == product_id,
        match_variant(Inventory.variant_id, variant_id),
    )
    if lock:
        query = query.with_for_update()      # two people changing the same stock wait for each other
    return query.first()


def total_stock(db, org_id: int, product_id: int):
    value = db.query(func.coalesce(func.sum(Inventory.quantity), 0)).filter(
        Inventory.organization_id == org_id, Inventory.product_id == product_id
    ).scalar()
    return int(value or 0)


def change_stock(db, member, warehouse_id: int, product, variant, delta: int, movement_type: str,
                 unit_cost=None, reference_type=None, reference_id=None, note=None):
    """
    THE one place where stock changes. Updates the stock row, writes a stock movement,
    and raises a low stock notification. Does not commit (caller commits).
    delta > 0 adds stock, delta < 0 removes stock.
    """
    org_id = member['organization_id']
    variant_id = variant.id if variant else None

    row = get_stock_row(db, org_id, warehouse_id, product.id, variant_id, lock=True)
    if row is None:
        row = Inventory(organization_id=org_id, warehouse_id=warehouse_id,
                        product_id=product.id, variant_id=variant_id, quantity=0)
        db.add(row)

    before_total = total_stock(db, org_id, product.id)
    new_qty = (row.quantity or 0) + delta
    if new_qty < 0:
        raise HTTPException(
            status_code=400,
            detail=f'Insufficient stock for "{product.name}": available {row.quantity or 0}, requested {-delta}',
        )
    row.quantity = new_qty

    movement = StockMovements(
        organization_id=org_id,
        warehouse_id=warehouse_id,
        product_id=product.id,
        variant_id=variant_id,
        movement_type=movement_type,
        quantity=delta,
        quantity_after=new_qty,
        unit_cost=unit_cost,
        reference_type=reference_type,
        reference_id=reference_id,
        note=note,
        created_by=member['id'],
    )
    db.add(movement)
    db.flush()

    # low stock alert: only when the TOTAL stock just crossed the reorder level (no spam)
    after_total = before_total + delta
    if before_total > product.reorder_level >= after_total:
        notify(
            db, org_id, 'low_stock', f'Low stock: {product.name}',
            f'"{product.name}" (SKU {product.sku}) is low on stock: {after_total} left, '
            f'reorder level is {product.reorder_level}.',
            reference_type='product', reference_id=product.id,
        )

    return movement


def low_stock_query(db, org_id: int):
    sq = db.query(
        Inventory.product_id.label('product_id'),
        func.sum(Inventory.quantity).label('stock'),
    ).filter(Inventory.organization_id == org_id).group_by(Inventory.product_id).subquery()

    stock = func.coalesce(sq.c.stock, 0)
    return db.query(Products, stock.label('total_stock')).outerjoin(
        sq, sq.c.product_id == Products.id
    ).filter(
        Products.organization_id == org_id,
        Products.is_active == True,
        stock <= Products.reorder_level,
    )


def valuation_summary(db, org_id: int, warehouse_id: Optional[int] = None):
    cost = func.coalesce(ProductVariants.cost_price, Products.cost_price)

    query = db.query(
        Warehouses.id,
        Warehouses.name,
        func.coalesce(func.sum(Inventory.quantity), 0),
        func.coalesce(func.sum(Inventory.quantity * cost), 0),
    ).select_from(Inventory).join(
        Warehouses, Warehouses.id == Inventory.warehouse_id
    ).join(
        Products, Products.id == Inventory.product_id
    ).outerjoin(
        ProductVariants, ProductVariants.id == Inventory.variant_id
    ).filter(Inventory.organization_id == org_id)

    if warehouse_id:
        query = query.filter(Inventory.warehouse_id == warehouse_id)

    rows = query.group_by(Warehouses.id, Warehouses.name).order_by(Warehouses.name).all()

    return {
        'total_quantity': int(sum(r[2] for r in rows)),
        'total_value': sum((r[3] for r in rows), Decimal('0')),
        'by_warehouse': [
            {'warehouse_id': r[0], 'warehouse_name': r[1], 'quantity': int(r[2]), 'value': r[3]}
            for r in rows
        ],
    }


# ---------------------------------------------------------------
# Stock in / out / adjustment / transfer
# ---------------------------------------------------------------
@router.post('/inventory/stock-in')
def stock_in(member: Annotated[dict, Depends(permission('inventory:stock_in'))], db: db_dependency, body: StockChange):

    org_id = member['organization_id']
    get_warehouse(db, org_id, body.warehouse_id)
    product, variant = validate_item(db, org_id, body.product_id, body.variant_id)

    cost = body.unit_cost if body.unit_cost is not None else default_cost(product, variant)
    movement = change_stock(db, member, body.warehouse_id, product, variant, body.quantity, 'in',
                            unit_cost=cost, reference_type='manual', note=body.note)

    log_action(db, member, 'stock_in', 'stock', product.id, new={
        'warehouse_id': body.warehouse_id, 'variant_id': body.variant_id,
        'quantity': body.quantity, 'quantity_after': movement.quantity_after,
    })
    db.commit()

    return JSONResponse(status_code=201, content={
        'message': 'Stock added successfully', 'quantity_after': movement.quantity_after,
    })


@router.post('/inventory/stock-out')
def stock_out(member: Annotated[dict, Depends(permission('inventory:stock_out'))], db: db_dependency, body: StockChange):

    org_id = member['organization_id']
    get_warehouse(db, org_id, body.warehouse_id)
    product, variant = validate_item(db, org_id, body.product_id, body.variant_id)

    movement = change_stock(db, member, body.warehouse_id, product, variant, -body.quantity, 'out',
                            unit_cost=default_cost(product, variant), reference_type='manual', note=body.note)

    log_action(db, member, 'stock_out', 'stock', product.id, new={
        'warehouse_id': body.warehouse_id, 'variant_id': body.variant_id,
        'quantity': -body.quantity, 'quantity_after': movement.quantity_after,
    })
    db.commit()

    return JSONResponse(status_code=201, content={
        'message': 'Stock removed successfully', 'quantity_after': movement.quantity_after,
    })


@router.post('/inventory/adjust')
def adjust_stock(member: Annotated[dict, Depends(permission('inventory:adjust'))], db: db_dependency, body: StockAdjust):

    org_id = member['organization_id']
    get_warehouse(db, org_id, body.warehouse_id)
    product, variant = validate_item(db, org_id, body.product_id, body.variant_id)

    row = get_stock_row(db, org_id, body.warehouse_id, product.id, body.variant_id)
    current = row.quantity if row else 0
    delta = body.new_quantity - current
    if delta == 0:
        raise HTTPException(status_code=400, detail='New quantity is the same as the current quantity')

    movement = change_stock(db, member, body.warehouse_id, product, variant, delta, 'adjustment',
                            unit_cost=default_cost(product, variant), reference_type='manual', note=body.note)

    log_action(db, member, 'stock_adjust', 'stock', product.id,
               old={'quantity': current}, new={'quantity': body.new_quantity, 'reason': body.note,
                                              'warehouse_id': body.warehouse_id, 'variant_id': body.variant_id})
    db.commit()

    return JSONResponse(status_code=200, content={
        'message': 'Stock adjusted successfully', 'quantity_after': movement.quantity_after,
    })


@router.post('/inventory/transfer')
def transfer_stock(member: Annotated[dict, Depends(permission('inventory:transfer'))], db: db_dependency, body: StockTransfer):

    org_id = member['organization_id']
    if body.from_warehouse_id == body.to_warehouse_id:
        raise HTTPException(status_code=400, detail='Source and destination warehouse must be different')

    get_warehouse(db, org_id, body.from_warehouse_id)
    get_warehouse(db, org_id, body.to_warehouse_id)
    product, variant = validate_item(db, org_id, body.product_id, body.variant_id)
    cost = default_cost(product, variant)

    note = body.note or f'Transfer {body.from_warehouse_id} -> {body.to_warehouse_id}'
    out = change_stock(db, member, body.from_warehouse_id, product, variant, -body.quantity,
                       'transfer_out', unit_cost=cost, reference_type='transfer', note=note)
    change_stock(db, member, body.to_warehouse_id, product, variant, body.quantity,
                 'transfer_in', unit_cost=cost, reference_type='transfer', note=note)

    log_action(db, member, 'stock_transfer', 'stock', product.id, new={
        'from_warehouse_id': body.from_warehouse_id, 'to_warehouse_id': body.to_warehouse_id,
        'variant_id': body.variant_id, 'quantity': body.quantity,
    })
    db.commit()

    return JSONResponse(status_code=200, content={
        'message': 'Stock transferred successfully', 'source_quantity_after': out.quantity_after,
    })


# ---------------------------------------------------------------
# Current stock / low stock / movement history / valuation
# ---------------------------------------------------------------
def _stock_item(row):
    inv, product, warehouse, variant = row
    cost = default_cost(product, variant)
    return {
        'id': inv.id,
        'warehouse_id': warehouse.id,
        'warehouse_name': warehouse.name,
        'product_id': product.id,
        'product_name': product.name,
        'variant_id': variant.id if variant else None,
        'variant_name': variant.name if variant else None,
        'sku': variant.sku if variant else product.sku,
        'quantity': inv.quantity,
        'reorder_level': product.reorder_level,
        'unit_cost': cost,
        'value': cost * inv.quantity,
        'updated_at': inv.updated_at,
    }


@router.get('/inventory')
def get_current_stock(
    member: Annotated[dict, Depends(permission('inventory:read'))],
    db: db_dependency,
    search: Optional[str] = None,                        # product name / sku
    warehouse_id: Optional[int] = None,
    product_id: Optional[int] = None,
    category_id: Optional[int] = None,
    in_stock_only: bool = False,
    sort_by: str = Query('product_name', pattern='^(product_name|quantity|updated_at)$'),
    order: str = Query('asc', pattern='^(asc|desc)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    query = db.query(Inventory, Products, Warehouses, ProductVariants).join(
        Products, Products.id == Inventory.product_id
    ).join(
        Warehouses, Warehouses.id == Inventory.warehouse_id
    ).outerjoin(
        ProductVariants, ProductVariants.id == Inventory.variant_id
    ).filter(Inventory.organization_id == member['organization_id'])

    if search:
        like = f'%{search}%'
        query = query.filter(or_(
            Products.name.ilike(like), Products.sku.ilike(like), ProductVariants.sku.ilike(like),
        ))
    if warehouse_id:
        query = query.filter(Inventory.warehouse_id == warehouse_id)
    if product_id:
        query = query.filter(Inventory.product_id == product_id)
    if category_id:
        query = query.filter(Products.category_id == category_id)
    if in_stock_only:
        query = query.filter(Inventory.quantity > 0)

    sort_map = {'product_name': Products.name, 'quantity': Inventory.quantity, 'updated_at': Inventory.updated_at}
    column = sort_map[sort_by]
    query = query.order_by(column.asc() if order == 'asc' else column.desc())

    return paginate_rows(query, page, page_size, _stock_item)


@router.get('/inventory/low-stock')
def get_low_stock(
    member: Annotated[dict, Depends(permission('inventory:read'))],
    db: db_dependency,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    query = low_stock_query(db, member['organization_id']).order_by(Products.name)

    return paginate_rows(query, page, page_size, lambda r: {
        'product_id': r[0].id,
        'name': r[0].name,
        'sku': r[0].sku,
        'total_stock': int(r[1]),
        'reorder_level': r[0].reorder_level,
        'shortage': r[0].reorder_level - int(r[1]),
    })


@router.get('/inventory/movements')
def get_stock_movements(
    member: Annotated[dict, Depends(permission('inventory:read'))],
    db: db_dependency,
    warehouse_id: Optional[int] = None,
    product_id: Optional[int] = None,
    variant_id: Optional[int] = None,
    movement_type: Optional[str] = Query(None, pattern='^(in|out|adjustment|transfer_in|transfer_out)$'),
    created_by: Optional[int] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    order: str = Query('desc', pattern='^(asc|desc)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    M = StockMovements
    query = db.query(M, Products.name, Warehouses.name).join(
        Products, Products.id == M.product_id
    ).join(
        Warehouses, Warehouses.id == M.warehouse_id
    ).filter(M.organization_id == member['organization_id'])

    if warehouse_id:
        query = query.filter(M.warehouse_id == warehouse_id)
    if product_id:
        query = query.filter(M.product_id == product_id)
    if variant_id:
        query = query.filter(M.variant_id == variant_id)
    if movement_type:
        query = query.filter(M.movement_type == movement_type)
    if created_by:
        query = query.filter(M.created_by == created_by)
    if from_date:
        query = query.filter(M.created_at >= from_date)
    if to_date:
        query = query.filter(M.created_at <= to_date)

    query = query.order_by(M.created_at.asc() if order == 'asc' else M.created_at.desc(), M.id.desc())

    return paginate_rows(query, page, page_size, lambda r: {
        **{c.name: getattr(r[0], c.name) for c in r[0].__table__.columns},
        'product_name': r[1],
        'warehouse_name': r[2],
    })


@router.get('/inventory/valuation')
def get_inventory_valuation(
    member: Annotated[dict, Depends(permission('inventory:read'))],
    db: db_dependency,
    warehouse_id: Optional[int] = None,
):
    # value = quantity x cost price (variant cost if it has one, else product cost)
    return valuation_summary(db, member['organization_id'], warehouse_id)