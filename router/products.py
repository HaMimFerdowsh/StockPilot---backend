import re
import secrets
from decimal import Decimal
from typing import Optional
from typing import Annotated
from fastapi import Depends
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, func

from models import (
    Products, ProductVariants, Categories, Suppliers, Inventory, Warehouses,
    StockMovements, PurchaseOrderItems, SalesOrderItems,
)
from router.auth import db_dependency, permission
from router.organizations import to_dict, paginate_rows
from router.audit_logs import log_action, snapshot
from router.subscriptions import check_limit

router = APIRouter()

Money = Decimal


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class ProductCreate(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    description: Optional[str] = Field(default=None, max_length=2000)
    category_id: Optional[int] = None
    supplier_id: Optional[int] = None
    sku: Optional[str] = Field(default=None, min_length=2, max_length=50)       # empty = auto generate
    barcode: Optional[str] = Field(default=None, min_length=4, max_length=50)
    generate_barcode: bool = True                                                # auto EAN-13 if no barcode given
    unit: str = Field(default='pcs', min_length=1, max_length=20)
    cost_price: Decimal = Field(default=Decimal('0'), ge=0, max_digits=12, decimal_places=2)
    selling_price: Decimal = Field(default=Decimal('0'), ge=0, max_digits=12, decimal_places=2)
    reorder_level: int = Field(default=0, ge=0, le=1000000)


class ProductUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=150)
    description: Optional[str] = Field(default=None, max_length=2000)
    category_id: Optional[int] = None
    supplier_id: Optional[int] = None
    sku: Optional[str] = Field(default=None, min_length=2, max_length=50)
    barcode: Optional[str] = Field(default=None, min_length=4, max_length=50)
    unit: Optional[str] = Field(default=None, min_length=1, max_length=20)
    cost_price: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    selling_price: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    reorder_level: Optional[int] = Field(default=None, ge=0, le=1000000)
    is_active: Optional[bool] = None


class VariantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)                              # e.g. "Red / XL"
    sku: Optional[str] = Field(default=None, min_length=2, max_length=50)
    barcode: Optional[str] = Field(default=None, min_length=4, max_length=50)
    generate_barcode: bool = True
    attributes: Optional[dict] = None
    cost_price: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    selling_price: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)


class VariantUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    sku: Optional[str] = Field(default=None, min_length=2, max_length=50)
    barcode: Optional[str] = Field(default=None, min_length=4, max_length=50)
    attributes: Optional[dict] = None
    cost_price: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    selling_price: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    is_active: Optional[bool] = None


# ---------------------------------------------------------------
# SKU & barcode helpers (csv_io.py uses these too)
# ---------------------------------------------------------------
def sku_in_use(db, org_id: int, sku: str, exclude_product_id=None, exclude_variant_id=None):
    p = db.query(Products.id).filter(Products.organization_id == org_id, Products.sku == sku)
    if exclude_product_id:
        p = p.filter(Products.id != exclude_product_id)
    v = db.query(ProductVariants.id).filter(ProductVariants.organization_id == org_id, ProductVariants.sku == sku)
    if exclude_variant_id:
        v = v.filter(ProductVariants.id != exclude_variant_id)
    return p.first() is not None or v.first() is not None


def barcode_in_use(db, org_id: int, code: str, exclude_product_id=None, exclude_variant_id=None):
    p = db.query(Products.id).filter(Products.organization_id == org_id, Products.barcode == code)
    if exclude_product_id:
        p = p.filter(Products.id != exclude_product_id)
    v = db.query(ProductVariants.id).filter(ProductVariants.organization_id == org_id, ProductVariants.barcode == code)
    if exclude_variant_id:
        v = v.filter(ProductVariants.id != exclude_variant_id)
    return p.first() is not None or v.first() is not None


def generate_sku(db, org_id: int, name: str):
    prefix = re.sub(r'[^A-Z0-9]', '', name.upper())[:3].ljust(3, 'X')
    while True:
        sku = f'{prefix}-{secrets.token_hex(3).upper()}'
        if not sku_in_use(db, org_id, sku):
            return sku


def ean13_check_digit(first12: str):
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(first12))
    return str((10 - total % 10) % 10)


def is_valid_ean13(code: str):
    return len(code) == 13 and code.isdigit() and code[-1] == ean13_check_digit(code[:12])


