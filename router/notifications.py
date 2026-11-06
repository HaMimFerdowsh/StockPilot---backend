import os
import smtplib
from email.message import EmailMessage
from typing import Optional
from typing import Annotated
from fastapi import Depends
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import or_

from models import Notifications, Users, OrganizationMembers, Roles
from router.auth import db_dependency, permission
from router.organizations import paginate_rows

router = APIRouter()

# Email settings (leave SMTP_HOST empty while developing: emails are printed to the console)
SMTP_HOST = os.getenv('SMTP_HOST')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')
MAIL_FROM = os.getenv('MAIL_FROM', 'no-reply@stockpilot.local')

EMAIL_ROLES = ('owner', 'admin')        # organization-wide notifications are emailed to these roles


# ---------------------------------------------------------------
# Helpers (other routers use notify())
#
#   notify(db, org_id, 'low_stock', 'Low stock', 'Pen is low', reference_type='product', reference_id=3)
#
# It only adds the row. The caller's db.commit() saves it.
# ---------------------------------------------------------------
def send_email(to_list, subject: str, body: str):
    if not to_list:
        return False
    if not SMTP_HOST:
        print(f'[EMAIL] to={to_list} subject={subject!r}')       # development mode
        return True

    msg = EmailMessage()
    msg['From'] = MAIL_FROM
    msg['To'] = ', '.join(to_list)
    msg['Subject'] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=5) as server:
            server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        # an email problem must never break stock or order operations
        print(f'[EMAIL ERROR] {e}')
        return False


def _recipients(db, organization_id: int, user_id: Optional[int]):
    if user_id:
        user = db.query(Users).filter(Users.id == user_id).first()
        return [user.email] if user else []

    rows = db.query(Users.email).join(
        OrganizationMembers, OrganizationMembers.user_id == Users.id
    ).join(
        Roles, Roles.id == OrganizationMembers.role_id
    ).filter(
        OrganizationMembers.organization_id == organization_id,
        OrganizationMembers.is_active == True,
        Users.is_active == True,
        Roles.name.in_(EMAIL_ROLES),
    ).all()
    return [r[0] for r in rows]


def notify(db, organization_id: int, type: str, title: str, message: str,
           user_id: Optional[int] = None, reference_type: Optional[str] = None,
           reference_id: Optional[int] = None, email: bool = True):
    notification = Notifications(
        organization_id=organization_id,
        user_id=user_id,
        type=type,
        title=title,
        message=message,
        reference_type=reference_type,
        reference_id=reference_id,
    )
    if email:
        notification.email_sent = send_email(_recipients(db, organization_id, user_id), title, message)
    db.add(notification)
    return notification


def _visible(db, member):
    # my own notifications + organization-wide ones (user_id is NULL)
    return db.query(Notifications).filter(
        Notifications.organization_id == member['organization_id'],
        or_(Notifications.user_id == member['id'], Notifications.user_id.is_(None)),
    )


# ---------------------------------------------------------------
# Notifications
# (types: low_stock, new_purchase, new_sale, order_status)
# ---------------------------------------------------------------
@router.get('/notifications')
def get_notifications(
    member: Annotated[dict, Depends(permission('notification:read'))],
    db: db_dependency,
    type: Optional[str] = None,
    is_read: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    query = _visible(db, member)

    if type:
        query = query.filter(Notifications.type == type)
    if is_read is not None:
        query = query.filter(Notifications.is_read == is_read)

    query = query.order_by(Notifications.created_at.desc())

    return paginate_rows(query, page, page_size, lambda n: {
        c.name: getattr(n, c.name) for c in n.__table__.columns
    })


@router.get('/notifications/unread-count')
def unread_count(member: Annotated[dict, Depends(permission('notification:read'))], db: db_dependency):

    count = _visible(db, member).filter(Notifications.is_read == False).count()
    return {'unread': count}


@router.put('/notifications/read-all')
def mark_all_read(member: Annotated[dict, Depends(permission('notification:read'))], db: db_dependency):

    _visible(db, member).filter(Notifications.is_read == False).update(
        {Notifications.is_read: True}, synchronize_session=False
    )
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'All notifications marked as read'})


@router.put('/notifications/{notification_id}/read')
def mark_read(member: Annotated[dict, Depends(permission('notification:read'))], db: db_dependency, notification_id: int):

    notification = _visible(db, member).filter(Notifications.id == notification_id).first()
    if notification is None:
        raise HTTPException(status_code=404, detail='Notification Not Found')

    notification.is_read = True
    db.commit()
    return JSONResponse(status_code=200, content={'message': 'Notification marked as read'})