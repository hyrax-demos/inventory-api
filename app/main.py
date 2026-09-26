import time

from fastapi import FastAPI, Response

from app.routes import admin, items, reports, sync

app = FastAPI(title="inventory-api")

app.include_router(items.router)
app.include_router(reports.router)
app.include_router(admin.router)
app.include_router(sync.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    return {"version": "1.0.0"}


@app.get("/ping")
def ping(response: Response):
    response.headers["X-Pong"] = "1"
    return {"pong": True, "ts": int(time.time())}
