import logging
from collections import defaultdict

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from sqlalchemy import text
from sqlalchemy.orm import Session

from cjob.models import Job, JobEvent

logger = logging.getLogger(__name__)


def list_cjob_k8s_jobs() -> list[k8s_client.V1Job]:
    """List all K8s Jobs with cjob.io/job-id label across all namespaces."""
    batch_v1 = k8s_client.BatchV1Api()
    try:
        result = batch_v1.list_job_for_all_namespaces(
            label_selector="cjob.io/job-id"
        )
        return result.items
    except ApiException as e:
        logger.error("Failed to list K8s Jobs: %s", e)
        return []


def determine_status(k8s_job: k8s_client.V1Job) -> str | None:
    """Map K8s Job conditions to DB status."""
    if k8s_job.status and k8s_job.status.conditions:
        for cond in k8s_job.status.conditions:
            if cond.type == "Complete" and cond.status == "True":
                return "SUCCEEDED"
            if cond.type == "Failed" and cond.status == "True":
                return "FAILED"

    # Check if pod is running
    if k8s_job.status and k8s_job.status.active and k8s_job.status.active > 0:
        return "RUNNING"

    return None


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

        # Normal status sync (don't overwrite CANCELLED or DELETING)
        new_status = determine_status(kj)
        if new_status and new_status != db_job.status:
            db_job.status = new_status
            if new_status in ("SUCCEEDED", "FAILED"):
                from sqlalchemy import func
                db_job.finished_at = func.now()
            session.add(
                JobEvent(namespace=ns, job_id=jid, event_type=new_status)
            )
            logger.info("Updated %s/%d: %s -> %s", ns, jid, db_job.status, new_status)

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
