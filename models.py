from database import Base
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Numeric,
    JSON, UniqueConstraint, Index,
)
from datetime import datetime


# ---------------------------------------------------------------
# NOTE (multi-tenancy):
# Every tenant-owned table has `organization_id`.
# EVERY query on those tables must filter by the current organization_id.
# ---------------------------------------------------------------


# ---------------------------------------------------------------
# 1. Users / Organizations / Roles / Permissions
# ---------------------------------------------------------------
class Users(Base):

    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    username = Column(String, unique=True, index=True)
    firstname = Column(String)
    lastname = Column(String)
    hash_password = Column(String)
    is_active = Column(Boolean, default=True)
    current_organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True)   # for org switching
    created_at = Column(DateTime, default=datetime.now)


class Organizations(Base):

    __tablename__ = 'organizations'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    slug = Column(String, unique=True, index=True)
    description = Column(Text, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    address = Column(String, nullable=True)
    currency = Column(String, default='USD')
    owner_id = Column(Integer, ForeignKey('users.id'))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class Permissions(Base):

    __tablename__ = 'permissions'

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True)       # e.g. product:create, stock:adjust
    description = Column(String, nullable=True)


class Roles(Base):

    __tablename__ = 'roles'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True)   # NULL = system role
    name = Column(String, index=True)                    # owner, admin, manager, staff, viewer
    description = Column(String, nullable=True)
    is_system = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (UniqueConstraint('organization_id', 'name', name='uq_role_org_name'),)


class RolePermissions(Base):

    __tablename__ = 'role_permissions'

    id = Column(Integer, primary_key=True, index=True)
    role_id = Column(Integer, ForeignKey('roles.id'), index=True)
    permission_id = Column(Integer, ForeignKey('permissions.id'), index=True)

    __table_args__ = (UniqueConstraint('role_id', 'permission_id', name='uq_role_permission'),)


class OrganizationMembers(Base):

    __tablename__ = 'organization_members'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    user_id = Column(Integer, ForeignKey('users.id'), index=True)
    role_id = Column(Integer, ForeignKey('roles.id'))
    is_active = Column(Boolean, default=True)
    joined_at = Column(DateTime, default=datetime.now)

    __table_args__ = (UniqueConstraint('organization_id', 'user_id', name='uq_member_org_user'),)


class Invitations(Base):

    __tablename__ = 'invitations'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    email = Column(String, index=True)
    role_id = Column(Integer, ForeignKey('roles.id'))
    token = Column(String, unique=True, index=True)
    invited_by = Column(Integer, ForeignKey('users.id'))
    status = Column(String, default='pending')           # pending / accepted / expired / cancelled
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)


