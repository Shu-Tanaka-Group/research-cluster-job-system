import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from cjob.config import Settings
from cjob.models import Job, JobEvent

logger = logging.getLogger(__name__)


def fetch_dispatchable_jobs(session: Session, settings: Settings) -> list[Job]:
    """Fetch up to batch_size oldest QUEUED jobs per namespace where dispatch budget is available."""
    result = session.execute(
        text(
            "SELECT * FROM ("
            "  SELECT *, ROW_NUMBER() OVER ("
            "    PARTITION BY namespace ORDER BY created_at ASC"
            "  ) AS rn,"
            "  COUNT(*) FILTER ("
            "    WHERE status IN ('DISPATCHING', 'DISPATCHED', 'RUNNING')"
            "  ) OVER (PARTITION BY namespace) AS active_count"
            "  FROM jobs"
            "  WHERE (status = 'QUEUED' AND (retry_after IS NULL OR retry_after <= NOW()))"
            "     OR status IN ('DISPATCHING', 'DISPATCHED', 'RUNNING')"
            ") sub "
            "WHERE status = 'QUEUED' "
            "  AND active_count < :dispatch_limit "
            "  AND rn <= LEAST(:batch_size, :dispatch_limit - active_count) "
            "ORDER BY namespace, created_at ASC"
        ),
        {
            "dispatch_limit": settings.DISPATCH_BUDGET_PER_NAMESPACE,
            "batch_size": settings.DISPATCH_BATCH_SIZE_PER_NAMESPACE,
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
