import os
import secrets
from datetime import datetime, timedelta
from typing import Literal, Optional
from typing import Annotated
from fastapi import Depends
from fastapi import APIRouter, HTTPException, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from models import (
    Plans, Subscriptions, Payments, OrganizationMembers, Products, Warehouses,
    PurchaseOrders, SalesOrders,
)
from router.auth import db_dependency, user_dependency, permission, DEBUG

# NOTE: this file must NOT import router.organizations (organizations imports this file).

router = APIRouter()

PAYMENT_PROVIDER = os.getenv('PAYMENT_PROVIDER', 'manual')         # stripe / sslcommerz / bkash ...
WEBHOOK_SECRET = os.getenv('PAYMENT_WEBHOOK_SECRET')               # shared secret for /payments/webhook

DEFAULT_PLANS = [
    dict(name='free', price_monthly=0, price_yearly=0, max_users=3, max_products=50,
         max_warehouses=1, max_orders_per_month=50,
         allow_csv=False, allow_reports=False, allow_audit_log=False),
    dict(name='pro', price_monthly=19, price_yearly=190, max_users=10, max_products=1000,
         max_warehouses=5, max_orders_per_month=1000,
         allow_csv=True, allow_reports=True, allow_audit_log=False),
    dict(name='business', price_monthly=49, price_yearly=490, max_users=None, max_products=None,
         max_warehouses=None, max_orders_per_month=None,
         allow_csv=True, allow_reports=True, allow_audit_log=True),
]

LIMIT_FIELDS = {
    'users': 'max_users',
    'products': 'max_products',
    'warehouses': 'max_warehouses',
    'orders': 'max_orders_per_month',
}
FEATURE_FIELDS = {
    'csv': 'allow_csv',
    'reports': 'allow_reports',
    'audit': 'allow_audit_log',
}


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class CheckoutRequest(BaseModel):
    plan_id: int
    billing_cycle: Literal['monthly', 'yearly'] = 'monthly'


class WebhookBody(BaseModel):
    provider_ref: str
    status: Literal['paid', 'failed']


# ---------------------------------------------------------------
# Helpers (other routers import these)
# ---------------------------------------------------------------
def seed_plans(db):
    existing = {p.name for p in db.query(Plans).all()}
    for data in DEFAULT_PLANS:
        if data['name'] not in existing:
            db.add(Plans(**data))
    db.flush()


def get_org_subscription(db, org_id: int):
    sub = db.query(Subscriptions).filter(
        Subscriptions.organization_id == org_id, Subscriptions.status == 'active'
    ).order_by(Subscriptions.id.desc()).first()

    # lazy expiry: no background job needed
    if sub and sub.end_date and sub.end_date < datetime.now():
        sub.status = 'expired'
        db.flush()
        return None
    return sub


def get_org_plan(db, org_id: int):
    sub = get_org_subscription(db, org_id)
    if sub:
        plan = db.query(Plans).filter(Plans.id == sub.plan_id).first()
        if plan:
            return plan

    # no active paid subscription -> Free plan limits
    plan = db.query(Plans).filter(Plans.name == 'free').first()
    if plan is None:
        seed_plans(db)
        plan = db.query(Plans).filter(Plans.name == 'free').first()
    return plan


def count_usage(db, org_id: int, resource: str):
    if resource == 'users':
        return db.query(OrganizationMembers).filter(
            OrganizationMembers.organization_id == org_id,
            OrganizationMembers.is_active == True,
        ).count()
    if resource == 'products':
        return db.query(Products).filter(
            Products.organization_id == org_id, Products.is_active == True
        ).count()
    if resource == 'warehouses':
        return db.query(Warehouses).filter(
            Warehouses.organization_id == org_id, Warehouses.is_active == True
        ).count()
    if resource == 'orders':
        month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        po = db.query(PurchaseOrders).filter(
            PurchaseOrders.organization_id == org_id,
            PurchaseOrders.created_at >= month_start,
            PurchaseOrders.status != 'cancelled',
        ).count()
        so = db.query(SalesOrders).filter(
            SalesOrders.organization_id == org_id,
            SalesOrders.created_at >= month_start,
            SalesOrders.status != 'cancelled',
        ).count()
        return po + so
    raise ValueError(f'Unknown resource: {resource}')


