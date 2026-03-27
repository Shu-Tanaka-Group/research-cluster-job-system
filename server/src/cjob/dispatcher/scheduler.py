import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from cjob.config import Settings
from cjob.models import Job, JobEvent

logger = logging.getLogger(__name__)


def _cleanup_old_usage(session: Session, settings: Settings):
    """Delete namespace_daily_usage rows outside the sliding window."""
    session.execute(
        text(
            "DELETE FROM namespace_daily_usage "
            "WHERE usage_date <= CURRENT_DATE - :window_days"
        ),
        {"window_days": settings.FAIR_SHARE_WINDOW_DAYS},
    )
    session.commit()


def _fetch_cluster_totals(session: Session) -> tuple[int, int, int]:
    """Fetch cluster resource totals from node_resources table."""
    row = session.execute(
        text(
            "SELECT COALESCE(SUM(cpu_millicores), 0) AS total_cpu, "
            "       COALESCE(SUM(memory_mib), 0) AS total_memory, "
            "       COALESCE(SUM(gpu), 0) AS total_gpu "
            "FROM node_resources"
        )
    ).mappings().first()
    return row["total_cpu"], row["total_memory"], row["total_gpu"]


def fetch_dispatchable_jobs(session: Session, settings: Settings) -> list[Job]:
    """Fetch up to batch_size QUEUED jobs, round-robin across namespaces.

    Uses DRF (Dominant Resource Fairness) to prioritise namespaces with
    lower cumulative resource consumption over a sliding window.
    """
    _cleanup_old_usage(session, settings)

    cluster_cpu, cluster_mem, cluster_gpus = _fetch_cluster_totals(session)

    # If node_resources is empty (Watcher not yet running), fall back to
    # simple namespace-name ordering without DRF.
    if cluster_cpu == 0 and cluster_mem == 0:
        logger.debug("node_resources is empty; DRF disabled, using namespace order")
        result = session.execute(
            text(
                "WITH active AS ("
                "  SELECT namespace, COUNT(*) AS active_count"
                "  FROM jobs"
                "  WHERE status IN ('DISPATCHING', 'DISPATCHED', 'RUNNING')"
                "  GROUP BY namespace"
                "), "
                "queued AS ("
                "  SELECT *, ROW_NUMBER() OVER ("
                "    PARTITION BY namespace ORDER BY created_at ASC"
                "  ) AS rn"
                "  FROM jobs"
                "  WHERE status = 'QUEUED'"
                "    AND (retry_after IS NULL OR retry_after <= NOW())"
                ") "
                "SELECT q.* FROM queued q"
                "  LEFT JOIN active a USING (namespace)"
                "  LEFT JOIN namespace_weights w ON q.namespace = w.namespace "
                "WHERE COALESCE(a.active_count, 0) < :dispatch_limit "
                "  AND q.rn <= :dispatch_limit - COALESCE(a.active_count, 0) "
                "  AND COALESCE(w.weight, 1) > 0 "
                "ORDER BY CEIL(q.rn * 1.0 / :round_size) ASC, "
                "  q.namespace ASC "
                "LIMIT :batch_size"
            ),
            {
                "dispatch_limit": settings.DISPATCH_BUDGET_PER_NAMESPACE,
                "batch_size": settings.DISPATCH_BATCH_SIZE,
                "round_size": settings.DISPATCH_ROUND_SIZE,
            },
        )
    else:
        result = session.execute(
            text(
                "WITH active AS ("
                "  SELECT namespace, COUNT(*) AS active_count"
                "  FROM jobs"
                "  WHERE status IN ('DISPATCHING', 'DISPATCHED', 'RUNNING')"
                "  GROUP BY namespace"
                "), "
                "queued AS ("
                "  SELECT *, ROW_NUMBER() OVER ("
                "    PARTITION BY namespace ORDER BY created_at ASC"
                "  ) AS rn"
                "  FROM jobs"
                "  WHERE status = 'QUEUED'"
                "    AND (retry_after IS NULL OR retry_after <= NOW())"
                "), "
                "usage AS ("
                "  SELECT namespace,"
                "    SUM(cpu_millicores_seconds) AS cpu_millicores_seconds,"
                "    SUM(memory_mib_seconds) AS memory_mib_seconds,"
                "    SUM(gpu_seconds) AS gpu_seconds"
                "  FROM namespace_daily_usage"
                "  WHERE usage_date > CURRENT_DATE - :window_days"
                "  GROUP BY namespace"
                ") "
                "SELECT q.* FROM queued q"
                "  LEFT JOIN active a USING (namespace)"
                "  LEFT JOIN usage u ON q.namespace = u.namespace"
                "  LEFT JOIN namespace_weights w ON q.namespace = w.namespace "
                "WHERE COALESCE(a.active_count, 0) < :dispatch_limit "
                "  AND q.rn <= :dispatch_limit - COALESCE(a.active_count, 0) "
                "  AND COALESCE(w.weight, 1) > 0 "
                "ORDER BY CEIL(q.rn * 1.0 / :round_size) ASC, "
                "  GREATEST("
                "    COALESCE(u.cpu_millicores_seconds, 0) * 1.0 / :cluster_cpu_millicores,"
                "    COALESCE(u.memory_mib_seconds, 0) * 1.0 / :cluster_memory_mib,"
                "    COALESCE(u.gpu_seconds, 0) * 1.0 / NULLIF(:cluster_gpus, 0)"
                "  ) / COALESCE(w.weight, 1) ASC NULLS FIRST, "
                "  q.namespace ASC "
                "LIMIT :batch_size"
            ),
            {
                "dispatch_limit": settings.DISPATCH_BUDGET_PER_NAMESPACE,
                "batch_size": settings.DISPATCH_BATCH_SIZE,
                "round_size": settings.DISPATCH_ROUND_SIZE,
                "window_days": settings.FAIR_SHARE_WINDOW_DAYS,
                "cluster_cpu_millicores": cluster_cpu,
                "cluster_memory_mib": cluster_mem,
                "cluster_gpus": cluster_gpus,
            },
        )

    jobs = []
    for row in result.mappings():
        job = session.get(Job, (row["namespace"], row["job_id"]))
        if job is not None:
            jobs.append(job)
    return jobs


