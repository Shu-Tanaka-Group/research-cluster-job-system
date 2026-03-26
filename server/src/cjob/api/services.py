import logging

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from cjob.config import get_settings
from cjob.models import Job, JobEvent, UserJobCounter

from .schemas import (
    CancelResponse,
    DeleteResponse,
    JobDetailResponse,
    JobListResponse,
    JobSubmitRequest,
    JobSubmitResponse,
    JobSummary,
    SkippedItem,
)

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}
ACTIVE_STATUSES = {"QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING"}
CANCELLABLE_STATUSES = {"QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING"}
DELETABLE_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}
# Statuses counted toward MAX_QUEUED_JOBS_PER_NAMESPACE
COUNTED_STATUSES = {"QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING", "CANCELLED"}


def allocate_job_id(session: Session, namespace: str) -> int:
    result = session.execute(
        text(
            "INSERT INTO user_job_counters (namespace, next_id) "
            "VALUES (:namespace, 2) "
            "ON CONFLICT (namespace) DO UPDATE "
            "SET next_id = user_job_counters.next_id + 1 "
            "RETURNING next_id - 1"
        ),
        {"namespace": namespace},
    )
    return result.scalar_one()


def submit_job(
    session: Session, namespace: str, req: JobSubmitRequest
) -> JobSubmitResponse:
    settings = get_settings()

    # Check for DELETING jobs (reset in progress)
    deleting_count = (
        session.query(func.count())
        .select_from(Job)
        .filter(Job.namespace == namespace, Job.status == "DELETING")
        .scalar()
    )
    if deleting_count > 0:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409,
            detail="リセット処理中のためジョブを投入できません。しばらく待ってから再試行してください",
        )

    # Check job count limit
    job_count = (
        session.query(func.count())
        .select_from(Job)
        .filter(Job.namespace == namespace, Job.status.in_(COUNTED_STATUSES))
        .scalar()
    )
    if job_count >= settings.MAX_QUEUED_JOBS_PER_NAMESPACE:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=429,
            detail=f"投入可能なジョブ数の上限（{settings.MAX_QUEUED_JOBS_PER_NAMESPACE}件）に達しています",
        )

    # Reject GPU jobs
    if req.resources.gpu > 0:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail="GPU ジョブは現在サポートされていません",
        )

    # Resolve time_limit_seconds
    time_limit = req.time_limit_seconds if req.time_limit_seconds is not None else settings.DEFAULT_TIME_LIMIT_SECONDS
    if time_limit > settings.MAX_TIME_LIMIT_SECONDS:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail=f"time_limit_seconds は {settings.MAX_TIME_LIMIT_SECONDS} 秒（7日）以下で指定してください",
        )
    if time_limit <= 0:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail="time_limit_seconds は 1 以上で指定してください",
        )

    # Extract username from namespace
    user = namespace.removeprefix(settings.JOB_NAMESPACE_PREFIX)

    # Allocate job_id
    job_id = allocate_job_id(session, namespace)

    # Compute log_dir
    log_dir = f"{settings.LOG_BASE_DIR}/{job_id}"

    # Insert job
    job = Job(
        namespace=namespace,
        job_id=job_id,
        user=user,
        image=req.image,
        command=req.command,
        cwd=req.cwd,
        env_json=req.env,
        cpu=req.resources.cpu,
        memory=req.resources.memory,
        gpu=req.resources.gpu,
        time_limit_seconds=time_limit,
        status="QUEUED",
        log_dir=log_dir,
    )
    session.add(job)

    # Record event
    event = JobEvent(
        namespace=namespace,
        job_id=job_id,
        event_type="SUBMITTED",
    )
    session.add(event)

    session.flush()

    return JobSubmitResponse(job_id=job_id, status="QUEUED")


