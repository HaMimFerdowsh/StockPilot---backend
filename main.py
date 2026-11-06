import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import models
from database import engine, SessionLocal
from router import (
    auth, organizations, users, categories, products, warehouses, inventory,
    suppliers, customers, purchase_orders, sales_orders, reports, notifications,
    csv_io, audit_logs, subscriptions,
)

app = FastAPI(title='StockPilot: Multi-Tenant Inventory & Order Management')


origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
frontend_url = os.getenv('FRONTEND_URL')
if frontend_url:
    origins.append(frontend_url)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


models.Base.metadata.create_all(bind=engine)

app.include_router(auth.router)
app.include_router(organizations.router)
app.include_router(users.router)
app.include_router(categories.router)
app.include_router(products.router)
app.include_router(warehouses.router)
app.include_router(inventory.router)
app.include_router(suppliers.router)
app.include_router(customers.router)
app.include_router(purchase_orders.router)
app.include_router(sales_orders.router)
app.include_router(reports.router)
app.include_router(notifications.router)
app.include_router(csv_io.router)
app.include_router(audit_logs.router)
app.include_router(subscriptions.router)


# Permissions and the Free / Pro / Business plans must exist before the first organization is created
@app.on_event('startup')
def seed_defaults():
    db = SessionLocal()
    try:
        organizations.seed_permissions(db)
        subscriptions.seed_plans(db)
        db.commit()
    finally:
        db.close()


@app.get('/')
def health_check():
    return {'status': 'ok', 'app': 'StockPilot'}