def generate_barcode(db, org_id: int):
    # numbers starting with 200-299 are reserved for in-store use, so they never clash with real products
    while True:
        body = '200' + ''.join(secrets.choice('0123456789') for _ in range(9))
        code = body + ean13_check_digit(body)
        if not barcode_in_use(db, org_id, code):
            return code


_L = ['0001101', '0011001', '0010011', '0111101', '0100011',
      '0110001', '0101111', '0111011', '0110111', '0001011']
_R = [''.join('1' if b == '0' else '0' for b in code) for code in _L]
_G = [code[::-1] for code in _R]
_PARITY = ['LLLLLL', 'LLGLGG', 'LLGGLG', 'LLGGGL', 'LGLLGG',
           'LGGLLG', 'LGGGLL', 'LGLGLG', 'LGLGGL', 'LGGLGL']


def ean13_svg(code: str):
    bits = '101'
    for i, ch in enumerate(code[1:7]):
        bits += (_L if _PARITY[int(code[0])][i] == 'L' else _G)[int(ch)]
    bits += '01010'
    for ch in code[7:]:
        bits += _R[int(ch)]
    bits += '101'

    module, quiet, bar_h = 3, 10, 90
    width = (len(bits) + 2 * quiet) * module
    bars = ''.join(
        f'<rect x="{(quiet + i) * module}" y="8" width="{module}" height="{bar_h}"/>'
        for i, b in enumerate(bits) if b == '1'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{bar_h + 34}" '
        f'viewBox="0 0 {width} {bar_h + 34}">'
        f'<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<g fill="#000000">{bars}</g>'
        f'<text x="{width / 2}" y="{bar_h + 26}" text-anchor="middle" font-family="monospace" '
        f'font-size="18" letter-spacing="4">{code}</text></svg>'
    )


# ---------------------------------------------------------------
# Other helpers
# ---------------------------------------------------------------
def get_product(db, member, product_id: int):
    product = db.query(Products).filter(
        Products.id == product_id, Products.organization_id == member['organization_id']
    ).first()
    if product is None:
        raise HTTPException(status_code=404, detail='Product Not Found')
    return product


def get_variant(db, member, variant_id: int):
    variant = db.query(ProductVariants).filter(
        ProductVariants.id == variant_id, ProductVariants.organization_id == member['organization_id']
    ).first()
    if variant is None:
        raise HTTPException(status_code=404, detail='Variant Not Found')
    return variant


def check_refs(db, member, category_id, supplier_id):
    org_id = member['organization_id']
    if category_id and db.query(Categories).filter(
        Categories.id == category_id, Categories.organization_id == org_id
    ).first() is None:
        raise HTTPException(status_code=404, detail='Category Not Found')
    if supplier_id and db.query(Suppliers).filter(
        Suppliers.id == supplier_id, Suppliers.organization_id == org_id
    ).first() is None:
        raise HTTPException(status_code=404, detail='Supplier Not Found')


def product_has_history(db, product_id: int):
    return (
        db.query(StockMovements.id).filter(StockMovements.product_id == product_id).first()
        or db.query(PurchaseOrderItems.id).filter(PurchaseOrderItems.product_id == product_id).first()
        or db.query(SalesOrderItems.id).filter(SalesOrderItems.product_id == product_id).first()
    ) is not None


def variant_has_history(db, variant_id: int):
    return (
        db.query(StockMovements.id).filter(StockMovements.variant_id == variant_id).first()
        or db.query(PurchaseOrderItems.id).filter(PurchaseOrderItems.variant_id == variant_id).first()
        or db.query(SalesOrderItems.id).filter(SalesOrderItems.variant_id == variant_id).first()
    ) is not None


# ---------------------------------------------------------------
# Products
# ---------------------------------------------------------------
@router.post('/products')
def create_product(member: Annotated[dict, Depends(permission('product:create'))], db: db_dependency, new_product: ProductCreate):

    org_id = member['organization_id']
    check_limit(db, org_id, 'products')
    check_refs(db, member, new_product.category_id, new_product.supplier_id)

    sku = new_product.sku.strip().upper() if new_product.sku else generate_sku(db, org_id, new_product.name)
    if new_product.sku and sku_in_use(db, org_id, sku):
        raise HTTPException(status_code=400, detail='SKU already exists')

    barcode = new_product.barcode
    if barcode:
        if barcode_in_use(db, org_id, barcode):
            raise HTTPException(status_code=400, detail='Barcode already exists')
    elif new_product.generate_barcode:
        barcode = generate_barcode(db, org_id)

    data = new_product.model_dump(exclude={'sku', 'barcode', 'generate_barcode'})
    product = Products(**data, organization_id=org_id, sku=sku, barcode=barcode)
    db.add(product)
    db.flush()

    log_action(db, member, 'create', 'product', product.id, new=snapshot(product))
    db.commit()

    return JSONResponse(status_code=201, content={
        'message': 'Product added successfully',
        'product_id': product.id, 'sku': sku, 'barcode': barcode,
    })


