from datetime import datetime
from typing import Optional
from typing import Annotated
from fastapi import Depends
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import or_, func

from models import Customers, SalesOrders
from router.auth import db_dependency, permission
from router.organizations import paginate, apply_sort, to_dict
from router.audit_logs import log_action, snapshot

router = APIRouter()


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class CustomerCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=200)


class CustomerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=200)
    is_active: Optional[bool] = None


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def get_customer(db, member, customer_id: int):
    customer = db.query(Customers).filter(
        Customers.id == customer_id, Customers.organization_id == member['organization_id']
    ).first()
    if customer is None:
        raise HTTPException(status_code=404, detail='Customer Not Found')
    return customer


# ---------------------------------------------------------------
# Customers
# ---------------------------------------------------------------
@router.post('/customers')
def create_customer(member: Annotated[dict, Depends(permission('customer:create'))], db: db_dependency, new_customer: CustomerCreate):

    customer = Customers(organization_id=member['organization_id'], **new_customer.model_dump())
    db.add(customer)
    db.flush()

    log_action(db, member, 'create', 'customer', customer.id, new=snapshot(customer))
    db.commit()

    return JSONResponse(status_code=201, content={'message': 'Customer added successfully', 'customer_id': customer.id})


@router.get('/customers')
def get_all_customers(
    member: Annotated[dict, Depends(permission('customer:read'))],
    db: db_dependency,
    search: Optional[str] = None,                        # name / email / phone / ID
    is_active: Optional[bool] = None,
    sort_by: str = Query('name', pattern='^(name|created_at)$'),
    order: str = Query('asc', pattern='^(asc|desc)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    query = db.query(Customers).filter(Customers.organization_id == member['organization_id'])

    if search:
        like = f'%{search}%'
        cond = [Customers.name.ilike(like), Customers.email.ilike(like), Customers.phone.ilike(like)]
        if search.isdigit():
            cond.append(Customers.id == int(search))
        query = query.filter(or_(*cond))
    if is_active is not None:
        query = query.filter(Customers.is_active == is_active)

    return paginate(apply_sort(query, Customers, sort_by, order), page, page_size)


@router.get('/customers/{customer_id}')
def get_customer_details(member: Annotated[dict, Depends(permission('customer:read'))], db: db_dependency, customer_id: int):

    customer = get_customer(db, member, customer_id)

    orders, spent = db.query(
        func.count(SalesOrders.id), func.coalesce(func.sum(SalesOrders.total), 0)
    ).filter(
        SalesOrders.customer_id == customer.id, SalesOrders.status == 'completed'
    ).one()

    return {**to_dict(customer), 'completed_orders': int(orders), 'total_spent': spent}


@router.get('/customers/{customer_id}/purchases')
def get_customer_purchase_history(
    member: Annotated[dict, Depends(permission('customer:read'))],
    db: db_dependency,
    customer_id: int,
    status: Optional[str] = Query(None, pattern='^(draft|confirmed|completed|cancelled)$'),
    from_date: Optional[datetime] = None,                # order_date range
    to_date: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    customer = get_customer(db, member, customer_id)

    query = db.query(SalesOrders).filter(
        SalesOrders.organization_id == member['organization_id'],
        SalesOrders.customer_id == customer.id,
    )
    if status:
        query = query.filter(SalesOrders.status == status)
    if from_date:
        query = query.filter(SalesOrders.order_date >= from_date)
    if to_date:
        query = query.filter(SalesOrders.order_date <= to_date)

    return paginate(apply_sort(query, SalesOrders, 'order_date', 'desc'), page, page_size)


@router.put('/customers/{customer_id}')
def update_customer(member: Annotated[dict, Depends(permission('customer:update'))], db: db_dependency,
                    update_customer: CustomerUpdate, customer_id: int):

    customer = get_customer(db, member, customer_id)
    old = snapshot(customer)

    update_data = update_customer.model_dump(exclude_unset=True)
    for field in ('name', 'is_active'):
        if field in update_data and update_data[field] is None:
            raise HTTPException(status_code=422, detail=f'{field} cannot be null')

    for key, value in update_data.items():
        setattr(customer, key, value)

    db.flush()
    log_action(db, member, 'update', 'customer', customer.id, old=old, new=snapshot(customer))
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Customer updated successfully'})


@router.delete('/customers/{customer_id}')
def delete_customer(member: Annotated[dict, Depends(permission('customer:delete'))], db: db_dependency, customer_id: int):

    customer = get_customer(db, member, customer_id)

    if db.query(SalesOrders.id).filter(SalesOrders.customer_id == customer.id).first():
        raise HTTPException(status_code=400, detail='Customer has sales orders. Set is_active to false instead')

    old = snapshot(customer)
    db.delete(customer)
    log_action(db, member, 'delete', 'customer', customer_id, old=old)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Customer deleted successfully'})