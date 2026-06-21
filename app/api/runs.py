"""HTTP transport; all run transitions go through AuditService."""

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Header, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from app.graph.interrupts import ResumeBody
from app.services.review import ReviewError

router = APIRouter()


async def store_upload(file: UploadFile, upload_dir: Path, limit: int) -> Path:
    content = await file.read(limit + 1)
    await file.close()
    if len(content) > limit:
        raise ReviewError("PDF 文件超过大小限制", 413)
    if not content.startswith(b"%PDF-"):
        raise ReviewError("请上传有效的 PDF 文件", 422)
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{uuid4()}.pdf"
    path.write_bytes(content)
    return path


@router.post("/runs", status_code=201)
async def submit_run(request: Request, file: UploadFile):
    settings = request.app.state.settings
    path = await store_upload(file, settings.upload_dir, settings.max_upload_bytes)
    return await run_in_threadpool(request.app.state.audit.submit, path)


@router.get("/runs")
def list_runs(request: Request, status: str | None = None):
    return request.app.state.audit.list(status)


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request):
    return request.app.state.audit.get(run_id)


@router.post("/runs/{run_id}/resume")
def resume_run(
    run_id: str, body: ResumeBody, request: Request, x_user_role: str = Header(default="ap_clerk")
):
    return request.app.state.audit.resume(run_id, body, x_user_role)


@router.post("/runs/{run_id}/documents")
async def document_run(
    run_id: str, request: Request, file: UploadFile, x_user_role: str = Header(default="ap_clerk")
):
    audit = request.app.state.audit
    current = await run_in_threadpool(audit.get, run_id)
    if current["status"] != "WAITING_INFO":
        raise ReviewError("仅等待补材料的审核允许补交发票", 409)
    if x_user_role != "ap_clerk":
        raise ReviewError("补交发票需要 ap_clerk 角色")
    settings = request.app.state.settings
    path = await store_upload(file, settings.upload_dir, settings.max_upload_bytes)
    return await run_in_threadpool(audit.document, run_id, path, x_user_role)


@router.get("/runs/{run_id}/audit")
def get_audit(run_id: str, request: Request):
    request.app.state.audit.get(run_id)
    return request.app.state.repo.audit(run_id)
