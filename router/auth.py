import os
from datetime import timedelta, datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import SessionLocal
from models import (
    Users, Organizations, OrganizationMembers, Roles, Permissions, RolePermissions,
)

router = APIRouter()

bcrypt_context = CryptContext(schemes=['bcrypt'], deprecated='auto')
OAuth2_bearer = OAuth2PasswordBearer(tokenUrl='login')

# Create your own key:  python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = os.getenv('SECRET_KEY', 'dev-only-change-me')
ALGORITHM = 'HS256'

ACCESS_MINUTES = 30
REFRESH_DAYS = 7
RESET_MINUTES = 15

# No email service yet, so the reset token is returned in the response for testing.
# Set to False when you finish.
DEBUG = True


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class CreateUsers(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=30)
    firstname: str = Field(min_length=1, max_length=50)
    lastname: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=8, max_length=64)


class UpdateUser(BaseModel):
    email: Optional[EmailStr] = Field(default=None)
    username: Optional[str] = Field(default=None, min_length=3, max_length=30)
    firstname: Optional[str] = Field(default=None, min_length=1, max_length=50)
    lastname: Optional[str] = Field(default=None, min_length=1, max_length=50)


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPassword(BaseModel):
    email: EmailStr


class ResetPassword(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=64)


class ChangePassword(BaseModel):
    old_password: str = Field(min_length=1, max_length=64)
    new_password: str = Field(min_length=8, max_length=64)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def authenticate_user(login, password, db):
    # login can be username or email
    user = db.query(Users).filter(
        or_(Users.username == login, Users.email == login.lower())
    ).first()
    if user is None:
        return False
    if bcrypt_context.verify(password, user.hash_password):
        return user
    return False


def create_token(username: str, user_id: int, token_type: str, expires_delta: timedelta, extra: Optional[dict] = None):
    encode = {'sub': username, 'id': user_id, 'type': token_type}
    if extra:
        encode.update(extra)
    encode.update({'exp': datetime.now(timezone.utc) + expires_delta})
    return jwt.encode(encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str, expected_type: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        # bad signature or expired
        raise HTTPException(status_code=401, detail='Token invalid or expired')
    if payload.get('type') != expected_type:
        raise HTTPException(status_code=401, detail='Wrong token type')
    return payload


def get_current_user(token: Annotated[str, Depends(OAuth2_bearer)]):
    payload = decode_token(token, 'access')
    username = payload.get('sub')
    user_id = payload.get('id')
    if username is None or user_id is None:
        raise HTTPException(status_code=401, detail='Could not validate user')
    return {'username': username, 'id': user_id}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


db_dependency = Annotated[Session, Depends(get_db)]
user_dependency = Annotated[dict, Depends(get_current_user)]


# ---------------------------------------------------------------
# Multi-tenancy + RBAC dependencies
#
# member_dependency  -> logged in AND has a current organization.
#                       Gives you member['organization_id'] to filter every query.
# permission('code') -> same as above + checks the role has that permission.
#
# Usage in other routers:
#     member: permission('product:create')
#     db.query(Products).filter(Products.organization_id == member['organization_id'])
# ---------------------------------------------------------------
def get_member(user: user_dependency, db: db_dependency):

    current_user = db.query(Users).filter(Users.id == user.get('id')).first()
    if current_user is None or not current_user.is_active:
        raise HTTPException(status_code=401, detail='User not available')

    org_id = current_user.current_organization_id
    if org_id is None:
        raise HTTPException(status_code=400, detail='No organization selected')

    org = db.query(Organizations).filter(Organizations.id == org_id).first()
    if org is None or not org.is_active:
        raise HTTPException(status_code=403, detail='Organization is not available')

    membership = db.query(OrganizationMembers).filter(
        OrganizationMembers.organization_id == org_id,
        OrganizationMembers.user_id == current_user.id,
        OrganizationMembers.is_active == True,
    ).first()
    if membership is None:
        raise HTTPException(status_code=403, detail='You are not an active member of this organization')

    role = db.query(Roles).filter(Roles.id == membership.role_id).first()
    codes = db.query(Permissions.code).join(
        RolePermissions, RolePermissions.permission_id == Permissions.id
    ).filter(RolePermissions.role_id == membership.role_id).all()

    return {
        'id': current_user.id,
        'username': current_user.username,
        'organization_id': org_id,
        'role_id': membership.role_id,
        'role': role.name if role else None,
        'permissions': {c[0] for c in codes},
    }


member_dependency = Annotated[dict, Depends(get_member)]


def require_permission(code: str):
    def checker(member: member_dependency):
        # owner can do everything inside the organization
        if member['role'] != 'owner' and code not in member['permissions']:
            raise HTTPException(status_code=403, detail=f'Permission required: {code}')
        return member
    return checker


def permission(code: str):
    return Annotated[dict, Depends(require_permission(code))]


# ---------------------------------------------------------------
# Signup / Login / Refresh
# ---------------------------------------------------------------
@router.post('/signup')
def create_users(db: db_dependency, new_user: CreateUsers):

    email = new_user.email.lower()

    if db.query(Users).filter(Users.email == email).first():
        raise HTTPException(status_code=400, detail='Email already registered')
    if db.query(Users).filter(Users.username == new_user.username).first():
        raise HTTPException(status_code=400, detail='Username already taken')

    user_model = Users(
        email=email,
        username=new_user.username,
        firstname=new_user.firstname,
        lastname=new_user.lastname,
        hash_password=bcrypt_context.hash(new_user.password),
        is_active=True,
    )

    db.add(user_model)
    db.commit()

    return JSONResponse(status_code=201, content={'message': 'User created successfully'})


@router.post('/login')
def login_user(db: db_dependency, form_data: Annotated[OAuth2PasswordRequestForm, Depends()]):

    user = authenticate_user(form_data.username, form_data.password, db)
    if not user:
        raise HTTPException(status_code=401, detail='Invalid username or password')
    if not user.is_active:
        raise HTTPException(status_code=403, detail='Account is deactivated')

    return {
        'access_token': create_token(user.username, user.id, 'access', timedelta(minutes=ACCESS_MINUTES)),
        'refresh_token': create_token(user.username, user.id, 'refresh', timedelta(days=REFRESH_DAYS)),
        'token_type': 'bearer',
        'current_organization_id': user.current_organization_id,
    }


@router.post('/refresh')
def refresh_access_token(db: db_dependency, body: RefreshRequest):

    payload = decode_token(body.refresh_token, 'refresh')

    user = db.query(Users).filter(Users.id == payload.get('id')).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail='User not available')

    return {
        'access_token': create_token(user.username, user.id, 'access', timedelta(minutes=ACCESS_MINUTES)),
        'token_type': 'bearer',
    }


