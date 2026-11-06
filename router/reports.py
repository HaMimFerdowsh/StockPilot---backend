from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func

from models import (
    Products, Categories, SalesOrders, SalesOrderItems, PurchaseOrders, Inventory,
)
from router.auth import db_dependency, permission
from router.organizations import paginate_rows
from router.subscriptions import require_feature
from router.inventory import low_stock_query, valuation_summary

router = APIRouter()

GROUP_BY = Query('month', pattern='^(day|month|year)$')       # Daily / Monthly / Yearly


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def guard(db, member):
    require_feature(db, member['organization_id'], 'reports')


def period_label(value):
    return value.date().isoformat() if value is not None else None


def date_filters(query, column, from_date, to_date):
    if from_date:
        query = query.filter(column >= from_date)
    if to_date:
        query = query.filter(column <= to_date)
    return query


def zero(value):
    return value if value is not None else Decimal('0')


# ---------------------------------------------------------------
# Products / stock / low stock
# ---------------------------------------------------------------
@router.get('/reports/products')
def total_products_report(member: Annotated[dict, Depends(permission('report:read'))], db: db_dependency):

    guard(db, member)
    org_id = member['organization_id']

    total = db.query(Products).filter(Products.organization_id == org_id).count()
    active = db.query(Products).filter(Products.organization_id == org_id, Products.is_active == True).count()

    by_category = db.query(Categories.name, func.count(Products.id)).outerjoin(
        Products, Products.category_id == Categories.id
    ).filter(Categories.organization_id == org_id).group_by(Categories.name).order_by(Categories.name).all()

    uncategorized = db.query(Products).filter(
        Products.organization_id == org_id, Products.category_id.is_(None)
    ).count()

    return {
        'total_products': total,
        'active_products': active,
        'inactive_products': total - active,
        'uncategorized': uncategorized,
        'by_category': [{'category': name, 'products': count} for name, count in by_category],
    }


@router.get('/reports/stock')
def total_stock_report(member: Annotated[dict, Depends(permission('report:read'))], db: db_dependency):

    guard(db, member)
    summary = valuation_summary(db, member['organization_id'])

    stocked = db.query(func.count(func.distinct(Inventory.product_id))).filter(
        Inventory.organization_id == member['organization_id'], Inventory.quantity > 0
    ).scalar()

    return {'products_in_stock': int(stocked or 0), **summary}


