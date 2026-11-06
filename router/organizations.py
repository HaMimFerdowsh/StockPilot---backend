import re
import secrets
from datetime import datetime, timedelta
from typing import Optional
from typing import Annotated
from fastapi import Depends
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import or_

from models import (
    Users, Organizations, OrganizationMembers, Invitations, Roles, Permissions,
    RolePermissions, Plans, Subscriptions,
)
from router.auth import db_dependency, user_dependency, member_dependency, permission, DEBUG
from router.subscriptions import check_limit

router = APIRouter()

INVITE_DAYS = 7


# ---------------------------------------------------------------
# Permissions & default roles
# (other routers use these codes:  member: Annotated[dict, Depends(permission('product:create'))
# ---------------------------------------------------------------
def _crud(name):
    return [f'{name}:read', f'{name}:create', f'{name}:update', f'{name}:delete']


ALL_PERMISSIONS = (
    ['organization:read', 'organization:update', 'organization:delete']
    + ['member:read', 'member:invite', 'member:update', 'member:remove']
    + ['role:read', 'role:manage']
    + _crud('user')
    + _crud('category')
    + _crud('product')
    + _crud('warehouse')
    + ['inventory:read', 'inventory:stock_in', 'inventory:stock_out',
       'inventory:adjust', 'inventory:transfer']
    + _crud('supplier')
    + _crud('customer')
    + ['purchase:read', 'purchase:create', 'purchase:update', 'purchase:cancel', 'purchase:receive']
    + ['sales:read', 'sales:create', 'sales:update', 'sales:cancel', 'sales:complete']
    + ['report:read', 'notification:read', 'csv:import', 'csv:export', 'audit:read']
    + ['subscription:read', 'subscription:manage']
)

_MANAGER_EXCLUDE = ('organization:', 'member:', 'role:', 'user:', 'audit:', 'subscription:')

DEFAULT_ROLES = {
    'owner': ALL_PERMISSIONS,
    'admin': [p for p in ALL_PERMISSIONS if p != 'organization:delete'],
    'manager': [p for p in ALL_PERMISSIONS if not p.startswith(_MANAGER_EXCLUDE)] + ['member:read', 'organization:read'],
    'staff': [
        'organization:read', 'category:read', 'product:read', 'warehouse:read',
        'inventory:read', 'inventory:stock_in', 'inventory:stock_out',
        'supplier:read', 'customer:read', 'customer:create', 'customer:update',
        'purchase:read', 'purchase:create', 'sales:read', 'sales:create', 'sales:complete',
        'notification:read',
    ],
    'viewer': [
        'organization:read', 'category:read', 'product:read', 'warehouse:read',
        'inventory:read', 'supplier:read', 'customer:read', 'purchase:read',
        'sales:read', 'report:read', 'notification:read',
    ],
}


def seed_permissions(db):
    existing = {p.code for p in db.query(Permissions).all()}
    for code in ALL_PERMISSIONS:
        if code not in existing:
            db.add(Permissions(code=code, description=code.replace(':', ' ').replace('_', ' ')))
    db.flush()


def create_default_roles(db, organization_id: int):
    seed_permissions(db)
    perm_ids = {p.code: p.id for p in db.query(Permissions).all()}

    roles = {}
    for role_name, codes in DEFAULT_ROLES.items():
        role = Roles(organization_id=organization_id, name=role_name, is_system=True,
                     description=f'Default {role_name} role')
        db.add(role)
        db.flush()
        for code in codes:
            db.add(RolePermissions(role_id=role.id, permission_id=perm_ids[code]))
        roles[role_name] = role
    return roles


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=200)
    currency: str = Field(default='USD', min_length=3, max_length=3)


class OrganizationUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=200)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)


class InviteCreate(BaseModel):
    email: EmailStr
    role_id: int


class InviteAccept(BaseModel):
    token: str


