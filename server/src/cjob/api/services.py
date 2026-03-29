import logging

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from cjob.config import get_settings
from cjob.models import Job, JobEvent, UserJobCounter
from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib

from .schemas import (
    CancelResponse,
    DailyUsage,
    DeleteResponse,
    JobDetailResponse,
    JobListResponse,
    JobSubmitRequest,
    JobSubmitResponse,
    JobSummary,
    ResourceSpec,
    SkippedItem,
    SweepSubmitRequest,
    UsageResponse,
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


def _validate_common(
    session: Session, namespace: str, resources: ResourceSpec,
    time_limit_seconds: int | None,
) -> int:
    """Shared validation for submit_job and submit_sweep. Returns resolved time_limit."""
    from fastapi import HTTPException

    settings = get_settings()

    # Check for DELETING jobs (reset in progress)
    deleting_count = (
        session.query(func.count())
        .select_from(Job)
        .filter(Job.namespace == namespace, Job.status == "DELETING")
        .scalar()
    )
    if deleting_count > 0:
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
        raise HTTPException(
            status_code=429,
            detail=f"投入可能なジョブ数の上限（{settings.MAX_QUEUED_JOBS_PER_NAMESPACE}件）に達しています",
        )

    # Check resource exceeds max node allocatable
    max_resources = session.execute(
        text(
            "SELECT MAX(cpu_millicores) AS max_cpu, "
            "       MAX(memory_mib) AS max_memory, "
            "       MAX(gpu) AS max_gpu "
            "FROM node_resources"
        )
    ).mappings().first()

    if max_resources and max_resources["max_cpu"] is not None:
        req_cpu = parse_cpu_millicores(resources.cpu)
        req_mem = parse_memory_mib(resources.memory)

        if req_cpu > max_resources["max_cpu"]:
            raise HTTPException(
                status_code=400,
                detail=f"要求 CPU ({resources.cpu}) がクラスタ内の最大ノード "
                       f"({max_resources['max_cpu']}m) を超えています",
            )
        if req_mem > max_resources["max_memory"]:
            raise HTTPException(
                status_code=400,
                detail=f"要求メモリ ({resources.memory}) がクラスタ内の最大ノード "
                       f"({max_resources['max_memory']}Mi) を超えています",
            )

    # Check GPU resource
    if resources.gpu > 0:
        max_gpu = max_resources["max_gpu"] if max_resources and max_resources["max_gpu"] is not None else 0
        if max_gpu == 0:
            raise HTTPException(
                status_code=400,
                detail="GPU ノードがクラスタに登録されていません",
            )
        if resources.gpu > max_gpu:
            raise HTTPException(
                status_code=400,
                detail=f"要求 GPU ({resources.gpu}) がクラスタ内の最大ノード "
                       f"({max_gpu}) を超えています",
            )

    # Resolve time_limit_seconds
    time_limit = time_limit_seconds if time_limit_seconds is not None else settings.DEFAULT_TIME_LIMIT_SECONDS
    if time_limit > settings.MAX_TIME_LIMIT_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"time_limit_seconds は {settings.MAX_TIME_LIMIT_SECONDS} 秒（7日）以下で指定してください",
        )
    if time_limit <= 0:
        raise HTTPException(
            status_code=400,
            detail="time_limit_seconds は 1 以上で指定してください",
        )

    return time_limit