@router.get('/reports/low-stock')
def low_stock_report(
    member: Annotated[dict, Depends(permission('report:read'))],
    db: db_dependency,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    guard(db, member)
    query = low_stock_query(db, member['organization_id']).order_by(Products.name)

    return paginate_rows(query, page, page_size, lambda r: {
        'product_id': r[0].id,
        'name': r[0].name,
        'sku': r[0].sku,
        'total_stock': int(r[1]),
        'reorder_level': r[0].reorder_level,
        'shortage': r[0].reorder_level - int(r[1]),
    })


@router.get('/reports/inventory-valuation')
def inventory_valuation_report(member: Annotated[dict, Depends(permission('report:read'))], db: db_dependency,
                               warehouse_id: Optional[int] = None):

    guard(db, member)
    return valuation_summary(db, member['organization_id'], warehouse_id)


# ---------------------------------------------------------------
# Sales / purchases / profit & loss  (day / month / year)
# Only COMPLETED sales and RECEIVED purchases are counted.
# ---------------------------------------------------------------
@router.get('/reports/sales')
def sales_report(
    member: Annotated[dict, Depends(permission('report:read'))],
    db: db_dependency,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    group_by: str = GROUP_BY,
):
    guard(db, member)
    S = SalesOrders
    period = func.date_trunc(group_by, S.completed_date)

    query = db.query(
        period, func.count(S.id), func.coalesce(func.sum(S.subtotal), 0),
        func.coalesce(func.sum(S.discount), 0), func.coalesce(func.sum(S.tax), 0),
        func.coalesce(func.sum(S.total), 0),
    ).filter(S.organization_id == member['organization_id'], S.status == 'completed')
    query = date_filters(query, S.completed_date, from_date, to_date)
    rows = query.group_by(period).order_by(period).all()

    data = [
        {'period': period_label(r[0]), 'orders': r[1], 'subtotal': r[2],
         'discount': r[3], 'tax': r[4], 'total': r[5]}
        for r in rows
    ]
    return {
        'group_by': group_by,
        'data': data,
        'totals': {
            'orders': sum(d['orders'] for d in data),
            'total': sum((d['total'] for d in data), Decimal('0')),
        },
    }


@router.get('/reports/purchases')
def purchase_report(
    member: Annotated[dict, Depends(permission('report:read'))],
    db: db_dependency,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    group_by: str = GROUP_BY,
):
    guard(db, member)
    P = PurchaseOrders
    period = func.date_trunc(group_by, P.received_date)

    query = db.query(
        period, func.count(P.id), func.coalesce(func.sum(P.subtotal), 0),
        func.coalesce(func.sum(P.tax), 0), func.coalesce(func.sum(P.total), 0),
    ).filter(P.organization_id == member['organization_id'], P.status == 'received')
    query = date_filters(query, P.received_date, from_date, to_date)
    rows = query.group_by(period).order_by(period).all()

    data = [
        {'period': period_label(r[0]), 'orders': r[1], 'subtotal': r[2], 'tax': r[3], 'total': r[4]}
        for r in rows
    ]
    return {
        'group_by': group_by,
        'data': data,
        'totals': {
            'orders': sum(d['orders'] for d in data),
            'total': sum((d['total'] for d in data), Decimal('0')),
        },
    }


@router.get('/reports/profit-loss')
def profit_loss_report(
    member: Annotated[dict, Depends(permission('report:read'))],
    db: db_dependency,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    group_by: str = GROUP_BY,
):
    """
    revenue      = sales subtotal - discount   (tax is not income)
    cogs         = quantity x unit cost saved when the order was completed
    gross_profit = revenue - cogs
    """
    guard(db, member)
    org_id = member['organization_id']
    S = SalesOrders

    period = func.date_trunc(group_by, S.completed_date)
    sales_q = db.query(
        period, func.coalesce(func.sum(S.subtotal), 0), func.coalesce(func.sum(S.discount), 0),
    ).filter(S.organization_id == org_id, S.status == 'completed')
    sales_q = date_filters(sales_q, S.completed_date, from_date, to_date)
    sales_rows = sales_q.group_by(period).all()

    cogs_q = db.query(
        period, func.coalesce(func.sum(SalesOrderItems.quantity * SalesOrderItems.unit_cost), 0),
    ).join(S, S.id == SalesOrderItems.sales_order_id).filter(
        S.organization_id == org_id, S.status == 'completed'
    )
    cogs_q = date_filters(cogs_q, S.completed_date, from_date, to_date)
    cogs_map = {period_label(r[0]): r[1] for r in cogs_q.group_by(period).all()}

    data = []
    for r in sorted(sales_rows, key=lambda x: x[0]):
        label = period_label(r[0])
        revenue = r[1] - r[2]
        cogs = zero(cogs_map.get(label))
        profit = revenue - cogs
        data.append({
            'period': label,
            'revenue': revenue,
            'cogs': cogs,
            'gross_profit': profit,
            'margin_percent': round(float(profit / revenue * 100), 2) if revenue else 0,
        })

    total_revenue = sum((d['revenue'] for d in data), Decimal('0'))
    total_cogs = sum((d['cogs'] for d in data), Decimal('0'))
    total_profit = total_revenue - total_cogs

    return {
        'group_by': group_by,
        'data': data,
        'totals': {
            'revenue': total_revenue,
            'cogs': total_cogs,
            'gross_profit': total_profit,
            'margin_percent': round(float(total_profit / total_revenue * 100), 2) if total_revenue else 0,
        },
    }


# ---------------------------------------------------------------
# Dashboard (quick numbers for the home screen)
# ---------------------------------------------------------------
@router.get('/reports/dashboard')
def dashboard(member: Annotated[dict, Depends(permission('report:read'))], db: db_dependency):

    guard(db, member)
    org_id = member['organization_id']
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    sales_month = db.query(func.count(SalesOrders.id), func.coalesce(func.sum(SalesOrders.total), 0)).filter(
        SalesOrders.organization_id == org_id, SalesOrders.status == 'completed',
        SalesOrders.completed_date >= month_start,
    ).one()
    purchases_month = db.query(func.count(PurchaseOrders.id), func.coalesce(func.sum(PurchaseOrders.total), 0)).filter(
        PurchaseOrders.organization_id == org_id, PurchaseOrders.status == 'received',
        PurchaseOrders.received_date >= month_start,
    ).one()
    valuation = valuation_summary(db, org_id)

    return {
        'total_products': db.query(Products).filter(Products.organization_id == org_id, Products.is_active == True).count(),
        'low_stock_products': low_stock_query(db, org_id).count(),
        'stock_quantity': valuation['total_quantity'],
        'stock_value': valuation['total_value'],
        'sales_this_month': {'orders': sales_month[0], 'total': sales_month[1]},
        'purchases_this_month': {'orders': purchases_month[0], 'total': purchases_month[1]},
        'open_sales_orders': db.query(SalesOrders).filter(
            SalesOrders.organization_id == org_id, SalesOrders.status.in_(['draft', 'confirmed'])).count(),
        'open_purchase_orders': db.query(PurchaseOrders).filter(
            PurchaseOrders.organization_id == org_id, PurchaseOrders.status.in_(['draft', 'ordered'])).count(),
    }