def check_limit(db, org_id: int, resource: str, extra: int = 0):
    """Raise 403 if the organization's plan does not allow one more `resource`."""
    plan = get_org_plan(db, org_id)
    limit = getattr(plan, LIMIT_FIELDS[resource])
    if limit is None:
        return
    if count_usage(db, org_id, resource) + extra >= limit:
        raise HTTPException(
            status_code=403,
            detail=f'Plan limit reached: the {plan.name} plan allows {limit} {resource}. Please upgrade your plan',
        )


def require_feature(db, org_id: int, feature: str):
    plan = get_org_plan(db, org_id)
    if not getattr(plan, FEATURE_FIELDS[feature]):
        raise HTTPException(
            status_code=403,
            detail=f'The {plan.name} plan does not include {feature}. Please upgrade your plan',
        )


def plan_to_dict(plan):
    return {
        'id': plan.id,
        'name': plan.name,
        'price_monthly': plan.price_monthly,
        'price_yearly': plan.price_yearly,
        'max_users': plan.max_users,
        'max_products': plan.max_products,
        'max_warehouses': plan.max_warehouses,
        'max_orders_per_month': plan.max_orders_per_month,
        'allow_csv': plan.allow_csv,
        'allow_reports': plan.allow_reports,
        'allow_audit_log': plan.allow_audit_log,
    }


def ensure_usage_fits(db, org_id: int, plan):
    """A downgrade is only allowed if current usage fits inside the new plan."""
    for resource, field in LIMIT_FIELDS.items():
        limit = getattr(plan, field)
        if limit is not None and count_usage(db, org_id, resource) > limit:
            raise HTTPException(
                status_code=400,
                detail=f'You currently use more {resource} than the {plan.name} plan allows ({limit})',
            )


def period_end(plan, cycle: str):
    if plan.name == 'free':
        return None
    return datetime.now() + timedelta(days=365 if cycle == 'yearly' else 30)


def activate_subscription(db, sub, plan):
    # only one active subscription per organization
    db.query(Subscriptions).filter(
        Subscriptions.organization_id == sub.organization_id,
        Subscriptions.status == 'active',
        Subscriptions.id != sub.id,
    ).update({Subscriptions.status: 'cancelled'})

    sub.status = 'active'
    sub.start_date = datetime.now()
    sub.end_date = period_end(plan, sub.billing_cycle)
    sub.auto_renew = plan.name != 'free'


def confirm_payment(db, payment):
    """Mark a payment as paid and activate the subscription it belongs to."""
    if payment.status == 'paid':
        return
    payment.status = 'paid'
    sub = db.query(Subscriptions).filter(Subscriptions.id == payment.subscription_id).first()
    if sub:
        plan = db.query(Plans).filter(Plans.id == sub.plan_id).first()
        activate_subscription(db, sub, plan)


# ---------------------------------------------------------------
# Plans & current subscription
# ---------------------------------------------------------------
@router.get('/plans')
def list_plans(user: user_dependency, db: db_dependency):

    seed_plans(db)
    db.commit()
    plans = db.query(Plans).filter(Plans.is_active == True).order_by(Plans.price_monthly).all()
    return [plan_to_dict(p) for p in plans]


@router.get('/subscription')
def get_subscription(member: Annotated[dict, Depends(permission('subscription:read'))], db: db_dependency):

    org_id = member['organization_id']
    sub = get_org_subscription(db, org_id)
    plan = get_org_plan(db, org_id)

    usage = {}
    for resource, field in LIMIT_FIELDS.items():
        usage[resource] = {'used': count_usage(db, org_id, resource), 'limit': getattr(plan, field)}

    return {
        'plan': plan_to_dict(plan),
        'subscription': None if sub is None else {
            'id': sub.id,
            'status': sub.status,
            'billing_cycle': sub.billing_cycle,
            'start_date': sub.start_date,
            'end_date': sub.end_date,
            'auto_renew': sub.auto_renew,
        },
        'usage': usage,
    }


