"""Shared FastAPI request dependencies."""

from fastapi import Header, HTTPException


def require_tenant(x_tenant_id: str = Header()) -> str:
    """FastAPI dependency: the caller's tenant id from ``X-Tenant-Id``.

    A missing header is rejected by FastAPI (422). A header that is present
    but empty gets a 400 here, so it never reaches a query as ``tenant_id = ''``.
    """
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="missing tenant")
    return x_tenant_id
