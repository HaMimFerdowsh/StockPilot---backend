from typing import Optional
from typing import Annotated
from fastapi import Depends
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, func

from models import Categories, Products
from router.auth import db_dependency, permission
from router.organizations import paginate, apply_sort

router = APIRouter()


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class CategoryCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)


class CategoryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def get_category(db, member, category_id: int):
    category = db.query(Categories).filter(
        Categories.id == category_id,
        Categories.organization_id == member['organization_id'],
    ).first()
    if category is None:
        raise HTTPException(status_code=404, detail='Category Not Found')
    return category


def name_exists(db, member, name: str, exclude_id: Optional[int] = None):
    query = db.query(Categories).filter(
        Categories.organization_id == member['organization_id'],
        func.lower(Categories.name) == name.lower(),
    )
    if exclude_id:
        query = query.filter(Categories.id != exclude_id)
    return query.first() is not None


# ---------------------------------------------------------------
# Categories
# ---------------------------------------------------------------
@router.post('/categories')
def create_category(member: Annotated[dict, Depends(permission('category:create'))], db: db_dependency, new_category: CategoryCreate):

    name = new_category.name.strip()
    if name_exists(db, member, name):
        raise HTTPException(status_code=400, detail='Category name already exists')

    category = Categories(
        organization_id=member['organization_id'],
        name=name,
        description=new_category.description,
    )
    db.add(category)
    db.commit()

    return JSONResponse(status_code=201, content={'message': 'Category added successfully', 'category_id': category.id})


@router.get('/categories')
def get_all_categories(
    member: Annotated[dict, Depends(permission('category:read'))],
    db: db_dependency,
    search: Optional[str] = None,                        # name / ID
    sort_by: str = Query('name', pattern='^(name|created_at)$'),
    order: str = Query('asc', pattern='^(asc|desc)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    query = db.query(Categories).filter(Categories.organization_id == member['organization_id'])

    if search:
        cond = [Categories.name.ilike(f'%{search}%')]
        if search.isdigit():
            cond.append(Categories.id == int(search))
        query = query.filter(or_(*cond))

    return paginate(apply_sort(query, Categories, sort_by, order), page, page_size)


@router.get('/categories/{category_id}')
def get_category_details(member: Annotated[dict, Depends(permission('category:read'))], db: db_dependency, category_id: int):

    category = get_category(db, member, category_id)

    product_count = db.query(Products).filter(
        Products.organization_id == member['organization_id'],
        Products.category_id == category_id,
    ).count()

    return {
        'id': category.id,
        'name': category.name,
        'description': category.description,
        'created_at': category.created_at,
        'product_count': product_count,
    }


@router.put('/categories/{category_id}')
def update_category(member: Annotated[dict, Depends(permission('category:update'))], db: db_dependency,
                    update_category: CategoryUpdate, category_id: int):

    category = get_category(db, member, category_id)

    update_data = update_category.model_dump(exclude_unset=True)

    if update_data.get('name'):
        update_data['name'] = update_data['name'].strip()
        if update_data['name'].lower() != category.name.lower() and name_exists(db, member, update_data['name']):
            raise HTTPException(status_code=400, detail='Category name already exists')

    for key, value in update_data.items():
        setattr(category, key, value)

    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Category updated successfully'})


@router.delete('/categories/{category_id}')
def delete_category(member: Annotated[dict, Depends(permission('category:delete'))], db: db_dependency, category_id: int):

    category = get_category(db, member, category_id)

    in_use = db.query(Products).filter(
        Products.organization_id == member['organization_id'],
        Products.category_id == category_id,
    ).first()
    if in_use:
        raise HTTPException(status_code=400, detail='Category is in use by products')

    db.delete(category)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Category deleted successfully'})