# ---------------------------------------------------------------
# 4. Categories
# ---------------------------------------------------------------
class Categories(Base):

    __tablename__ = 'categories'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    name = Column(String, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (UniqueConstraint('organization_id', 'name', name='uq_category_org_name'),)


# ---------------------------------------------------------------
# 5. Products & Variants
# ---------------------------------------------------------------
class Products(Base):

    __tablename__ = 'products'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    supplier_id = Column(Integer, ForeignKey('suppliers.id'), nullable=True)     # default supplier
    name = Column(String, index=True)
    description = Column(Text, nullable=True)
    sku = Column(String, index=True)
    barcode = Column(String, index=True, nullable=True)
    unit = Column(String, default='pcs')
    cost_price = Column(Numeric(12, 2), default=0)
    selling_price = Column(Numeric(12, 2), default=0)
    reorder_level = Column(Integer, default=0)           # low stock threshold
    has_variants = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (UniqueConstraint('organization_id', 'sku', name='uq_product_org_sku'),)


class ProductVariants(Base):

    __tablename__ = 'product_variants'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    product_id = Column(Integer, ForeignKey('products.id'), index=True)
    name = Column(String)                                # e.g. "Red / XL"
    sku = Column(String, index=True)
    barcode = Column(String, index=True, nullable=True)
    attributes = Column(JSON, nullable=True)             # {"color": "red", "size": "XL"}
    cost_price = Column(Numeric(12, 2), nullable=True)   # NULL = use product price
    selling_price = Column(Numeric(12, 2), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (UniqueConstraint('organization_id', 'sku', name='uq_variant_org_sku'),)


# ---------------------------------------------------------------
# 6. Warehouses
# ---------------------------------------------------------------
class Warehouses(Base):

    __tablename__ = 'warehouses'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    name = Column(String, index=True)
    code = Column(String)
    address = Column(String, nullable=True)
    city = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (UniqueConstraint('organization_id', 'code', name='uq_warehouse_org_code'),)


# ---------------------------------------------------------------
# 7. Inventory (current stock + movement history)
# ---------------------------------------------------------------
class Inventory(Base):

    __tablename__ = 'inventory'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    warehouse_id = Column(Integer, ForeignKey('warehouses.id'), index=True)
    product_id = Column(Integer, ForeignKey('products.id'), index=True)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True)   # NULL = no variant
    quantity = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # NOTE: PostgreSQL treats NULLs as distinct, so check "warehouse + product + variant(NULL)"
    # duplicates in code before inserting.
    __table_args__ = (
        UniqueConstraint('warehouse_id', 'product_id', 'variant_id', name='uq_inventory_item'),
    )


class StockMovements(Base):

    __tablename__ = 'stock_movements'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    warehouse_id = Column(Integer, ForeignKey('warehouses.id'), index=True)
    product_id = Column(Integer, ForeignKey('products.id'), index=True)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True)
    movement_type = Column(String, index=True)       # in / out / adjustment / transfer_in / transfer_out
    quantity = Column(Integer)                       # signed: + adds stock, - removes stock
    unit_cost = Column(Numeric(12, 2), nullable=True)
    quantity_after = Column(Integer, nullable=True)
    reference_type = Column(String, nullable=True)   # purchase_order / sales_order / transfer / manual
    reference_id = Column(Integer, nullable=True)
    note = Column(String, nullable=True)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)


# ---------------------------------------------------------------
# 8. Suppliers / 9. Customers
# ---------------------------------------------------------------
class Suppliers(Base):

    __tablename__ = 'suppliers'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    name = Column(String, index=True)
    contact_person = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    address = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class Customers(Base):

    __tablename__ = 'customers'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    name = Column(String, index=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    address = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


# ---------------------------------------------------------------
# 10. Purchase Orders
# ---------------------------------------------------------------
class PurchaseOrders(Base):

    __tablename__ = 'purchase_orders'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    po_number = Column(String, index=True)
    supplier_id = Column(Integer, ForeignKey('suppliers.id'), index=True)
    warehouse_id = Column(Integer, ForeignKey('warehouses.id'))              # where stock will be received
    status = Column(String, default='draft')         # draft / ordered / received / cancelled
    order_date = Column(DateTime, default=datetime.now)
    expected_date = Column(DateTime, nullable=True)
    received_date = Column(DateTime, nullable=True)
    subtotal = Column(Numeric(14, 2), default=0)
    tax = Column(Numeric(14, 2), default=0)
    total = Column(Numeric(14, 2), default=0)
    note = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey('users.id'))
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (UniqueConstraint('organization_id', 'po_number', name='uq_po_org_number'),)


class PurchaseOrderItems(Base):

    __tablename__ = 'purchase_order_items'

    id = Column(Integer, primary_key=True, index=True)
    purchase_order_id = Column(Integer, ForeignKey('purchase_orders.id'), index=True)
    product_id = Column(Integer, ForeignKey('products.id'))
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True)
    quantity = Column(Integer)
    unit_cost = Column(Numeric(12, 2))
    line_total = Column(Numeric(14, 2))


