from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_

from models import AuditLogs, Users
from router.auth import db_dependency, permission
from router.organizations import paginate_rows
from router.subscriptions import require_feature
from typing import Annotated
from fastapi import Depends

router = APIRouter()


# ---------------------------------------------------------------
# Helpers (every router uses these)
#
#   log_action(db, member, 'update', 'product', product.id, old=snapshot_before, new=snapshot(product))
#
# It only adds the row. The caller's db.commit() saves it together with the real change.
# ---------------------------------------------------------------
def snapshot(obj):
    """Plain-dict copy of a model row, safe to store in a JSON column."""
    return jsonable_encoder({c.name: getattr(obj, c.name) for c in obj.__table__.columns})


def log_action(db, member, action: str, entity_type: str, entity_id: Optional[int] = None,
               old=None, new=None):
    db.add(AuditLogs(
        organization_id=member['organization_id'],
        user_id=member['id'],
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_values=None if old is None else jsonable_encoder(old),
        new_values=None if new is None else jsonable_encoder(new),
    ))


def _log_item(row):
    log, username = row
    return {
        'id': log.id,
        'user_id': log.user_id,
        'username': username,
        'action': log.action,
        'entity_type': log.entity_type,
        'entity_id': log.entity_id,
        'old_values': log.old_values,
        'new_values': log.new_values,
        'created_at': log.created_at,
    }


def _base_query(db, org_id):
    return db.query(AuditLogs, Users.username).outerjoin(
        Users, Users.id == AuditLogs.user_id
    ).filter(AuditLogs.organization_id == org_id)


# ---------------------------------------------------------------
# Audit logs  (Who changed what and when)
#
# User activity      -> ?user_id=5
# Product history    -> ?entity_type=product&entity_id=12
# Stock history      -> ?entity_type=stock
# Purchase activity  -> ?entity_type=purchase_order
# Sales activity     -> ?entity_type=sales_order
# ---------------------------------------------------------------
@router.get('/audit-logs')
def get_audit_logs(
    member: Annotated[dict, Depends(permission('audit:read'))],
    db: db_dependency,
    search: Optional[str] = None,                        # action / entity type
    user_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    action: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    order: str = Query('desc', pattern='^(asc|desc)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    require_feature(db, member['organization_id'], 'audit')

    query = _base_query(db, member['organization_id'])

    if search:
        like = f'%{search}%'
        query = query.filter(or_(AuditLogs.action.ilike(like), AuditLogs.entity_type.ilike(like)))
    if user_id:
        query = query.filter(AuditLogs.user_id == user_id)
    if entity_type:
        query = query.filter(AuditLogs.entity_type == entity_type)
    if entity_id:
        query = query.filter(AuditLogs.entity_id == entity_id)
    if action:
        query = query.filter(AuditLogs.action == action)
    if from_date:
        query = query.filter(AuditLogs.created_at >= from_date)
    if to_date:
        query = query.filter(AuditLogs.created_at <= to_date)

    query = query.order_by(AuditLogs.created_at.asc() if order == 'asc' else AuditLogs.created_at.desc())

    return paginate_rows(query, page, page_size, _log_item)


@router.get('/audit-logs/{entity_type}/{entity_id}')
def get_entity_history(
    member: Annotated[dict, Depends(permission('audit:read'))],
    db: db_dependency,
    entity_type: str,
    entity_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    require_feature(db, member['organization_id'], 'audit')

    query = _base_query(db, member['organization_id']).filter(
        AuditLogs.entity_type == entity_type, AuditLogs.entity_id == entity_id
    ).order_by(AuditLogs.created_at.desc())

    return paginate_rows(query, page, page_size, _log_item)