# ---------------------------------------------------------------
# Helpers (other routers import these too)
# ---------------------------------------------------------------
def to_dict(obj):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def paginate(query, page: int, page_size: int):
    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        'items': [to_dict(i) for i in items],
        'total': total,
        'page': page,
        'page_size': page_size,
    }


def apply_sort(query, model, sort_by: str, order: str):
    column = getattr(model, sort_by)
    return query.order_by(column.asc() if order == 'asc' else column.desc())


def paginate_rows(query, page: int, page_size: int, to_item):
    """Like paginate(), but for joined queries. to_item(row) builds the dict for one row."""
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        'items': [to_item(r) for r in rows],
        'total': total,
        'page': page,
        'page_size': page_size,
    }


def make_slug(db, name: str):
    base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'org'
    slug = base
    while db.query(Organizations).filter(Organizations.slug == slug).first():
        slug = f'{base}-{secrets.token_hex(2)}'
    return slug


# ---------------------------------------------------------------
# Organization: create / profile / update / delete / switch
# ---------------------------------------------------------------
@router.post('/organizations')
def create_organization(user: user_dependency, db: db_dependency, new_org: OrganizationCreate):

    data = new_org.model_dump()
    data['currency'] = data['currency'].upper()

    org = Organizations(**data, slug=make_slug(db, new_org.name), owner_id=user.get('id'))
    db.add(org)
    db.flush()

    roles = create_default_roles(db, org.id)

    db.add(OrganizationMembers(organization_id=org.id, user_id=user.get('id'), role_id=roles['owner'].id))

    # every new organization starts on the Free plan (if the plan exists)
    free_plan = db.query(Plans).filter(Plans.name == 'free').first()
    if free_plan:
        db.add(Subscriptions(organization_id=org.id, plan_id=free_plan.id, status='active'))

    current_user = db.query(Users).filter(Users.id == user.get('id')).first()
    current_user.current_organization_id = org.id

    db.commit()

    return JSONResponse(status_code=201, content={
        'message': 'Organization created successfully',
        'organization_id': org.id,
    })


@router.get('/organizations/current')
def get_organization_profile(member: member_dependency, db: db_dependency):

    org = db.query(Organizations).filter(Organizations.id == member['organization_id']).first()

    member_count = db.query(OrganizationMembers).filter(
        OrganizationMembers.organization_id == org.id,
        OrganizationMembers.is_active == True,
    ).count()

    return {
        **to_dict(org),
        'member_count': member_count,
        'my_role': member['role'],
        'my_permissions': sorted(member['permissions']),
    }


@router.put('/organizations/current')
def update_organization(member: Annotated[dict, Depends(permission('organization:update'))], db: db_dependency,
                        update_org: OrganizationUpdate):

    org = db.query(Organizations).filter(Organizations.id == member['organization_id']).first()

    update_data = update_org.model_dump(exclude_unset=True)
    if 'currency' in update_data and update_data['currency']:
        update_data['currency'] = update_data['currency'].upper()

    for key, value in update_data.items():
        setattr(org, key, value)

    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Organization updated successfully'})


@router.delete('/organizations/current')
def delete_organization(member: member_dependency, db: db_dependency):

    if member['role'] != 'owner':
        raise HTTPException(status_code=403, detail='Only the owner can delete the organization')

    org = db.query(Organizations).filter(Organizations.id == member['organization_id']).first()

    # Soft delete: orders, stock history and audit logs must stay intact
    org.is_active = False
    db.query(Users).filter(Users.current_organization_id == org.id).update(
        {Users.current_organization_id: None}
    )

    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Organization deleted successfully'})


@router.post('/organizations/switch/{organization_id}')
def switch_organization(user: user_dependency, db: db_dependency, organization_id: int):

    membership = db.query(OrganizationMembers).join(
        Organizations, Organizations.id == OrganizationMembers.organization_id
    ).filter(
        OrganizationMembers.organization_id == organization_id,
        OrganizationMembers.user_id == user.get('id'),
        OrganizationMembers.is_active == True,
        Organizations.is_active == True,
    ).first()
    if membership is None:
        raise HTTPException(status_code=404, detail='Organization Not Found')

    current_user = db.query(Users).filter(Users.id == user.get('id')).first()
    current_user.current_organization_id = organization_id

    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Organization switched successfully'})