def cas_update_to_dispatching(
    session: Session, namespace: str, job_id: int
) -> bool:
    """CAS update: QUEUED -> DISPATCHING. Returns True if successful."""
    result = session.execute(
        text(
            "UPDATE jobs SET status = 'DISPATCHING' "
            "WHERE namespace = :namespace AND job_id = :job_id AND status = 'QUEUED'"
        ),
        {"namespace": namespace, "job_id": job_id},
    )
    session.flush()
    return result.rowcount > 0


def mark_dispatched(
    session: Session, namespace: str, job_id: int, k8s_job_name: str
) -> bool:
    """Mark job as DISPATCHED after K8s Job creation success."""
    result = session.execute(
        text(
            "UPDATE jobs SET status = 'DISPATCHED', "
            "k8s_job_name = :k8s_job_name, dispatched_at = NOW() "
            "WHERE namespace = :namespace AND job_id = :job_id "
            "AND status = 'DISPATCHING'"
        ),
        {"namespace": namespace, "job_id": job_id, "k8s_job_name": k8s_job_name},
    )
    if result.rowcount > 0:
        session.add(
            JobEvent(namespace=namespace, job_id=job_id, event_type="DISPATCHED")
        )
    session.flush()
    return result.rowcount > 0


def mark_failed(
    session: Session, namespace: str, job_id: int, error: str
) -> bool:
    """Mark job as FAILED (permanent error or max retries exceeded)."""
    result = session.execute(
        text(
            "UPDATE jobs SET status = 'FAILED', "
            "finished_at = NOW(), last_error = :error "
            "WHERE namespace = :namespace AND job_id = :job_id "
            "AND status = 'DISPATCHING'"
        ),
        {"namespace": namespace, "job_id": job_id, "error": error},
    )
    if result.rowcount > 0:
        session.add(
            JobEvent(
                namespace=namespace,
                job_id=job_id,
                event_type="FAILED",
                payload_json={"error": error},
            )
        )
    session.flush()
    return result.rowcount > 0


