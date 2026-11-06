from datetime import datetime
from typing import Optional, Annotated

from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import or_, func

from models import Suppliers, PurchaseOrders, Products
from router.auth import db_dependency, permission
from router.organizations import paginate, apply_sort, to_dict
from router.audit_logs import log_action, snapshot

router = APIRouter()


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class SupplierCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    contact_person: Optional[str] = Field(default=None, max_length=100)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=200)


class SupplierUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    contact_person: Optional[str] = Field(default=None, max_length=100)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=200)
    is_active: Optional[bool] = None


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def get_supplier(db, member, supplier_id: int):
    supplier = db.query(Suppliers).filter(
        Suppliers.id == supplier_id, Suppliers.organization_id == member['organization_id']
    ).first()
    if supplier is None:
        raise HTTPException(status_code=404, detail='Supplier Not Found')
    return supplier


# ---------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------
@router.post('/suppliers')
def create_supplier(
    member: Annotated[dict, Depends(permission('supplier:create'))],
    db: db_dependency,
    new_supplier: SupplierCreate
):

    supplier = Suppliers(organization_id=member['organization_id'], **new_supplier.model_dump())
    db.add(supplier)
    db.flush()

    log_action(db, member, 'create', 'supplier', supplier.id, new=snapshot(supplier))
    db.commit()

    return JSONResponse(status_code=201, content={'message': 'Supplier added successfully', 'supplier_id': supplier.id})


@router.get('/suppliers')
def get_all_suppliers(
    member: Annotated[dict, Depends(permission('supplier:read'))],
    db: db_dependency,
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
    sort_by: str = Query('name', pattern='^(name|created_at)$'),
    order: str = Query('asc', pattern='^(asc|desc)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    query = db.query(Suppliers).filter(Suppliers.organization_id == member['organization_id'])

    if search:
        like = f'%{search}%'
        cond = [Suppliers.name.ilike(like), Suppliers.contact_person.ilike(like),
                Suppliers.email.ilike(like), Suppliers.phone.ilike(like)]
        if search.isdigit():
            cond.append(Suppliers.id == int(search))
        query = query.filter(or_(*cond))
    if is_active is not None:
        query = query.filter(Suppliers.is_active == is_active)

    return paginate(apply_sort(query, Suppliers, sort_by, order), page, page_size)


@router.get('/suppliers/{supplier_id}')
def get_supplier_details(
    member: Annotated[dict, Depends(permission('supplier:read'))],
    db: db_dependency,
    supplier_id: int
):

    supplier = get_supplier(db, member, supplier_id)

    orders, spent = db.query(
        func.count(PurchaseOrders.id), func.coalesce(func.sum(PurchaseOrders.total), 0)
    ).filter(
        PurchaseOrders.supplier_id == supplier.id, PurchaseOrders.status == 'received'
    ).one()

    return {**to_dict(supplier), 'received_orders': int(orders), 'total_purchased': spent}


@router.get('/suppliers/{supplier_id}/purchases')
def get_supplier_purchase_history(
    member: Annotated[dict, Depends(permission('supplier:read'))],
    db: db_dependency,
    supplier_id: int,
    status: Optional[str] = Query(None, pattern='^(draft|ordered|received|cancelled)$'),
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    supplier = get_supplier(db, member, supplier_id)

    query = db.query(PurchaseOrders).filter(
        PurchaseOrders.organization_id == member['organization_id'],
        PurchaseOrders.supplier_id == supplier.id,
    )
    if status:
        query = query.filter(PurchaseOrders.status == status)
    if from_date:
        query = query.filter(PurchaseOrders.order_date >= from_date)
    if to_date:
        query = query.filter(PurchaseOrders.order_date <= to_date)

    return paginate(apply_sort(query, PurchaseOrders, 'order_date', 'desc'), page, page_size)


@router.put('/suppliers/{supplier_id}')
def update_supplier(
    member: Annotated[dict, Depends(permission('supplier:update'))],
    db: db_dependency,
    update_supplier: SupplierUpdate,
    supplier_id: int
):

    supplier = get_supplier(db, member, supplier_id)
    old = snapshot(supplier)

    update_data = update_supplier.model_dump(exclude_unset=True)
    for field in ('name', 'is_active'):
        if field in update_data and update_data[field] is None:
            raise HTTPException(status_code=422, detail=f'{field} cannot be null')

    for key, value in update_data.items():
        setattr(supplier, key, value)

    db.flush()
    log_action(db, member, 'update', 'supplier', supplier.id, old=old, new=snapshot(supplier))
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Supplier updated successfully'})


@router.delete('/suppliers/{supplier_id}')
def delete_supplier(
    member: Annotated[dict, Depends(permission('supplier:delete'))],
    db: db_dependency,
    supplier_id: int
):

    supplier = get_supplier(db, member, supplier_id)

    if db.query(PurchaseOrders.id).filter(PurchaseOrders.supplier_id == supplier.id).first():
        raise HTTPException(status_code=400, detail='Supplier has purchase orders. Set is_active to false instead')
    if db.query(Products.id).filter(Products.supplier_id == supplier.id).first():
        raise HTTPException(status_code=400, detail='Supplier is used by products. Change those products first')

    old = snapshot(supplier)
    db.delete(supplier)
    log_action(db, member, 'delete', 'supplier', supplier_id, old=old)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Supplier deleted successfully'})
