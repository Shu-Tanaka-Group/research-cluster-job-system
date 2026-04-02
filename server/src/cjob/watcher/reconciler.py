import logging
from collections import defaultdict

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from cjob.models import Job, JobEvent
from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib

logger = logging.getLogger(__name__)


def list_cjob_k8s_jobs() -> list[k8s_client.V1Job]:
    """List all K8s Jobs with cjob.io/job-id label across all namespaces.

    Raises ApiException on failure so that the caller (main loop) can skip
    the entire reconcile cycle.  Processing with an incomplete job list would
    cause Step 8 to mark healthy jobs as FAILED and DELETING Phase 2 to
    prematurely clean up DB records.
    """
    batch_v1 = k8s_client.BatchV1Api()
    result = batch_v1.list_job_for_all_namespaces(
        label_selector="cjob.io/job-id"
    )
    return result.items


def determine_status(k8s_job: k8s_client.V1Job) -> tuple[str | None, str | None]:
    """Map K8s Job conditions to (DB status, reason).

    Returns a tuple of (status, reason). reason is set for specific failure
    modes (e.g. "DeadlineExceeded") and None otherwise.
    """
    if k8s_job.status and k8s_job.status.conditions:
        for cond in k8s_job.status.conditions:
            if cond.type == "Complete" and cond.status == "True":
                return "SUCCEEDED", None
            if cond.type == "Failed" and cond.status == "True":
                return "FAILED", cond.reason

    # Check if pod is running
    if k8s_job.status and k8s_job.status.active and k8s_job.status.active > 0:
        return "RUNNING", None

    return None, None


def _delete_k8s_job(namespace: str, name: str):
    """Delete a K8s Job with background propagation."""
    batch_v1 = k8s_client.BatchV1Api()
    try:
        batch_v1.delete_namespaced_job(
            name=name,
            namespace=namespace,
            body=k8s_client.V1DeleteOptions(
                propagation_policy="Background",
            ),
        )
        logger.info("Deleted K8s Job %s/%s", namespace, name)
    except ApiException as e:
        if e.status == 404:
            logger.debug("K8s Job %s/%s already deleted", namespace, name)
        else:
            logger.error("Failed to delete K8s Job %s/%s: %s", namespace, name, e)


def _fetch_node_name(namespace: str, k8s_job_name: str) -> str | None:
    """Fetch the node name where the Job's Pod is running."""
    core_v1 = k8s_client.CoreV1Api()
    try:
        pods = core_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"job-name={k8s_job_name}",
        )
        for pod in pods.items:
            if pod.spec and pod.spec.node_name:
                return pod.spec.node_name
    except ApiException as e:
        logger.warning("Failed to fetch Pod for %s/%s: %s", namespace, k8s_job_name, e)
    return None


def _record_resource_usage(session: Session, job: Job):
    """Add resource usage to namespace_daily_usage on RUNNING transition."""
    parallelism = job.parallelism if job.completions is not None else 1
    delta_cpu = job.time_limit_seconds * parse_cpu_millicores(job.cpu) * parallelism
    delta_mem = job.time_limit_seconds * parse_memory_mib(job.memory) * parallelism
    delta_gpu = job.time_limit_seconds * job.gpu * parallelism

    session.execute(
        text(
            "INSERT INTO namespace_daily_usage "
            "(namespace, usage_date, cpu_millicores_seconds, memory_mib_seconds, gpu_seconds) "
            "VALUES (:namespace, CURRENT_DATE, :delta_cpu, :delta_mem, :delta_gpu) "
            "ON CONFLICT (namespace, usage_date) DO UPDATE SET "
            "cpu_millicores_seconds = namespace_daily_usage.cpu_millicores_seconds "
            "+ :delta_cpu, "
            "memory_mib_seconds = namespace_daily_usage.memory_mib_seconds "
            "+ :delta_mem, "
            "gpu_seconds = namespace_daily_usage.gpu_seconds + :delta_gpu"
        ),
        {
            "namespace": job.namespace,
            "delta_cpu": delta_cpu,
            "delta_mem": delta_mem,
            "delta_gpu": delta_gpu,
        },
    )