# ---------------------------------------------------------------
# Members
# ---------------------------------------------------------------
@router.get('/organizations/members')
def list_members(
    member: Annotated[dict, Depends(permission('member:read'))],
    db: db_dependency,
    search: Optional[str] = None,                        # name / email / username
    role_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    sort_by: str = Query('joined_at', pattern='^(joined_at|firstname|email)$'),
    order: str = Query('desc', pattern='^(asc|desc)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    M = OrganizationMembers
    query = db.query(M, Users, Roles).join(Users, Users.id == M.user_id).join(
        Roles, Roles.id == M.role_id
    ).filter(M.organization_id == member['organization_id'])

    if search:
        like = f'%{search}%'
        query = query.filter(or_(
            Users.firstname.ilike(like), Users.lastname.ilike(like),
            Users.email.ilike(like), Users.username.ilike(like),
        ))
    if role_id:
        query = query.filter(M.role_id == role_id)
    if is_active is not None:
        query = query.filter(M.is_active == is_active)

    sort_map = {'joined_at': M.joined_at, 'firstname': Users.firstname, 'email': Users.email}
    column = sort_map[sort_by]
    query = query.order_by(column.asc() if order == 'asc' else column.desc())

    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    return {
        'items': [
            {
                'user_id': u.id,
                'email': u.email,
                'username': u.username,
                'firstname': u.firstname,
                'lastname': u.lastname,
                'role_id': r.id,
                'role': r.name,
                'is_active': m.is_active,
                'joined_at': m.joined_at,
            }
            for m, u, r in rows
        ],
        'total': total,
        'page': page,
        'page_size': page_size,
    }


@router.delete('/organizations/members/{user_id}')
def remove_member(member: Annotated[dict, Depends(permission('member:remove'))], db: db_dependency, user_id: int):

    if user_id == member['id']:
        raise HTTPException(status_code=400, detail='You cannot remove yourself. Use leave instead')

    target = db.query(OrganizationMembers).filter(
        OrganizationMembers.organization_id == member['organization_id'],
        OrganizationMembers.user_id == user_id,
    ).first()
    if target is None:
        raise HTTPException(status_code=404, detail='Member Not Found')

    role = db.query(Roles).filter(Roles.id == target.role_id).first()
    if role and role.name == 'owner':
        raise HTTPException(status_code=400, detail='The owner cannot be removed')

    _drop_membership(db, target)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Member removed successfully'})


@router.post('/organizations/leave')
def leave_organization(member: member_dependency, db: db_dependency):

    if member['role'] == 'owner':
        raise HTTPException(status_code=400, detail='The owner cannot leave. Delete the organization instead')

    target = db.query(OrganizationMembers).filter(
        OrganizationMembers.organization_id == member['organization_id'],
        OrganizationMembers.user_id == member['id'],
    ).first()

    _drop_membership(db, target)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'You left the organization'})


def _drop_membership(db, membership):
    """Delete a membership and fix the removed user's current organization."""
    user = db.query(Users).filter(Users.id == membership.user_id).first()
    org_id = membership.organization_id
    db.delete(membership)
    db.flush()

    if user and user.current_organization_id == org_id:
        other = db.query(OrganizationMembers).join(
            Organizations, Organizations.id == OrganizationMembers.organization_id
        ).filter(
            OrganizationMembers.user_id == user.id,
            OrganizationMembers.is_active == True,
            Organizations.is_active == True,
        ).first()
        user.current_organization_id = other.organization_id if other else None