# ---------------------------------------------------------------
# Forgot / Reset / Change password
# ---------------------------------------------------------------
@router.post('/forgot-password')
def forgot_password(db: db_dependency, body: ForgotPassword):

    user = db.query(Users).filter(Users.email == body.email.lower()).first()
    response = {'message': 'If this email is registered, a reset link has been sent'}

    if user and user.is_active:
        # 'pw' makes the token one-time: once the password changes, the old token stops working
        token = create_token(user.username, user.id, 'reset', timedelta(minutes=RESET_MINUTES),
                             extra={'pw': user.hash_password[-10:]})
        print(f'[RESET TOKEN] {token}')      # TODO: send by email
        if DEBUG:
            response['reset_token'] = token

    return response


@router.post('/reset-password')
def reset_password(db: db_dependency, body: ResetPassword):

    payload = decode_token(body.token, 'reset')

    user = db.query(Users).filter(Users.id == payload.get('id')).first()
    if user is None:
        raise HTTPException(status_code=404, detail='User not found')
    if payload.get('pw') != user.hash_password[-10:]:
        raise HTTPException(status_code=401, detail='Token invalid or expired')

    user.hash_password = bcrypt_context.hash(body.new_password)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Password reset successfully'})


@router.put('/change-password')
def change_password(user: user_dependency, db: db_dependency, body: ChangePassword):

    current_user = db.query(Users).filter(Users.id == user.get('id')).first()
    if current_user is None:
        raise HTTPException(status_code=404, detail='User not found')

    if not bcrypt_context.verify(body.old_password, current_user.hash_password):
        raise HTTPException(status_code=400, detail='Old password is incorrect')

    if body.old_password == body.new_password:
        raise HTTPException(status_code=400, detail='New password must be different from old password')

    current_user.hash_password = bcrypt_context.hash(body.new_password)
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Password changed successfully'})


# ---------------------------------------------------------------
# Profile (read / update / delete)
# ---------------------------------------------------------------
@router.get('/user')
def get_user_details(user: user_dependency, db: db_dependency):

    current_user = db.query(Users).filter(Users.id == user.get('id')).first()
    if current_user is None:
        raise HTTPException(status_code=404, detail='User not found')

    # all organizations this user belongs to (for the organization switcher)
    rows = db.query(Organizations, Roles).join(
        OrganizationMembers, OrganizationMembers.organization_id == Organizations.id
    ).join(
        Roles, Roles.id == OrganizationMembers.role_id
    ).filter(
        OrganizationMembers.user_id == current_user.id,
        OrganizationMembers.is_active == True,
    ).all()

    return {
        'id': current_user.id,
        'email': current_user.email,
        'username': current_user.username,
        'firstname': current_user.firstname,
        'lastname': current_user.lastname,
        'is_active': current_user.is_active,
        'current_organization_id': current_user.current_organization_id,
        'organizations': [
            {'id': org.id, 'name': org.name, 'slug': org.slug, 'role': role.name}
            for org, role in rows
        ],
    }


@router.put('/edituser')
def update_user(user: user_dependency, db: db_dependency, update_user: UpdateUser):

    current_user = db.query(Users).filter(Users.id == user.get('id')).first()
    if current_user is None:
        raise HTTPException(status_code=404, detail='User not found')

    update_data = update_user.model_dump(exclude_unset=True)

    if 'email' in update_data:
        update_data['email'] = update_data['email'].lower()
        if update_data['email'] != current_user.email:
            if db.query(Users).filter(Users.email == update_data['email']).first():
                raise HTTPException(status_code=400, detail='Email already registered')
    if 'username' in update_data and update_data['username'] != current_user.username:
        if db.query(Users).filter(Users.username == update_data['username']).first():
            raise HTTPException(status_code=400, detail='Username already taken')

    for key, value in update_data.items():
        setattr(current_user, key, value)

    db.commit()
    return JSONResponse(status_code=200, content={'message': 'User updated successfully'})


@router.delete('/deleteuser')
def delete_user(user: user_dependency, db: db_dependency):

    current_user = db.query(Users).filter(Users.id == user.get('id')).first()
    if current_user is None:
        raise HTTPException(status_code=404, detail='User not found')

    owns_org = db.query(Organizations).filter(
        Organizations.owner_id == current_user.id, Organizations.is_active == True
    ).first()
    if owns_org:
        raise HTTPException(status_code=400, detail='You own an organization. Transfer ownership or delete it first')

    # Soft delete: orders, stock movements and audit logs still point to this user,
    # so the row stays and the account is just deactivated.
    db.query(OrganizationMembers).filter(OrganizationMembers.user_id == current_user.id).delete()
    current_user.is_active = False
    current_user.current_organization_id = None

    db.commit()
    return JSONResponse(status_code=200, content={'message': 'User deleted successfully'})