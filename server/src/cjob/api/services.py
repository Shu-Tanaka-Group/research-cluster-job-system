import logging

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from cjob.config import get_settings
from cjob.metrics import JOBS_COMPLETED_TOTAL, JOBS_SUBMITTED_TOTAL
from cjob.models import Job, JobEvent, UserJobCounter
from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib

from .schemas import (
    CancelResponse,
    DailyUsage,
    DeleteResponse,
    FlavorInfo,
    FlavorListResponse,
    FlavorNodeInfo,
    FlavorQuotaInfo,
    HoldResponse,
    JobDetailResponse,
    JobListResponse,
    JobSubmitRequest,
    JobSubmitResponse,
    JobSummary,
    ReleaseResponse,
    ResourceQuota,
    ResourceSpec,
    SkippedItem,
    SweepSubmitRequest,
    UsageResponse,
)

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}
ACTIVE_STATUSES = {"QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING", "HELD"}
CANCELLABLE_STATUSES = {"QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING", "HELD"}
DELETABLE_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}
HOLDABLE_STATUSES = {"QUEUED"}
RELEASABLE_STATUSES = {"HELD"}
# Statuses counted toward MAX_QUEUED_JOBS_PER_NAMESPACE
COUNTED_STATUSES = {"QUEUED", "DISPATCHING", "DISPATCHED", "CANCELLED", "HELD"}


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
) -> tuple[int, str]:
    """Shared validation for submit_job and submit_sweep. Returns (resolved_time_limit, resolved_flavor)."""
    from fastapi import HTTPException

    settings = get_settings()

    # Resolve flavor
    flavor = resources.flavor or settings.DEFAULT_FLAVOR
    flavor_def = settings.get_flavor_definition(flavor)
    if flavor_def is None:
        available = ", ".join(f.name for f in settings.flavors)
        raise HTTPException(
            status_code=400,
            detail=f"指定された flavor '{flavor}' は存在しません。利用可能な flavor: {available}",
        )

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

    # Check resource exceeds max node allocatable (per-flavor)
    max_resources = session.execute(
        text(
            "SELECT MAX(cpu_millicores) AS max_cpu, "
            "       MAX(memory_mib) AS max_memory, "
            "       MAX(gpu) AS max_gpu "
            "FROM node_resources "
            "WHERE flavor = :flavor"
        ),
        {"flavor": flavor},
    ).mappings().first()

    # Fetch nominalQuota for the flavor
    quota_row = session.execute(
        text("SELECT cpu, memory, gpu FROM flavor_quotas WHERE flavor = :flavor"),
        {"flavor": flavor},
    ).mappings().first()

    if max_resources and max_resources["max_cpu"] is not None:
        req_cpu = parse_cpu_millicores(resources.cpu)
        req_mem = parse_memory_mib(resources.memory)

        effective_cpu = max_resources["max_cpu"]
        cpu_source = "最大ノード"
        effective_memory = max_resources["max_memory"]
        memory_source = "最大ノード"

        if quota_row:
            quota_cpu = parse_cpu_millicores(quota_row["cpu"])
            quota_mem = parse_memory_mib(quota_row["memory"])
            if quota_cpu < effective_cpu:
                effective_cpu = quota_cpu
                cpu_source = "クォータ"
            if quota_mem < effective_memory:
                effective_memory = quota_mem
                memory_source = "クォータ"

        if req_cpu > effective_cpu:
            raise HTTPException(
                status_code=400,
                detail=f"要求 CPU ({resources.cpu}) が flavor '{flavor}' の{cpu_source} "
                       f"({effective_cpu}m) を超えています",
            )
        if req_mem > effective_memory:
            raise HTTPException(
                status_code=400,
                detail=f"要求メモリ ({resources.memory}) が flavor '{flavor}' の{memory_source} "
                       f"({effective_memory}Mi) を超えています",
            )

    # Check GPU resource
    if resources.gpu > 0:
        if flavor_def.gpu_resource_name is None:
            raise HTTPException(
                status_code=400,
                detail=f"flavor '{flavor}' は GPU をサポートしていません",
            )
        max_gpu = max_resources["max_gpu"] if max_resources and max_resources["max_gpu"] is not None else 0
        if max_gpu == 0:
            raise HTTPException(
                status_code=400,
                detail=f"flavor '{flavor}' に GPU ノードが登録されていません",
            )
        effective_gpu = max_gpu
        gpu_source = "最大ノード"
        if quota_row:
            quota_gpu = int(quota_row["gpu"])
            if quota_gpu < effective_gpu:
                effective_gpu = quota_gpu
                gpu_source = "クォータ"
        if resources.gpu > effective_gpu:
            raise HTTPException(
                status_code=400,
                detail=f"要求 GPU ({resources.gpu}) が flavor '{flavor}' の{gpu_source} "
                       f"({effective_gpu}) を超えています",
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

    return time_limit, flavor


def submit_job(
    session: Session, namespace: str, username: str, req: JobSubmitRequest
) -> JobSubmitResponse:
    from fastapi import HTTPException

    if not req.command:
        raise HTTPException(status_code=400, detail="command は空にできません")

    if not req.image:
        raise HTTPException(status_code=400, detail="image は空にできません")

    settings = get_settings()
    time_limit, flavor = _validate_common(session, namespace, req.resources, req.time_limit_seconds)

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
        flavor=flavor,
        time_limit_seconds=time_limit,
        status="QUEUED",
        log_dir=log_dir,
        cpu_millicores=parse_cpu_millicores(req.resources.cpu),
        memory_mib=parse_memory_mib(req.resources.memory),
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
    JOBS_SUBMITTED_TOTAL.inc()

    return JobSubmitResponse(job_id=job_id, status="QUEUED")


def submit_sweep(
    session: Session, namespace: str, username: str, req: SweepSubmitRequest
) -> JobSubmitResponse:
    from fastapi import HTTPException

    if not req.command:
        raise HTTPException(status_code=400, detail="command は空にできません")

    if not req.image:
        raise HTTPException(status_code=400, detail="image は空にできません")

    settings = get_settings()
    time_limit, flavor = _validate_common(session, namespace, req.resources, req.time_limit_seconds)

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

    # Check parallelism * per_pod_resource <= flavor total (capped by nominalQuota)
    flavor_totals = session.execute(
        text(
            "SELECT COALESCE(SUM(cpu_millicores), 0) AS total_cpu, "
            "       COALESCE(SUM(memory_mib), 0) AS total_memory, "
            "       COALESCE(SUM(gpu), 0) AS total_gpu "
            "FROM node_resources "
            "WHERE flavor = :flavor"
        ),
        {"flavor": flavor},
    ).mappings().first()

    # Fetch nominalQuota for sweep cluster-wide check
    quota_row = session.execute(
        text("SELECT cpu, memory, gpu FROM flavor_quotas WHERE flavor = :flavor"),
        {"flavor": flavor},
    ).mappings().first()

    if flavor_totals and flavor_totals["total_cpu"] > 0:
        req_cpu = parse_cpu_millicores(req.resources.cpu)
        req_mem = parse_memory_mib(req.resources.memory)
        total_cpu = req_cpu * req.parallelism
        total_mem = req_mem * req.parallelism

        effective_total_cpu = flavor_totals["total_cpu"]
        effective_total_mem = flavor_totals["total_memory"]
        if quota_row:
            quota_cpu = parse_cpu_millicores(quota_row["cpu"])
            quota_mem = parse_memory_mib(quota_row["memory"])
            effective_total_cpu = min(effective_total_cpu, quota_cpu)
            effective_total_mem = min(effective_total_mem, quota_mem)

        if total_cpu > effective_total_cpu:
            raise HTTPException(
                status_code=400,
                detail=f"parallelism × 要求 CPU ({total_cpu}m) が flavor '{flavor}' の CPU 合計 "
                       f"({effective_total_cpu}m) を超えています",
            )
        if total_mem > effective_total_mem:
            raise HTTPException(
                status_code=400,
                detail=f"parallelism × 要求メモリ ({total_mem}Mi) が flavor '{flavor}' のメモリ合計 "
                       f"({effective_total_mem}Mi) を超えています",
            )

    if req.resources.gpu > 0 and flavor_totals:
        total_gpu = req.resources.gpu * req.parallelism
        flavor_gpu = flavor_totals["total_gpu"]
        effective_gpu = flavor_gpu
        if quota_row:
            quota_gpu = int(quota_row["gpu"])
            effective_gpu = min(effective_gpu, quota_gpu)
        if effective_gpu == 0 or total_gpu > effective_gpu:
            raise HTTPException(
                status_code=400,
                detail=f"parallelism × 要求 GPU ({total_gpu}) が flavor '{flavor}' の GPU 合計 "
                       f"({effective_gpu}) を超えています",
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
        flavor=flavor,
        time_limit_seconds=time_limit,
        status="QUEUED",
        log_dir=log_dir,
        completions=req.completions,
        parallelism=req.parallelism,
        succeeded_count=0,
        failed_count=0,
        cpu_millicores=parse_cpu_millicores(req.resources.cpu),
        memory_mib=parse_memory_mib(req.resources.memory),
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
    JOBS_SUBMITTED_TOTAL.inc()

    return JobSubmitResponse(job_id=job_id, status="QUEUED")


def list_jobs(
    session: Session,
    namespace: str,
    status: str | None = None,
    flavor: str | None = None,
    time_limit_ge: int | None = None,
    time_limit_lt: int | None = None,
    limit: int | None = None,
    order: str = "asc",
) -> JobListResponse:
    settings = get_settings()
    base_query = session.query(Job).filter(Job.namespace == namespace)
    if status:
        base_query = base_query.filter(Job.status == status)
    if flavor:
        base_query = base_query.filter(Job.flavor == flavor)
    if time_limit_ge is not None:
        base_query = base_query.filter(Job.time_limit_seconds >= time_limit_ge)
    if time_limit_lt is not None:
        base_query = base_query.filter(Job.time_limit_seconds < time_limit_lt)

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
            flavor=j.flavor,
            command=j.command,
            created_at=j.created_at,
            finished_at=j.finished_at,
            time_limit_seconds=j.time_limit_seconds,
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
        flavor=job.flavor,
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
        node_name=job.node_name.split(",") if job.node_name else None,
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
        JOBS_COMPLETED_TOTAL.labels(status="cancelled").inc()
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


def hold_single(
    session: Session, namespace: str, job_id: int
) -> dict:
    job = session.get(Job, (namespace, job_id))
    if job is None:
        return {"not_found": True}

    if job.status in HOLDABLE_STATUSES:
        job.status = "HELD"
        session.add(
            JobEvent(namespace=namespace, job_id=job_id, event_type="HELD")
        )
        session.flush()
        return {"job_id": job_id, "status": "HELD"}

    return {"job_id": job_id, "status": job.status, "skipped": True}


def hold_bulk(
    session: Session, namespace: str, job_ids: list[int] | None
) -> HoldResponse:
    held = []
    skipped = []
    not_found = []

    if job_ids is None:
        jobs = (
            session.query(Job)
            .filter(Job.namespace == namespace, Job.status.in_(HOLDABLE_STATUSES))
            .all()
        )
        for job in jobs:
            result = hold_single(session, namespace, job.job_id)
            if result.get("skipped"):
                skipped.append(job.job_id)
            else:
                held.append(job.job_id)
    else:
        for jid in job_ids:
            result = hold_single(session, namespace, jid)
            if result.get("not_found"):
                not_found.append(jid)
            elif result.get("skipped"):
                skipped.append(jid)
            else:
                held.append(jid)

    return HoldResponse(held=held, skipped=skipped, not_found=not_found)


def release_single(
    session: Session, namespace: str, job_id: int
) -> dict:
    job = session.get(Job, (namespace, job_id))
    if job is None:
        return {"not_found": True}

    if job.status in RELEASABLE_STATUSES:
        job.status = "QUEUED"
        session.add(
            JobEvent(namespace=namespace, job_id=job_id, event_type="RELEASED")
        )
        session.flush()
        return {"job_id": job_id, "status": "QUEUED"}

    return {"job_id": job_id, "status": job.status, "skipped": True}


def release_bulk(
    session: Session, namespace: str, job_ids: list[int] | None
) -> ReleaseResponse:
    released = []
    skipped = []
    not_found = []

    if job_ids is None:
        jobs = (
            session.query(Job)
            .filter(Job.namespace == namespace, Job.status.in_(RELEASABLE_STATUSES))
            .all()
        )
        for job in jobs:
            result = release_single(session, namespace, job.job_id)
            if result.get("skipped"):
                skipped.append(job.job_id)
            else:
                released.append(job.job_id)
    else:
        for jid in job_ids:
            result = release_single(session, namespace, jid)
            if result.get("not_found"):
                not_found.append(jid)
            elif result.get("skipped"):
                skipped.append(jid)
            else:
                released.append(jid)

    return ReleaseResponse(released=released, skipped=skipped, not_found=not_found)


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
            .filter(Job.namespace == namespace, Job.status.in_(DELETABLE_STATUSES))
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
        elif status == "HELD":
            skipped.append(SkippedItem(job_id=jid, reason="held"))
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

    quota_row = session.execute(
        text(
            "SELECT hard_cpu_millicores, hard_memory_mib, hard_gpu, hard_count, "
            "       used_cpu_millicores, used_memory_mib, used_gpu, used_count "
            "FROM namespace_resource_quotas "
            "WHERE namespace = :namespace"
        ),
        {"namespace": namespace},
    ).mappings().first()

    resource_quota = ResourceQuota(**quota_row) if quota_row else None

    return UsageResponse(
        window_days=window_days,
        daily=daily,
        total_cpu_millicores_seconds=total_cpu,
        total_memory_mib_seconds=total_mem,
        total_gpu_seconds=total_gpu,
        resource_quota=resource_quota,
    )


def list_flavors(session: Session) -> FlavorListResponse:
    settings = get_settings()

    # Fetch all nodes grouped by flavor
    result = session.execute(
        text(
            "SELECT node_name, cpu_millicores, memory_mib, gpu, flavor "
            "FROM node_resources "
            "ORDER BY flavor, node_name"
        )
    )
    nodes_by_flavor: dict[str, list[FlavorNodeInfo]] = {}
    for row in result.mappings():
        nodes_by_flavor.setdefault(row["flavor"], []).append(
            FlavorNodeInfo(
                node_name=row["node_name"],
                cpu_millicores=row["cpu_millicores"],
                memory_mib=row["memory_mib"],
                gpu=row["gpu"],
            )
        )

    # Fetch flavor quotas
    quota_result = session.execute(
        text("SELECT flavor, cpu, memory, gpu FROM flavor_quotas")
    )
    quotas_by_flavor: dict[str, FlavorQuotaInfo] = {}
    for row in quota_result.mappings():
        quotas_by_flavor[row["flavor"]] = FlavorQuotaInfo(
            cpu=row["cpu"],
            memory=row["memory"],
            gpu=row["gpu"],
        )

    flavors = []
    for flavor_def in settings.flavors:
        flavors.append(FlavorInfo(
            name=flavor_def.name,
            has_gpu=flavor_def.gpu_resource_name is not None,
            nodes=nodes_by_flavor.get(flavor_def.name, []),
            quota=quotas_by_flavor.get(flavor_def.name),
        ))

    return FlavorListResponse(
        flavors=flavors,
        default_flavor=settings.DEFAULT_FLAVOR,
    )