# ---------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------
@router.post('/organizations/invite')
def invite_member(member: Annotated[dict, Depends(permission('member:invite'))], db: db_dependency, new_invite: InviteCreate):

    email = new_invite.email.lower()

    role = db.query(Roles).filter(
        Roles.id == new_invite.role_id, Roles.organization_id == member['organization_id']
    ).first()
    if role is None:
        raise HTTPException(status_code=404, detail='Role Not Found')
    if role.name == 'owner':
        raise HTTPException(status_code=400, detail='You cannot invite someone as owner')

    already_member = db.query(OrganizationMembers).join(
        Users, Users.id == OrganizationMembers.user_id
    ).filter(
        OrganizationMembers.organization_id == member['organization_id'],
        Users.email == email,
    ).first()
    if already_member:
        raise HTTPException(status_code=400, detail='This user is already a member')

    pending = db.query(Invitations).filter(
        Invitations.organization_id == member['organization_id'],
        Invitations.email == email,
        Invitations.status == 'pending',
        Invitations.expires_at > datetime.now(),
    ).first()
    if pending:
        raise HTTPException(status_code=400, detail='A pending invitation already exists for this email')

    token = secrets.token_urlsafe(32)
    db.add(Invitations(
        organization_id=member['organization_id'],
        email=email,
        role_id=role.id,
        token=token,
        invited_by=member['id'],
        expires_at=datetime.now() + timedelta(days=INVITE_DAYS),
    ))
    db.commit()

    print(f'[INVITE TOKEN] {email}: {token}')      # TODO: send by email
    content = {'message': 'Invitation sent successfully'}
    if DEBUG:
        content['invite_token'] = token

    return JSONResponse(status_code=201, content=content)


@router.get('/organizations/invitations')
def list_invitations(
    member: Annotated[dict, Depends(permission('member:read'))],
    db: db_dependency,
    status: Optional[str] = Query(None, pattern='^(pending|accepted|expired|cancelled)$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    query = db.query(Invitations).filter(Invitations.organization_id == member['organization_id'])
    if status:
        query = query.filter(Invitations.status == status)

    return paginate(apply_sort(query, Invitations, 'created_at', 'desc'), page, page_size)


@router.delete('/organizations/invitations/{invitation_id}')
def cancel_invitation(member: Annotated[dict, Depends(permission('member:invite'))], db: db_dependency, invitation_id: int):

    invite = db.query(Invitations).filter(
        Invitations.id == invitation_id,
        Invitations.organization_id == member['organization_id'],
    ).first()
    if invite is None:
        raise HTTPException(status_code=404, detail='Invitation Not Found')
    if invite.status != 'pending':
        raise HTTPException(status_code=400, detail='Only pending invitations can be cancelled')

    invite.status = 'cancelled'
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Invitation cancelled successfully'})


@router.post('/organizations/invitations/accept')
def accept_invitation(user: user_dependency, db: db_dependency, body: InviteAccept):

    invite = db.query(Invitations).filter(Invitations.token == body.token).first()
    if invite is None or invite.status != 'pending':
        raise HTTPException(status_code=404, detail='Invitation Not Found')

    if invite.expires_at < datetime.now():
        invite.status = 'expired'
        db.commit()
        raise HTTPException(status_code=400, detail='Invitation has expired')

    current_user = db.query(Users).filter(Users.id == user.get('id')).first()
    if current_user.email != invite.email:
        raise HTTPException(status_code=403, detail='This invitation was sent to a different email')

    org = db.query(Organizations).filter(Organizations.id == invite.organization_id).first()
    if org is None or not org.is_active:
        raise HTTPException(status_code=404, detail='Organization Not Found')

    existing = db.query(OrganizationMembers).filter(
        OrganizationMembers.organization_id == invite.organization_id,
        OrganizationMembers.user_id == current_user.id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail='You are already a member')

    check_limit(db, invite.organization_id, 'users')      # plan limit (max users)

    db.add(OrganizationMembers(
        organization_id=invite.organization_id, user_id=current_user.id, role_id=invite.role_id,
    ))
    invite.status = 'accepted'
    current_user.current_organization_id = invite.organization_id

    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Invitation accepted successfully'})