@router.get('/products')
def get_all_products(
    member: Annotated[dict, Depends(permission('product:read'))],
    db: db_dependency,
    search: Optional[str] = None,                        # name / sku / barcode / ID
    category_id: Optional[int] = None,
    supplier_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    min_price: Optional[Decimal] = Query(None, ge=0),    # selling price range
    max_price: Optional[Decimal] = Query(None, ge=0),
    sort_by: str = Query('name', pattern='^(name|sku|selling_price|cost_price|created_at|total_stock)$'),
    order: str = Query('asc', pattern='^(asc|desc)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    org_id = member['organization_id']

    stock_sq = db.query(
        Inventory.product_id.label('product_id'),
        func.sum(Inventory.quantity).label('stock'),
    ).filter(Inventory.organization_id == org_id).group_by(Inventory.product_id).subquery()
    stock = func.coalesce(stock_sq.c.stock, 0)

    query = db.query(Products, stock.label('total_stock')).outerjoin(
        stock_sq, stock_sq.c.product_id == Products.id
    ).filter(Products.organization_id == org_id)

    if search:
        like = f'%{search}%'
        cond = [Products.name.ilike(like), Products.sku.ilike(like), Products.barcode.ilike(like)]
        if search.isdigit():
            cond.append(Products.id == int(search))
        query = query.filter(or_(*cond))
    if category_id:
        query = query.filter(Products.category_id == category_id)
    if supplier_id:
        query = query.filter(Products.supplier_id == supplier_id)
    if is_active is not None:
        query = query.filter(Products.is_active == is_active)
    if min_price is not None:
        query = query.filter(Products.selling_price >= min_price)
    if max_price is not None:
        query = query.filter(Products.selling_price <= max_price)

    sort_map = {
        'name': Products.name, 'sku': Products.sku, 'selling_price': Products.selling_price,
        'cost_price': Products.cost_price, 'created_at': Products.created_at, 'total_stock': stock,
    }
    column = sort_map[sort_by]
    query = query.order_by(column.asc() if order == 'asc' else column.desc(), Products.id)

    return paginate_rows(query, page, page_size, lambda r: {**to_dict(r[0]), 'total_stock': int(r[1])})


@router.get('/products/barcode/{barcode}')
def find_by_barcode(member: Annotated[dict, Depends(permission('product:read'))], db: db_dependency, barcode: str):
    # for barcode scanners: returns the product (and the variant if the barcode belongs to one)
    org_id = member['organization_id']

    product = db.query(Products).filter(Products.organization_id == org_id, Products.barcode == barcode).first()
    if product:
        return {'product': to_dict(product), 'variant': None}

    variant = db.query(ProductVariants).filter(
        ProductVariants.organization_id == org_id, ProductVariants.barcode == barcode
    ).first()
    if variant:
        product = db.query(Products).filter(Products.id == variant.product_id).first()
        return {'product': to_dict(product), 'variant': to_dict(variant)}

    raise HTTPException(status_code=404, detail='Barcode Not Found')


@router.get('/products/{product_id}')
def get_product_details(member: Annotated[dict, Depends(permission('product:read'))], db: db_dependency, product_id: int):

    product = get_product(db, member, product_id)

    category = db.query(Categories).filter(Categories.id == product.category_id).first()
    supplier = db.query(Suppliers).filter(Suppliers.id == product.supplier_id).first()
    variants = db.query(ProductVariants).filter(ProductVariants.product_id == product.id).order_by(ProductVariants.id).all()

    stock_rows = db.query(Inventory, Warehouses.name).join(
        Warehouses, Warehouses.id == Inventory.warehouse_id
    ).filter(Inventory.product_id == product.id, Inventory.organization_id == member['organization_id']).all()

    return {
        **to_dict(product),
        'category_name': category.name if category else None,
        'supplier_name': supplier.name if supplier else None,
        'variants': [to_dict(v) for v in variants],
        'stock': [
            {'warehouse_id': inv.warehouse_id, 'warehouse_name': name,
             'variant_id': inv.variant_id, 'quantity': inv.quantity}
            for inv, name in stock_rows
        ],
        'total_stock': sum(inv.quantity for inv, _ in stock_rows),
    }


@router.put('/products/{product_id}')
def update_product(member: Annotated[dict, Depends(permission('product:update'))], db: db_dependency,
                   update_product: ProductUpdate, product_id: int):

    org_id = member['organization_id']
    product = get_product(db, member, product_id)
    old = snapshot(product)

    update_data = update_product.model_dump(exclude_unset=True)
    # fields that cannot be NULL
    for field in ('name', 'unit', 'cost_price', 'selling_price', 'reorder_level', 'is_active', 'sku'):
        if field in update_data and update_data[field] is None:
            raise HTTPException(status_code=422, detail=f'{field} cannot be null')

    check_refs(db, member, update_data.get('category_id'), update_data.get('supplier_id'))

    if 'sku' in update_data:
        update_data['sku'] = update_data['sku'].strip().upper()
        if update_data['sku'] != product.sku and sku_in_use(db, org_id, update_data['sku'], exclude_product_id=product.id):
            raise HTTPException(status_code=400, detail='SKU already exists')
    if update_data.get('barcode') and update_data['barcode'] != product.barcode:
        if barcode_in_use(db, org_id, update_data['barcode'], exclude_product_id=product.id):
            raise HTTPException(status_code=400, detail='Barcode already exists')
    if update_data.get('is_active') and not product.is_active:
        check_limit(db, org_id, 'products')

    for key, value in update_data.items():
        setattr(product, key, value)

    db.flush()
    log_action(db, member, 'update', 'product', product.id, old=old, new=snapshot(product))
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Product updated successfully'})


@router.delete('/products/{product_id}')
def delete_product(member: Annotated[dict, Depends(permission('product:delete'))], db: db_dependency, product_id: int):

    product = get_product(db, member, product_id)
    old = snapshot(product)

    # a product with stock / order history must stay (reports and audit need it), so it is deactivated
    if product_has_history(db, product.id):
        product.is_active = False
        log_action(db, member, 'deactivate', 'product', product.id, old=old)
        db.commit()
        return JSONResponse(status_code=200, content={
            'message': 'Product has stock or order history, so it was deactivated instead of deleted',
        })

    db.query(ProductVariants).filter(ProductVariants.product_id == product.id).delete()
    db.delete(product)
    log_action(db, member, 'delete', 'product', product_id, old=old)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Product deleted successfully'})


# ---------------------------------------------------------------
# Barcode
# ---------------------------------------------------------------
@router.post('/products/{product_id}/generate-barcode')
def generate_product_barcode(member: Annotated[dict, Depends(permission('product:update'))], db: db_dependency,
                             product_id: int, force: bool = False):

    product = get_product(db, member, product_id)
    if product.barcode and not force:
        raise HTTPException(status_code=400, detail='Product already has a barcode. Use force=true to replace it')

    old = snapshot(product)
    product.barcode = generate_barcode(db, member['organization_id'])
    log_action(db, member, 'update', 'product', product.id, old=old, new=snapshot(product))
    db.commit()

    return JSONResponse(status_code=200, content={'message': 'Barcode generated', 'barcode': product.barcode})


@router.get('/products/{product_id}/barcode')
def get_barcode_image(member: Annotated[dict, Depends(permission('product:read'))], db: db_dependency,
                      product_id: int, variant_id: Optional[int] = None):
    # returns an SVG image of the EAN-13 barcode (print it or show it in the frontend)
    product = get_product(db, member, product_id)
    code = product.barcode

    if variant_id:
        variant = get_variant(db, member, variant_id)
        if variant.product_id != product.id:
            raise HTTPException(status_code=404, detail='Variant Not Found')
        code = variant.barcode

    if not code:
        raise HTTPException(status_code=404, detail='No barcode set. Generate one first')
    if not is_valid_ean13(code):
        raise HTTPException(status_code=400, detail='Barcode image is only available for valid EAN-13 barcodes')

    return Response(content=ean13_svg(code), media_type='image/svg+xml')


# ---------------------------------------------------------------
# Variants
# ---------------------------------------------------------------
@router.get('/products/{product_id}/variants')
def get_variants(member: Annotated[dict, Depends(permission('product:read'))], db: db_dependency, product_id: int):

    product = get_product(db, member, product_id)
    variants = db.query(ProductVariants).filter(
        ProductVariants.product_id == product.id
    ).order_by(ProductVariants.id).all()
    return [to_dict(v) for v in variants]


@router.post('/products/{product_id}/variants')
def create_variant(member: Annotated[dict, Depends(permission('product:create'))], db: db_dependency,
                   new_variant: VariantCreate, product_id: int):

    org_id = member['organization_id']
    product = get_product(db, member, product_id)

    if not product.has_variants:
        # stock that was added without a variant would be left behind, so it must be cleared first
        loose_stock = db.query(Inventory.id).filter(
            Inventory.product_id == product.id, Inventory.variant_id.is_(None), Inventory.quantity != 0
        ).first()
        if loose_stock:
            raise HTTPException(
                status_code=400,
                detail='This product has stock without a variant. Adjust that stock to 0 before adding variants',
            )

    if new_variant.sku:
        sku = new_variant.sku.strip().upper()
        if sku_in_use(db, org_id, sku):
            raise HTTPException(status_code=400, detail='SKU already exists')
    else:
        n = db.query(ProductVariants).filter(ProductVariants.product_id == product.id).count() + 1
        while sku_in_use(db, org_id, f'{product.sku}-{n}'):
            n += 1
        sku = f'{product.sku}-{n}'

    barcode = new_variant.barcode
    if barcode:
        if barcode_in_use(db, org_id, barcode):
            raise HTTPException(status_code=400, detail='Barcode already exists')
    elif new_variant.generate_barcode:
        barcode = generate_barcode(db, org_id)

    data = new_variant.model_dump(exclude={'sku', 'barcode', 'generate_barcode'})
    variant = ProductVariants(**data, organization_id=org_id, product_id=product.id, sku=sku, barcode=barcode)
    db.add(variant)
    product.has_variants = True
    db.flush()

    log_action(db, member, 'create', 'product_variant', variant.id, new=snapshot(variant))
    db.commit()

    return JSONResponse(status_code=201, content={
        'message': 'Variant added successfully',
        'variant_id': variant.id, 'sku': sku, 'barcode': barcode,
    })


@router.put('/variants/{variant_id}')
def update_variant(member: Annotated[dict, Depends(permission('product:update'))], db: db_dependency,
                   update_variant: VariantUpdate, variant_id: int):

    org_id = member['organization_id']
    variant = get_variant(db, member, variant_id)
    old = snapshot(variant)

    update_data = update_variant.model_dump(exclude_unset=True)
    for field in ('name', 'sku', 'is_active'):
        if field in update_data and update_data[field] is None:
            raise HTTPException(status_code=422, detail=f'{field} cannot be null')

    if 'sku' in update_data:
        update_data['sku'] = update_data['sku'].strip().upper()
        if update_data['sku'] != variant.sku and sku_in_use(db, org_id, update_data['sku'], exclude_variant_id=variant.id):
            raise HTTPException(status_code=400, detail='SKU already exists')
    if update_data.get('barcode') and update_data['barcode'] != variant.barcode:
        if barcode_in_use(db, org_id, update_data['barcode'], exclude_variant_id=variant.id):
            raise HTTPException(status_code=400, detail='Barcode already exists')

    for key, value in update_data.items():
        setattr(variant, key, value)

    db.flush()
    log_action(db, member, 'update', 'product_variant', variant.id, old=old, new=snapshot(variant))
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Variant updated successfully'})


@router.delete('/variants/{variant_id}')
def delete_variant(member: Annotated[dict, Depends(permission('product:delete'))], db: db_dependency, variant_id: int):

    variant = get_variant(db, member, variant_id)
    old = snapshot(variant)

    if variant_has_history(db, variant.id):
        variant.is_active = False
        log_action(db, member, 'deactivate', 'product_variant', variant.id, old=old)
        db.commit()
        return JSONResponse(status_code=200, content={
            'message': 'Variant has stock or order history, so it was deactivated instead of deleted',
        })

    product_id = variant.product_id
    db.delete(variant)
    db.flush()

    remaining = db.query(ProductVariants).filter(ProductVariants.product_id == product_id).count()
    if remaining == 0:
        db.query(Products).filter(Products.id == product_id).update({Products.has_variants: False})

    log_action(db, member, 'delete', 'product_variant', variant_id, old=old)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Variant deleted successfully'})