# ---------------------------------------------------------------
# Checkout / payments
# ---------------------------------------------------------------
@router.post('/subscription/checkout')
def checkout(member: Annotated[dict, Depends(permission('subscription:read'))], db: db_dependency, body: CheckoutRequest):

    org_id = member['organization_id']

    plan = db.query(Plans).filter(Plans.id == body.plan_id, Plans.is_active == True).first()
    if plan is None:
        raise HTTPException(status_code=404, detail='Plan Not Found')

    current = get_org_plan(db, org_id)
    if plan.id == current.id and plan.name != 'free':
        raise HTTPException(status_code=400, detail='You are already on this plan')

    ensure_usage_fits(db, org_id, plan)

    # old unpaid checkouts are dropped
    db.query(Subscriptions).filter(
        Subscriptions.organization_id == org_id, Subscriptions.status == 'pending'
    ).update({Subscriptions.status: 'cancelled'})

    sub = Subscriptions(organization_id=org_id, plan_id=plan.id, status='pending',
                        billing_cycle=body.billing_cycle)
    db.add(sub)
    db.flush()

    # Free plan: nothing to pay
    if plan.name == 'free':
        activate_subscription(db, sub, plan)
        db.commit()
        return JSONResponse(status_code=200, content={'message': 'Switched to the Free plan'})

    amount = plan.price_yearly if body.billing_cycle == 'yearly' else plan.price_monthly
    payment = Payments(
        organization_id=org_id,
        subscription_id=sub.id,
        amount=amount,
        provider=PAYMENT_PROVIDER,
        provider_ref=f'pay_{secrets.token_hex(12)}',
        status='pending',
    )
    db.add(payment)
    db.commit()

    # TODO: create a real checkout session with your provider here (Stripe / SSLCommerz / bKash)
    #       and return its URL as `checkout_url`. The provider then calls /payments/webhook.
    return JSONResponse(status_code=201, content={
        'message': 'Checkout created. Complete the payment to activate the plan',
        'payment_id': payment.id,
        'provider': payment.provider,
        'provider_ref': payment.provider_ref,
        'amount': float(amount),
        'checkout_url': None,
    })


@router.post('/subscription/payments/{payment_id}/confirm')
def confirm_payment_test(member: Annotated[dict, Depends(permission('subscription:read'))], db: db_dependency, payment_id: int):
    # TEST ONLY: simulates a successful payment. Disabled when DEBUG = False.
    if not DEBUG:
        raise HTTPException(status_code=404, detail='Not Found')

    payment = db.query(Payments).filter(
        Payments.id == payment_id, Payments.organization_id == member['organization_id']
    ).first()
    if payment is None:
        raise HTTPException(status_code=404, detail='Payment Not Found')

    confirm_payment(db, payment)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Payment confirmed, plan activated'})


@router.post('/payments/webhook')
def payment_webhook(db: db_dependency, body: WebhookBody, x_webhook_secret: Optional[str] = Header(default=None)):
    # Called by the payment provider (no login). Protected by a shared secret.
    # TODO: for Stripe etc. verify the provider's own signature header instead.
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail='Webhook is not configured')
    if not x_webhook_secret or not secrets.compare_digest(x_webhook_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail='Invalid webhook secret')

    payment = db.query(Payments).filter(Payments.provider_ref == body.provider_ref).first()
    if payment is None:
        raise HTTPException(status_code=404, detail='Payment Not Found')

    if body.status == 'paid':
        confirm_payment(db, payment)
    elif payment.status == 'pending':
        payment.status = 'failed'

    db.commit()
    return {'message': 'ok'}


@router.get('/subscription/payments')
def payment_history(
    member: Annotated[dict, Depends(permission('subscription:read'))],
    db: db_dependency,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    query = db.query(Payments).filter(Payments.organization_id == member['organization_id'])
    total = query.count()
    items = query.order_by(Payments.id.desc()).offset((page - 1) * page_size).limit(page_size).all()

    return {
        'items': [
            {c.name: getattr(p, c.name) for c in p.__table__.columns} for p in items
        ],
        'total': total,
        'page': page,
        'page_size': page_size,
    }


@router.post('/subscription/cancel')
def cancel_subscription(member: Annotated[dict, Depends(permission('subscription:read'))], db: db_dependency):

    sub = get_org_subscription(db, member['organization_id'])
    if sub is None:
        raise HTTPException(status_code=400, detail='No paid subscription to cancel')

    # stays active until end_date, then falls back to the Free plan
    sub.auto_renew = False
    db.commit()
    return JSONResponse(status_code=200, content={
        'message': 'Auto-renew turned off. The plan stays active until it expires',
    })