from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from cjob.config import get_settings
from cjob.db import get_session

from .auth import get_namespace
from .schemas import (
    CancelRequest,
    CancelResponse,
    CliVersionResponse,
    DeleteRequest,
    DeleteResponse,
    JobDetailResponse,
    JobListResponse,
    JobSubmitRequest,
    JobSubmitResponse,
    ResetErrorResponse,
    ResetResponse,
    UsageResponse,
)
from .services import (
    cancel_bulk,
    cancel_single,
    delete_jobs,
    get_job,
    get_usage,
    list_jobs,
    reset,
    submit_job,
)

router = APIRouter(prefix="/v1")


def _read_latest_version(cli_dir: str) -> str:
    latest_file = Path(cli_dir) / "latest"
    if not latest_file.is_file():
        raise HTTPException(status_code=404, detail="CLI binary not found")
    return latest_file.read_text().strip()


@router.get("/cli/version", response_model=CliVersionResponse)
def get_cli_version():
    settings = get_settings()
    version = _read_latest_version(settings.CLI_BINARY_DIR)
    return CliVersionResponse(version=version)


@router.get("/cli/download")
def download_cli_binary():
    settings = get_settings()
    version = _read_latest_version(settings.CLI_BINARY_DIR)
    binary_path = Path(settings.CLI_BINARY_DIR) / version / "cjob"
    if not binary_path.is_file():
        raise HTTPException(status_code=404, detail="CLI binary not found")
    return FileResponse(
        path=str(binary_path),
        media_type="application/octet-stream",
        filename="cjob",
    )


@router.post("/jobs", response_model=JobSubmitResponse, status_code=201)
def post_job(
    req: JobSubmitRequest,
    namespace: str = Depends(get_namespace),
    session: Session = Depends(get_session),
):
    return submit_job(session, namespace, req)


@router.get("/jobs", response_model=JobListResponse)
def get_jobs(
    status: str | None = None,
    limit: int | None = None,
    order: str = "asc",
    namespace: str = Depends(get_namespace),
    session: Session = Depends(get_session),
):
    return list_jobs(session, namespace, status=status, limit=limit, order=order)


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
def get_job_detail(
    job_id: int,
    namespace: str = Depends(get_namespace),
    session: Session = Depends(get_session),
):
    result = get_job(session, namespace, job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return result


@router.post("/jobs/{job_id}/cancel")
def post_cancel_single(
    job_id: int,
    namespace: str = Depends(get_namespace),
    session: Session = Depends(get_session),
):
    result = cancel_single(session, namespace, job_id)
    if result.get("not_found"):
        raise HTTPException(status_code=404, detail="Job not found")
    if result.get("skipped"):
        return {"job_id": job_id, "status": result["status"]}
    return result


@router.post("/jobs/cancel", response_model=CancelResponse)
def post_cancel_bulk(
    req: CancelRequest,
    namespace: str = Depends(get_namespace),
    session: Session = Depends(get_session),
):
    return cancel_bulk(session, namespace, req.job_ids)


@router.post("/jobs/delete", response_model=DeleteResponse)
def post_delete(
    req: DeleteRequest,
    namespace: str = Depends(get_namespace),
    session: Session = Depends(get_session),
):
    return delete_jobs(session, namespace, req.job_ids)


@router.get("/usage", response_model=UsageResponse)
def get_usage_endpoint(
    namespace: str = Depends(get_namespace),
    session: Session = Depends(get_session),
):
    return get_usage(session, namespace)


@router.post(
    "/reset",
    responses={
        202: {"model": ResetResponse},
        409: {"model": ResetErrorResponse},
    },
)
def post_reset(
    namespace: str = Depends(get_namespace),
    session: Session = Depends(get_session),
):
    from fastapi.responses import JSONResponse

    status_code, body = reset(session, namespace)
    return JSONResponse(status_code=status_code, content=body)