def submit_job(
    session: Session, namespace: str, username: str, req: JobSubmitRequest
) -> JobSubmitResponse:
    settings = get_settings()
    time_limit = _validate_common(session, namespace, req.resources, req.time_limit_seconds)

    # Allocate job_id
    job_id = allocate_job_id(session, namespace)

    # Compute log_dir
    log_dir = f"{settings.LOG_BASE_DIR}/{job_id}"

    # Insert job
    job = Job(
        namespace=namespace,
        job_id=job_id,
        user=username,
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


def submit_sweep(
    session: Session, namespace: str, username: str, req: SweepSubmitRequest
) -> JobSubmitResponse:
    from fastapi import HTTPException

    settings = get_settings()
    time_limit = _validate_common(session, namespace, req.resources, req.time_limit_seconds)

    # Sweep-specific validation
    if req.completions < 1 or req.completions > settings.MAX_SWEEP_COMPLETIONS:
        raise HTTPException(
            status_code=400,
            detail=f"completions は 1 以上 {settings.MAX_SWEEP_COMPLETIONS} 以下で指定してください",
        )

    if req.parallelism < 1 or req.parallelism > req.completions:
        raise HTTPException(
            status_code=400,
            detail="parallelism は 1 以上 completions 以下で指定してください",
        )

    # Check parallelism * per_pod_resource <= cluster total
    cluster_totals = session.execute(
        text(
            "SELECT COALESCE(SUM(cpu_millicores), 0) AS total_cpu, "
            "       COALESCE(SUM(memory_mib), 0) AS total_memory, "
            "       COALESCE(SUM(gpu), 0) AS total_gpu "
            "FROM node_resources"
        )
    ).mappings().first()

    if cluster_totals and cluster_totals["total_cpu"] > 0:
        req_cpu = parse_cpu_millicores(req.resources.cpu)
        req_mem = parse_memory_mib(req.resources.memory)
        total_cpu = req_cpu * req.parallelism
        total_mem = req_mem * req.parallelism

        if total_cpu > cluster_totals["total_cpu"]:
            raise HTTPException(
                status_code=400,
                detail=f"parallelism × 要求 CPU ({total_cpu}m) がクラスタ全体の CPU "
                       f"({cluster_totals['total_cpu']}m) を超えています",
            )
        if total_mem > cluster_totals["total_memory"]:
            raise HTTPException(
                status_code=400,
                detail=f"parallelism × 要求メモリ ({total_mem}Mi) がクラスタ全体のメモリ "
                       f"({cluster_totals['total_memory']}Mi) を超えています",
            )

    if req.resources.gpu > 0 and cluster_totals:
        total_gpu = req.resources.gpu * req.parallelism
        cluster_gpu = cluster_totals["total_gpu"]
        if cluster_gpu == 0 or total_gpu > cluster_gpu:
            raise HTTPException(
                status_code=400,
                detail=f"parallelism × 要求 GPU ({total_gpu}) がクラスタ全体の GPU "
                       f"({cluster_gpu}) を超えています",
            )

    # Allocate job_id
    job_id = allocate_job_id(session, namespace)

    # Compute log_dir
    log_dir = f"{settings.LOG_BASE_DIR}/{job_id}"

    # Insert job
    job = Job(
        namespace=namespace,
        job_id=job_id,
        user=username,
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
        completions=req.completions,
        parallelism=req.parallelism,
        succeeded_count=0,
        failed_count=0,
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
    settings = get_settings()
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
            completions=j.completions,
            parallelism=j.parallelism,
            succeeded_count=j.succeeded_count,
            failed_count=j.failed_count,
        )
        for j in rows
    ]
    return JobListResponse(jobs=jobs, total_count=total_count, log_base_dir=settings.LOG_BASE_DIR)


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
        cpu=job.cpu,
        memory=job.memory,
        gpu=job.gpu,
        time_limit_seconds=job.time_limit_seconds,
        k8s_job_name=job.k8s_job_name,
        log_dir=job.log_dir,
        created_at=job.created_at,
        dispatched_at=job.dispatched_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        last_error=job.last_error,
        completions=job.completions,
        parallelism=job.parallelism,
        succeeded_count=job.succeeded_count,
        failed_count=job.failed_count,
        completed_indexes=job.completed_indexes,
        failed_indexes=job.failed_indexes,
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
    log_dirs = []

    if job_ids is None:
        # Delete all completed jobs in namespace
        jobs = (
            session.query(Job)
            .filter(Job.namespace == namespace)
            .all()
        )
        targets = [(j.job_id, j.status, j.log_dir) for j in jobs]
    else:
        targets = []
        for jid in job_ids:
            job = session.get(Job, (namespace, jid))
            if job is None:
                not_found.append(jid)
            else:
                targets.append((job.job_id, job.status, job.log_dir))

    for jid, status, log_dir in targets:
        if status in DELETABLE_STATUSES:
            session.query(Job).filter(
                Job.namespace == namespace, Job.job_id == jid
            ).delete()
            deleted.append(jid)
            if log_dir:
                log_dirs.append(log_dir)
        elif status == "DELETING":
            skipped.append(SkippedItem(job_id=jid, reason="deleting"))
        elif status in ACTIVE_STATUSES:
            skipped.append(SkippedItem(job_id=jid, reason="running"))

    return DeleteResponse(deleted=deleted, skipped=skipped, not_found=not_found, log_dirs=log_dirs)


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


def get_usage(session: Session, namespace: str) -> UsageResponse:
    settings = get_settings()
    window_days = settings.FAIR_SHARE_WINDOW_DAYS

    result = session.execute(
        text(
            "SELECT usage_date, cpu_millicores_seconds, memory_mib_seconds, gpu_seconds "
            "FROM namespace_daily_usage "
            "WHERE namespace = :namespace "
            "  AND usage_date > CURRENT_DATE - :window_days "
            "ORDER BY usage_date ASC"
        ),
        {"namespace": namespace, "window_days": window_days},
    )

    daily = []
    total_cpu = 0
    total_mem = 0
    total_gpu = 0
    for row in result.mappings():
        cpu = row["cpu_millicores_seconds"]
        mem = row["memory_mib_seconds"]
        gpu = row["gpu_seconds"]
        daily.append(DailyUsage(
            date=str(row["usage_date"]),
            cpu_millicores_seconds=cpu,
            memory_mib_seconds=mem,
            gpu_seconds=gpu,
        ))
        total_cpu += cpu
        total_mem += mem
        total_gpu += gpu

    return UsageResponse(
        window_days=window_days,
        daily=daily,
        total_cpu_millicores_seconds=total_cpu,
        total_memory_mib_seconds=total_mem,
        total_gpu_seconds=total_gpu,
    )
