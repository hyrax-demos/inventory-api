"""Report generation and bulk snapshot import."""
import pickle
import subprocess

from fastapi import APIRouter, Request

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