def list_jobs(
    session: Session,
    namespace: str,
    status: str | None = None,
    limit: int | None = None,
    order: str = "asc",
) -> JobListResponse:
    base_query = session.query(Job).filter(Job.namespace == namespace)
    if status:
        base_query = base_query.filter(Job.status == status)

    total_count = base_query.count()

    # Always fetch newest first; limit selects the newest N jobs
    query = base_query.order_by(Job.job_id.desc())
    if limit:
        query = query.limit(limit)

    rows = query.all()

    # Sort for display
    if order != "desc":
        rows.sort(key=lambda j: j.job_id)

    jobs = [
        JobSummary(
            job_id=j.job_id,
            status=j.status,
            command=j.command,
            created_at=j.created_at,
            finished_at=j.finished_at,
        )
        for j in rows
    ]
    return JobListResponse(jobs=jobs, total_count=total_count)


def get_job(
    session: Session, namespace: str, job_id: int
) -> JobDetailResponse | None:
    job = session.get(Job, (namespace, job_id))
    if job is None:
        return None

    return JobDetailResponse(
        job_id=job.job_id,
        status=job.status,
        namespace=job.namespace,
        command=job.command,
        cwd=job.cwd,
        time_limit_seconds=job.time_limit_seconds,
        k8s_job_name=job.k8s_job_name,
        log_dir=job.log_dir,
        created_at=job.created_at,
        dispatched_at=job.dispatched_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def cancel_single(
    session: Session, namespace: str, job_id: int
) -> dict:
    job = session.get(Job, (namespace, job_id))
    if job is None:
        return {"not_found": True}

    if job.status in CANCELLABLE_STATUSES:
        job.status = "CANCELLED"
        session.add(
            JobEvent(namespace=namespace, job_id=job_id, event_type="CANCELLED")
        )
        session.flush()
        return {"job_id": job_id, "status": "CANCELLED"}

    return {"job_id": job_id, "status": job.status, "skipped": True}


def cancel_bulk(
    session: Session, namespace: str, job_ids: list[int]
) -> CancelResponse:
    cancelled = []
    skipped = []
    not_found = []

    for jid in job_ids:
        result = cancel_single(session, namespace, jid)
        if result.get("not_found"):
            not_found.append(jid)
        elif result.get("skipped"):
            skipped.append(jid)
        else:
            cancelled.append(jid)

    return CancelResponse(cancelled=cancelled, skipped=skipped, not_found=not_found)


def delete_jobs(
    session: Session, namespace: str, job_ids: list[int] | None
) -> DeleteResponse:
    deleted = []
    skipped = []
    not_found = []

    if job_ids is None:
        # Delete all completed jobs in namespace
        jobs = (
            session.query(Job)
            .filter(Job.namespace == namespace)
            .all()
        )
        targets = [(j.job_id, j.status) for j in jobs]
    else:
        targets = []
        for jid in job_ids:
            job = session.get(Job, (namespace, jid))
            if job is None:
                not_found.append(jid)
            else:
                targets.append((job.job_id, job.status))

    for jid, status in targets:
        if status in DELETABLE_STATUSES:
            session.query(Job).filter(
                Job.namespace == namespace, Job.job_id == jid
            ).delete()
            deleted.append(jid)
        elif status == "DELETING":
            skipped.append(SkippedItem(job_id=jid, reason="deleting"))
        elif status in ACTIVE_STATUSES:
            skipped.append(SkippedItem(job_id=jid, reason="running"))

    return DeleteResponse(deleted=deleted, skipped=skipped, not_found=not_found)


def reset(session: Session, namespace: str) -> tuple[int, dict]:
    # Check for DELETING jobs (previous reset still in progress)
    deleting_count = (
        session.query(func.count())
        .select_from(Job)
        .filter(Job.namespace == namespace, Job.status == "DELETING")
        .scalar()
    )
    if deleting_count > 0:
        return 409, {
            "message": "リセット処理が進行中のため再実行できません。しばらく待ってから再試行してください"
        }

    # Check for active jobs
    active_jobs = (
        session.query(Job.job_id)
        .filter(Job.namespace == namespace, Job.status.in_(ACTIVE_STATUSES))
        .all()
    )
    if active_jobs:
        return 409, {
            "message": "完了していないジョブがあるためリセットできません",
            "blocking_job_ids": [j.job_id for j in active_jobs],
        }

    # Mark all jobs as DELETING
    session.query(Job).filter(
        Job.namespace == namespace,
        Job.status.in_(TERMINAL_STATUSES),
    ).update({"status": "DELETING"}, synchronize_session="fetch")

    return 202, {"status": "accepted"}
