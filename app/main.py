from fastapi import FastAPI

from app import cache, config
from app.routes import admin, items, reports, sync

app = FastAPI(title="inventory-api")

app.include_router(items.router)
app.include_router(reports.router)
app.include_router(admin.router)
app.include_router(sync.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/details")
def health_details():
    return {
        "status": "ok",
        "version": config.VERSION,
        "cache_entries": cache.live_count(),
    }
