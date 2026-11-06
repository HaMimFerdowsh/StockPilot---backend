from typing import Optional, Annotated

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, EmailStr

from models import (
    Users, Organizations, OrganizationMembers, Invitations, Roles, Permissions, RolePermissions,
)
from router.auth import db_dependency, permission, bcrypt_context
from router.organizations import _drop_membership
from router.subscriptions import check_limit

router = APIRouter()


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class UserCreate(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=30)
    firstname: str = Field(min_length=1, max_length=50)
    lastname: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=8, max_length=64)
    role_id: int


class UserUpdate(BaseModel):
    firstname: Optional[str] = Field(default=None, min_length=1, max_length=50)
    lastname: Optional[str] = Field(default=None, min_length=1, max_length=50)


class RoleAssign(BaseModel):
    role_id: int


class StatusUpdate(BaseModel):
    is_active: bool


class RoleCreate(BaseModel):
    name: str = Field(min_length=2, max_length=30)
    description: Optional[str] = Field(default=None, max_length=200)
    permissions: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=30)
    description: Optional[str] = Field(default=None, max_length=200)
    permissions: Optional[list[str]] = None


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def get_org_role(db, member, role_id: int):
    role = db.query(Roles).filter(
        Roles.id == role_id, Roles.organization_id == member['organization_id']
    ).first()
    if role is None:
        raise HTTPException(status_code=404, detail='Role Not Found')
    return role


def get_membership(db, member, user_id: int):
    # tenant isolation: a user is only visible if he is a member of THIS organization
    membership = db.query(OrganizationMembers).filter(
        OrganizationMembers.organization_id == member['organization_id'],
        OrganizationMembers.user_id == user_id,
    ).first()
    if membership is None:
        raise HTTPException(status_code=404, detail='User Not Found')
    return membership


def role_codes(db, role_id: int):
    rows = db.query(Permissions.code).join(
        RolePermissions, RolePermissions.permission_id == Permissions.id
    ).filter(RolePermissions.role_id == role_id).all()
    return {r[0] for r in rows}


def check_no_escalation(member, codes):
    # a non-owner cannot hand out permissions he does not have himself
    if member['role'] != 'owner' and not set(codes) <= member['permissions']:
        raise HTTPException(status_code=403, detail='You cannot grant permissions you do not have')


def role_to_dict(db, role):
    return {
        'id': role.id,
        'name': role.name,
        'description': role.description,
        'is_system': role.is_system,
        'permissions': sorted(role_codes(db, role.id)),
    }


def resolve_permissions(db, codes):
    codes = set(codes)
    rows = db.query(Permissions).filter(Permissions.code.in_(codes)).all() if codes else []
    unknown = codes - {p.code for p in rows}
    if unknown:
        raise HTTPException(status_code=400, detail=f'Unknown permissions: {", ".join(sorted(unknown))}')
    return rows


# ---------------------------------------------------------------
# Users (inside the current organization)
# ---------------------------------------------------------------
@router.post('/users')
def create_user(member: Annotated[dict, Depends(permission('user:create'))], db: db_dependency, new_user: UserCreate):

    email = new_user.email.lower()

    if db.query(Users).filter(Users.email == email).first():
        raise HTTPException(status_code=400, detail='Email already registered. Invite the user instead')
    if db.query(Users).filter(Users.username == new_user.username).first():
        raise HTTPException(status_code=400, detail='Username already taken')

    role = get_org_role(db, member, new_user.role_id)
    if role.name == 'owner':
        raise HTTPException(status_code=400, detail='You cannot create a user as owner')
    check_no_escalation(member, role_codes(db, role.id))

    check_limit(db, member['organization_id'], 'users')

    user_model = Users(
        email=email,
        username=new_user.username,
        firstname=new_user.firstname,
        lastname=new_user.lastname,
        hash_password=bcrypt_context.hash(new_user.password),
        is_active=True,
        current_organization_id=member['organization_id'],
    )
    db.add(user_model)
    db.flush()

    db.add(OrganizationMembers(
        organization_id=member['organization_id'], user_id=user_model.id, role_id=role.id,
    ))
    db.commit()

    return JSONResponse(status_code=201, content={'message': 'User created successfully', 'user_id': user_model.id})


@router.get('/users/{user_id}')
def get_user(member: Annotated[dict, Depends(permission('user:read'))], db: db_dependency, user_id: int):

    membership = get_membership(db, member, user_id)
    user = db.query(Users).filter(Users.id == user_id).first()
    role = db.query(Roles).filter(Roles.id == membership.role_id).first()

    return {
        'id': user.id,
        'email': user.email,
        'username': user.username,
        'firstname': user.firstname,
        'lastname': user.lastname,
        'role_id': role.id,
        'role': role.name,
        'is_active': membership.is_active,
        'joined_at': membership.joined_at,
    }


@router.put('/users/{user_id}')
def update_user(member: Annotated[dict, Depends(permission('user:update'))], db: db_dependency, update_user: UserUpdate, user_id: int):

    get_membership(db, member, user_id)
    user = db.query(Users).filter(Users.id == user_id).first()

    # email / username / password belong to the user himself (/edituser, /change-password),
    # because one account can be a member of many organizations
    update_data = update_user.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(user, key, value)

    db.commit()
    return JSONResponse(status_code=200, content={'message': 'User updated successfully'})