def reconcile_cycle(session: Session, k8s_jobs: list[k8s_client.V1Job]):
    """Run one reconciliation cycle."""
    # Build lookup: (namespace, job_id) -> k8s_job
    k8s_map: dict[tuple[str, int], k8s_client.V1Job] = {}
    for kj in k8s_jobs:
        labels = kj.metadata.labels or {}
        ns = labels.get("cjob.io/namespace")
        jid_str = labels.get("cjob.io/job-id")
        if ns and jid_str:
            try:
                k8s_map[(ns, int(jid_str))] = kj
            except ValueError:
                logger.warning("Invalid cjob.io/job-id label: %s", jid_str)

    # Get all relevant DB jobs
    all_namespaces = {ns for ns, _ in k8s_map}
    db_jobs: dict[tuple[str, int], Job] = {}
    if all_namespaces:
        jobs = (
            session.query(Job)
            .filter(Job.namespace.in_(all_namespaces))
            .all()
        )
        for j in jobs:
            db_jobs[(j.namespace, j.job_id)] = j

    # Also get DELETING jobs that may not have K8s Jobs anymore
    deleting_jobs = (
        session.query(Job).filter(Job.status == "DELETING").all()
    )
    deleting_by_ns: dict[str, list[Job]] = defaultdict(list)
    for j in deleting_jobs:
        deleting_by_ns[j.namespace].append(j)
        db_jobs[(j.namespace, j.job_id)] = j

    # Process each K8s Job
    for (ns, jid), kj in k8s_map.items():
        kj_name = kj.metadata.name
        db_job = db_jobs.get((ns, jid))

        if db_job is None:
            # Orphan K8s Job: no DB record
            logger.warning("Orphan K8s Job detected: %s/%s (job_id=%d)", ns, kj_name, jid)
            _delete_k8s_job(ns, kj_name)
            continue

        # Handle CANCELLED jobs: delete K8s Job, keep DB status
        if db_job.status == "CANCELLED":
            _delete_k8s_job(ns, kj_name)
            continue

        # Handle DELETING jobs: Phase 1 - delete K8s Job
        if db_job.status == "DELETING":
            _delete_k8s_job(ns, kj_name)
            continue

        # Update sweep index tracking before status sync
        if db_job.completions is not None and kj.status:
            kj_status = kj.status
            new_succeeded = kj_status.succeeded or 0
            new_failed = kj_status.failed or 0
            new_completed_indexes = getattr(kj_status, 'completed_indexes', None) or ""
            new_failed_indexes = getattr(kj_status, 'failed_indexes', None) or ""
            if (db_job.succeeded_count != new_succeeded
                    or db_job.failed_count != new_failed
                    or db_job.completed_indexes != new_completed_indexes
                    or db_job.failed_indexes != new_failed_indexes):
                db_job.succeeded_count = new_succeeded
                db_job.failed_count = new_failed
                db_job.completed_indexes = new_completed_indexes
                db_job.failed_indexes = new_failed_indexes

        # Normal status sync (don't overwrite CANCELLED or DELETING)
        new_status, reason = determine_status(kj)

        # For sweep: K8s Complete + failed_count > 0 → FAILED
        if (db_job.completions is not None
                and new_status == "SUCCEEDED"
                and db_job.failed_count
                and db_job.failed_count > 0):
            new_status = "FAILED"

        if new_status and new_status != db_job.status:
            old_status = db_job.status
            db_job.status = new_status
            if new_status == "RUNNING" and db_job.started_at is None:
                db_job.started_at = func.now()
                if kj.metadata and kj.metadata.name:
                    db_job.node_name = _fetch_node_name(ns, kj.metadata.name)
                _record_resource_usage(session, db_job)
            if db_job.node_name is None and new_status in ("SUCCEEDED", "FAILED") \
                    and kj.metadata and kj.metadata.name:
                db_job.node_name = _fetch_node_name(ns, kj.metadata.name)
            if new_status in ("SUCCEEDED", "FAILED"):
                db_job.finished_at = func.now()
            if new_status == "FAILED" and reason == "DeadlineExceeded":
                db_job.last_error = "time limit exceeded"
            session.add(
                JobEvent(namespace=ns, job_id=jid, event_type=new_status)
            )
            logger.info("Updated %s/%d: %s -> %s", ns, jid, old_status, new_status)

    # Step 8: Mark DISPATCHED/RUNNING jobs with no K8s Job as FAILED
    disappeared_jobs = (
        session.query(Job)
        .filter(Job.status.in_(["DISPATCHED", "RUNNING"]))
        .all()
    )
    for job in disappeared_jobs:
        if (job.namespace, job.job_id) not in k8s_map:
            job.status = "FAILED"
            job.last_error = (
                "K8s Job not found (TTL expired or manually deleted)"
            )
            job.finished_at = func.now()
            session.add(
                JobEvent(
                    namespace=job.namespace,
                    job_id=job.job_id,
                    event_type="FAILED",
                )
            )
            logger.info(
                "Marked %s/%d as FAILED: K8s Job not found",
                job.namespace,
                job.job_id,
            )

    # DELETING Phase 2: For each namespace with DELETING jobs,
    # check if all K8s Jobs are gone. If so, clean up DB.
    for ns, del_jobs in deleting_by_ns.items():
        all_gone = True
        for dj in del_jobs:
            if (dj.namespace, dj.job_id) in k8s_map:
                all_gone = False
                break

        if all_gone and del_jobs:
            logger.info(
                "All K8s Jobs for namespace %s are deleted. "
                "Cleaning up DB records (%d jobs)",
                ns,
                len(del_jobs),
            )
            # Single transaction: delete all jobs + reset counter
            session.execute(
                text("DELETE FROM jobs WHERE namespace = :namespace"),
                {"namespace": ns},
            )
            session.execute(
                text(
                    "UPDATE user_job_counters SET next_id = 1 "
                    "WHERE namespace = :namespace"
                ),
                {"namespace": ns},
            )
            logger.info("Reset complete for namespace %s", ns)

    session.flush()