# ---------------------------------------------------------------
# 11. Sales Orders
# ---------------------------------------------------------------
class SalesOrders(Base):

    __tablename__ = 'sales_orders'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    so_number = Column(String, index=True)
    customer_id = Column(Integer, ForeignKey('customers.id'), index=True)
    warehouse_id = Column(Integer, ForeignKey('warehouses.id'))              # where stock will be taken from
    status = Column(String, default='draft')         # draft / confirmed / completed / cancelled
    order_date = Column(DateTime, default=datetime.now)
    completed_date = Column(DateTime, nullable=True)
    subtotal = Column(Numeric(14, 2), default=0)
    tax = Column(Numeric(14, 2), default=0)
    discount = Column(Numeric(14, 2), default=0)
    total = Column(Numeric(14, 2), default=0)
    note = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey('users.id'))
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (UniqueConstraint('organization_id', 'so_number', name='uq_so_org_number'),)


class SalesOrderItems(Base):

    __tablename__ = 'sales_order_items'

    id = Column(Integer, primary_key=True, index=True)
    sales_order_id = Column(Integer, ForeignKey('sales_orders.id'), index=True)
    product_id = Column(Integer, ForeignKey('products.id'))
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=True)
    quantity = Column(Integer)
    unit_price = Column(Numeric(12, 2))
    unit_cost = Column(Numeric(12, 2), default=0)    # cost snapshot at sale time (for profit & loss)
    line_total = Column(Numeric(14, 2))


# ---------------------------------------------------------------
# 13. Notifications
# ---------------------------------------------------------------
class Notifications(Base):

    __tablename__ = 'notifications'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)   # NULL = whole organization
    type = Column(String, index=True)                # low_stock / new_purchase / new_sale / order_status
    title = Column(String)
    message = Column(Text)
    reference_type = Column(String, nullable=True)
    reference_id = Column(Integer, nullable=True)
    is_read = Column(Boolean, default=False)
    email_sent = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


# ---------------------------------------------------------------
# 15. Audit Logs
# ---------------------------------------------------------------
class AuditLogs(Base):

    __tablename__ = 'audit_logs'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    action = Column(String, index=True)              # create / update / delete / login / stock_in ...
    entity_type = Column(String, index=True)         # product / stock / purchase_order / sales_order ...
    entity_id = Column(Integer, nullable=True)
    old_values = Column(JSON, nullable=True)
    new_values = Column(JSON, nullable=True)
    ip_address = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)


# ---------------------------------------------------------------
# 16. Subscription & SaaS
# ---------------------------------------------------------------
class Plans(Base):

    __tablename__ = 'plans'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True)               # free / pro / business
    price_monthly = Column(Numeric(10, 2), default=0)
    price_yearly = Column(Numeric(10, 2), default=0)
    max_users = Column(Integer, nullable=True)       # NULL = unlimited
    max_products = Column(Integer, nullable=True)
    max_warehouses = Column(Integer, nullable=True)
    max_orders_per_month = Column(Integer, nullable=True)
    allow_csv = Column(Boolean, default=False)
    allow_reports = Column(Boolean, default=False)
    allow_audit_log = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)


class Subscriptions(Base):

    __tablename__ = 'subscriptions'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    plan_id = Column(Integer, ForeignKey('plans.id'))
    status = Column(String, default='active')        # active / expired / cancelled / past_due
    billing_cycle = Column(String, default='monthly')  # monthly / yearly
    start_date = Column(DateTime, default=datetime.now)
    end_date = Column(DateTime, nullable=True)
    auto_renew = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class Payments(Base):

    __tablename__ = 'payments'

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id'), nullable=True)
    amount = Column(Numeric(10, 2))
    currency = Column(String, default='USD')
    provider = Column(String)                        # stripe / sslcommerz / bkash ...
    provider_ref = Column(String, nullable=True, index=True)   # transaction / session id
    status = Column(String, default='pending')       # pending / paid / failed / refunded
    created_at = Column(DateTime, default=datetime.now)