def increment_retry(
    session: Session, namespace: str, job_id: int, retry_interval_sec: int
) -> bool:
    """Increment retry count and set retry_after, reverting to QUEUED."""
    result = session.execute(
        text(
            "UPDATE jobs SET "
            "retry_count = retry_count + 1, "
            "retry_after = NOW() + MAKE_INTERVAL(secs => :interval), "
            "status = 'QUEUED' "
            "WHERE namespace = :namespace AND job_id = :job_id "
            "AND status = 'DISPATCHING'"
        ),
        {"namespace": namespace, "job_id": job_id, "interval": retry_interval_sec},
    )
    if result.rowcount > 0:
        session.add(
            JobEvent(namespace=namespace, job_id=job_id, event_type="RETRY")
        )
    session.flush()
    return result.rowcount > 0


def fetch_stalled_jobs(session: Session, threshold_sec: int) -> list[Job]:
    """Fetch DISPATCHED jobs that have been waiting longer than threshold_sec."""
    result = session.execute(
        text(
            "SELECT namespace, job_id FROM jobs "
            "WHERE status = 'DISPATCHED' "
            "  AND dispatched_at <= NOW() - MAKE_INTERVAL(secs => :threshold)"
        ),
        {"threshold": threshold_sec},
    )
    jobs = []
    for row in result.mappings():
        job = session.get(Job, (row["namespace"], row["job_id"]))
        if job is not None:
            jobs.append(job)
    return jobs


def estimate_shortest_remaining(session: Session, namespace: str) -> int | None:
    """Estimate the shortest remaining time (seconds) among RUNNING jobs in a namespace.

    Returns None if there are no RUNNING jobs with a known started_at.
    """
    result = session.execute(
        text(
            "SELECT MIN("
            "  EXTRACT(EPOCH FROM "
            "    (started_at + MAKE_INTERVAL(secs => time_limit_seconds)) - NOW()"
            "  )"
            ") AS min_remaining "
            "FROM jobs "
            "WHERE namespace = :namespace "
            "  AND status = 'RUNNING' "
            "  AND started_at IS NOT NULL"
        ),
        {"namespace": namespace},
    )
    row = result.mappings().first()
    if row is None or row["min_remaining"] is None:
        return None
    remaining = int(row["min_remaining"])
    return max(remaining, 0)


def apply_gap_filling(
    session: Session, candidates: list[Job], settings: Settings
) -> list[Job]:
    """Filter dispatch candidates based on gap filling logic.

    When stalled jobs (DISPATCHED for too long) exist in a namespace,
    only dispatch QUEUED jobs whose time_limit_seconds fits within the
    estimated remaining time of RUNNING jobs in that namespace.
    """
    if not settings.GAP_FILLING_ENABLED:
        return candidates

    stalled = fetch_stalled_jobs(session, settings.GAP_FILLING_STALL_THRESHOLD_SEC)
    stalled_namespaces = {job.namespace for job in stalled}

    if not stalled_namespaces:
        return candidates

    result = [c for c in candidates if c.namespace not in stalled_namespaces]

    for ns in stalled_namespaces:
        ns_candidates = [c for c in candidates if c.namespace == ns]
        if not ns_candidates:
            continue

        remaining = estimate_shortest_remaining(session, ns)

        if remaining is None:
            # No RUNNING jobs: allow all candidates to avoid deadlock.
            # Without this, a stalled large job with no RUNNING jobs
            # would block all dispatch for this namespace indefinitely.
            result.extend(ns_candidates)
            continue

        for c in ns_candidates:
            if c.time_limit_seconds <= remaining:
                result.append(c)
            else:
                logger.debug(
                    "Gap filling: holding %s/%d (time_limit=%ds, remaining=%s)",
                    ns, c.job_id, c.time_limit_seconds, remaining,
                )

    return result


def reset_stale_dispatching(session: Session) -> int:
    """Reset DISPATCHING jobs to QUEUED on startup."""
    result = session.execute(
        text(
            "UPDATE jobs SET status = 'QUEUED', retry_after = NULL "
            "WHERE status = 'DISPATCHING'"
        )
    )
    session.commit()
    count = result.rowcount
    if count > 0:
        logger.info("Reset %d stale DISPATCHING jobs to QUEUED", count)
    return count
