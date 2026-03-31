from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from packaging.version import InvalidVersion, Version
from sqlalchemy.orm import Session

from cjob.config import get_settings
from cjob.db import get_session

from .auth import UserInfo, get_namespace, get_user_info
from .schemas import (
    CancelRequest,
    CancelResponse,
    SingleCancelResponse,
    CliVersionResponse,
    CliVersionsResponse,
    DeleteRequest,
    DeleteResponse,
    FlavorListResponse,
    JobDetailResponse,
    JobListResponse,
    JobSubmitRequest,
    JobSubmitResponse,
    ResetErrorResponse,
    ResetResponse,
    SweepSubmitRequest,
    UsageResponse,
)
from .services import (
    cancel_bulk,
    cancel_single,
    delete_jobs,
    get_job,
    get_usage,
    list_flavors,
    list_jobs,
    reset,
    submit_job,
    submit_sweep,
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


@router.get("/cli/versions", response_model=CliVersionsResponse)
def get_cli_versions():
    settings = get_settings()
    cli_dir = Path(settings.CLI_BINARY_DIR)
    latest = _read_latest_version(settings.CLI_BINARY_DIR)

    versions = []
    for entry in cli_dir.iterdir():
        if entry.name == "latest" or not entry.is_dir():
            continue
        try:
            Version(entry.name)
        except InvalidVersion:
            continue
        versions.append(entry.name)

    versions.sort(key=Version, reverse=True)
    return CliVersionsResponse(versions=versions, latest=latest)


@router.get("/cli/download")
def download_cli_binary(version: str | None = Query(default=None)):
    settings = get_settings()
    if version is None:
        version = _read_latest_version(settings.CLI_BINARY_DIR)
    binary_path = Path(settings.CLI_BINARY_DIR) / version / "cjob"
    if not binary_path.is_file():
        raise HTTPException(status_code=404, detail="CLI binary not found")
    return FileResponse(
        path=str(binary_path),
        media_type="application/octet-stream",
        filename="cjob",
    )


@router.get("/flavors", response_model=FlavorListResponse)
def get_flavors(
    session: Session = Depends(get_session),
):
    return list_flavors(session)


@router.post("/sweep", response_model=JobSubmitResponse, status_code=201)
def post_sweep(
    req: SweepSubmitRequest,
    user_info: UserInfo = Depends(get_user_info),
    session: Session = Depends(get_session),
):
    return submit_sweep(session, user_info.namespace, user_info.username, req)


@router.post("/jobs", response_model=JobSubmitResponse, status_code=201)
def post_job(
    req: JobSubmitRequest,
    user_info: UserInfo = Depends(get_user_info),
    session: Session = Depends(get_session),
):
    return submit_job(session, user_info.namespace, user_info.username, req)


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


@router.post("/jobs/{job_id}/cancel", response_model=SingleCancelResponse)
def post_cancel_single(
    job_id: int,
    namespace: str = Depends(get_namespace),
    session: Session = Depends(get_session),
):
    result = cancel_single(session, namespace, job_id)
    if result.get("not_found"):
        raise HTTPException(status_code=404, detail="Job not found")
    return SingleCancelResponse(job_id=job_id, status=result["status"])


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