@router.delete('/users/{user_id}')
def delete_user(member: Annotated[dict, Depends(permission('user:delete'))], db: db_dependency, user_id: int):

    if user_id == member['id']:
        raise HTTPException(status_code=400, detail='You cannot delete yourself')

    membership = get_membership(db, member, user_id)
    role = db.query(Roles).filter(Roles.id == membership.role_id).first()
    if role.name == 'owner':
        raise HTTPException(status_code=400, detail='The owner cannot be deleted')

    _drop_membership(db, membership)

    # if the user belongs to no other organization, the account is deactivated too
    still_member = db.query(OrganizationMembers).filter(OrganizationMembers.user_id == user_id).first()
    if still_member is None:
        db.query(Users).filter(Users.id == user_id).update({Users.is_active: False})

    db.commit()
    return JSONResponse(status_code=200, content={'message': 'User deleted successfully'})


@router.put('/users/{user_id}/role')
def assign_role(member: Annotated[dict, Depends(permission('member:update'))], db: db_dependency, body: RoleAssign, user_id: int):

    if user_id == member['id']:
        raise HTTPException(status_code=400, detail='You cannot change your own role')

    membership = get_membership(db, member, user_id)

    current_role = db.query(Roles).filter(Roles.id == membership.role_id).first()
    if current_role.name == 'owner':
        raise HTTPException(status_code=400, detail="The owner's role cannot be changed")

    new_role = get_org_role(db, member, body.role_id)
    if new_role.name == 'owner':
        raise HTTPException(status_code=400, detail='You cannot assign the owner role')
    check_no_escalation(member, role_codes(db, new_role.id))

    membership.role_id = new_role.id
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Role assigned successfully'})


@router.put('/users/{user_id}/status')
def set_user_status(member: Annotated[dict, Depends(permission('member:update'))], db: db_dependency, body: StatusUpdate, user_id: int):

    if user_id == member['id']:
        raise HTTPException(status_code=400, detail='You cannot change your own status')

    membership = get_membership(db, member, user_id)

    role = db.query(Roles).filter(Roles.id == membership.role_id).first()
    if role.name == 'owner':
        raise HTTPException(status_code=400, detail='The owner cannot be deactivated')

    # only this organization membership is switched; the account itself stays active
    membership.is_active = body.is_active
    db.commit()

    word = 'activated' if body.is_active else 'deactivated'
    return JSONResponse(status_code=200, content={'message': f'User {word} successfully'})


# ---------------------------------------------------------------
# Permissions & Roles
# ---------------------------------------------------------------
@router.get('/permissions')
def list_permissions(member: Annotated[dict, Depends(permission('role:read'))], db: db_dependency):

    rows = db.query(Permissions).order_by(Permissions.code).all()
    return [{'id': p.id, 'code': p.code, 'description': p.description} for p in rows]


@router.get('/roles')
def list_roles(member: Annotated[dict, Depends(permission('role:read'))], db: db_dependency):

    roles = db.query(Roles).filter(Roles.organization_id == member['organization_id']).order_by(Roles.id).all()
    return [role_to_dict(db, r) for r in roles]


@router.get('/roles/{role_id}')
def get_role(member: Annotated[dict, Depends(permission('role:read'))], db: db_dependency, role_id: int):

    return role_to_dict(db, get_org_role(db, member, role_id))


@router.post('/roles')
def create_role(member: Annotated[dict, Depends(permission('role:manage'))], db: db_dependency, new_role: RoleCreate):

    name = new_role.name.strip().lower()

    if db.query(Roles).filter(Roles.organization_id == member['organization_id'], Roles.name == name).first():
        raise HTTPException(status_code=400, detail='Role name already exists')

    perms = resolve_permissions(db, new_role.permissions)
    check_no_escalation(member, new_role.permissions)

    role = Roles(organization_id=member['organization_id'], name=name,
                 description=new_role.description, is_system=False)
    db.add(role)
    db.flush()

    for p in perms:
        db.add(RolePermissions(role_id=role.id, permission_id=p.id))
    db.commit()

    return JSONResponse(status_code=201, content={'message': 'Role created successfully', 'role_id': role.id})


@router.put('/roles/{role_id}')
def update_role(member: Annotated[dict, Depends(permission('role:manage'))], db: db_dependency, update_role: RoleUpdate, role_id: int):

    role = get_org_role(db, member, role_id)
    if role.is_system:
        raise HTTPException(status_code=400, detail='System roles cannot be modified')

    update_data = update_role.model_dump(exclude_unset=True)

    if update_data.get('name'):
        name = update_data['name'].strip().lower()
        if name != role.name and db.query(Roles).filter(
            Roles.organization_id == member['organization_id'], Roles.name == name
        ).first():
            raise HTTPException(status_code=400, detail='Role name already exists')
        role.name = name

    if 'description' in update_data:
        role.description = update_data['description']

    if update_data.get('permissions') is not None:
        perms = resolve_permissions(db, update_data['permissions'])
        check_no_escalation(member, update_data['permissions'])
        db.query(RolePermissions).filter(RolePermissions.role_id == role.id).delete()
        for p in perms:
            db.add(RolePermissions(role_id=role.id, permission_id=p.id))

    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Role updated successfully'})


@router.delete('/roles/{role_id}')
def delete_role(member: Annotated[dict, Depends(permission('role:manage'))], db: db_dependency, role_id: int):

    role = get_org_role(db, member, role_id)
    if role.is_system:
        raise HTTPException(status_code=400, detail='System roles cannot be deleted')

    in_use = (
        db.query(OrganizationMembers).filter(OrganizationMembers.role_id == role_id).first()
        or db.query(Invitations).filter(Invitations.role_id == role_id, Invitations.status == 'pending').first()
    )
    if in_use:
        raise HTTPException(status_code=400, detail='Role is in use by members or invitations')

    db.query(RolePermissions).filter(RolePermissions.role_id == role_id).delete()
    db.delete(role)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Role deleted successfully'})