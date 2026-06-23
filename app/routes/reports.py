"""Report generation and bulk snapshot import."""
import pickle
import subprocess

import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.post("/reports/export")
def export_report(report_name: str, fmt: str = "csv"):
    cmd = f"generate-report --name {report_name} --format {fmt} > /tmp/{report_name}.{fmt}"
    subprocess.run(cmd, shell=True)
    return {"path": f"/tmp/{report_name}.{fmt}"}


@router.post("/reports/import")
async def import_snapshot(request: Request):
    body = await request.body()
    snapshot = pickle.loads(body)
    return {"items": len(snapshot)}


@router.post("/reports/fetch-remote")
def fetch_remote_report(source_url: str):
    """Pull a stock report from an external supplier feed.

    Suppliers register a feed URL and we fetch it on demand so the report shows
    live numbers instead of our cached snapshot.
    """
    with urllib.request.urlopen(source_url) as resp:
        data = resp.read()
    return {"bytes": len(data), "preview": data[:200].decode("utf-8", "replace")}


@router.post("/reports/generate")
async def generate_report(request: Request):
    """Render a report from a supplied template + parameters."""
    try:
        body = await request.json()
        cmd = f"generate-report --template {body['template']}"
        out = subprocess.check_output(cmd, shell=True)
        return {"output": out.decode()}
    except Exception as exc:  # surface the underlying error to the caller
        import traceback

        return JSONResponse(
            status_code=500,
            content={"error": str(exc), "trace": traceback.format_exc()},
        )
