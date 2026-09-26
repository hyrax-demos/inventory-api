from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import DomainError
from app.routes import admin, items, reports, sync, transfers

app = FastAPI(title="inventory-api")


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """Render service-layer errors in the same ``{"detail": ...}`` envelope
    ``HTTPException`` uses, plus a stable machine-readable ``code``."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code},
    )


app.include_router(items.router)
app.include_router(reports.router)
app.include_router(admin.router)
app.include_router(sync.router)
app.include_router(transfers.router)


@app.get("/health")
def health():
    return {"status": "ok"}
