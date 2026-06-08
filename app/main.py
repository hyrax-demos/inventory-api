from fastapi import FastAPI

from app.routes import admin, items, reports

app = FastAPI(title="inventory-api")

app.include_router(items.router)
app.include_router(reports.router)
app.include_router(admin.router)


@app.get("/health")
def health():
    return {"status": "ok"}
