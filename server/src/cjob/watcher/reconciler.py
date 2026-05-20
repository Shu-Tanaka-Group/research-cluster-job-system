import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from sqlalchemy import and_, func, or_, select, text, tuple_
from sqlalchemy.orm import Session

from cjob.metrics import JOBS_COMPLETED_TOTAL
from cjob.models import Job, JobEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LightJobCondition:
    """Minimal representation of a V1JobCondition (§5.1 of watcher.md)."""

    type: str
    status: str
    reason: str | None


@dataclass(frozen=True)
class LightK8sJob:
    """Memory-efficient snapshot of a K8s Job used by the reconciler.

    Only the fields required by the reconcile algorithm are kept so that raw
    V1Job objects can be discarded immediately after each page is parsed
    (see watcher.md §5.1).
    """

    namespace: str
    job_id: int
    name: str
    conditions: tuple[LightJobCondition, ...]
    active: int | None
    ready: int | None
    succeeded: int | None
    failed: int | None
    completed_indexes: str | None
    failed_indexes: str | None

    @classmethod
    def from_v1job(cls, v1: k8s_client.V1Job) -> "LightK8sJob | None":
        meta = v1.metadata
        if meta is None:
            return None
        labels = meta.labels or {}
        ns = labels.get("cjob.io/namespace")
        jid_str = labels.get("cjob.io/job-id")
        if not ns or jid_str is None:
            return None
        try:
            jid = int(jid_str)
        except ValueError:
            logger.warning("Invalid cjob.io/job-id label: %s", jid_str)
            return None

        conditions: tuple[LightJobCondition, ...] = ()
        active: int | None = None
        ready: int | None = None
        succeeded: int | None = None
        failed: int | None = None
        completed_indexes: str | None = None
        failed_indexes: str | None = None

        status = v1.status
        if status is not None:
            if status.conditions:
                conditions = tuple(
                    LightJobCondition(
                        type=c.type or "",
                        status=c.status or "",
                        reason=c.reason,
                    )
                    for c in status.conditions
                )
            active = status.active
            ready = getattr(status, "ready", None)
            succeeded = status.succeeded
            failed = status.failed
            completed_indexes = getattr(status, "completed_indexes", None)
            failed_indexes = getattr(status, "failed_indexes", None)

        return cls(
            namespace=ns,
            job_id=jid,
            name=meta.name or "",
            conditions=conditions,
            active=active,
            ready=ready,
            succeeded=succeeded,
            failed=failed,
            completed_indexes=completed_indexes,
            failed_indexes=failed_indexes,
        )


def list_cjob_k8s_jobs(page_size: int = 500) -> list[LightK8sJob]:
    """List all K8s Jobs with cjob.io/job-id label across all namespaces.

    Pages through the K8s API (``limit``/``_continue``) and converts each page
    to ``LightK8sJob`` so raw V1Job objects can be garbage-collected per page
    (watcher.md §5.1).

    Raises ApiException on any page failure so the caller (main loop) skips
    the entire reconcile cycle. Processing with an incomplete job list would
    cause Step 8 to mark healthy jobs as FAILED and DELETING Phase 2 to
    prematurely clean up DB records.
    """
    batch_v1 = k8s_client.BatchV1Api()
    results: list[LightK8sJob] = []
    continue_token: str | None = None
    while True:
        kwargs: dict[str, object] = {
            "label_selector": "cjob.io/job-id",
            "limit": page_size,
        }
        if continue_token:
            kwargs["_continue"] = continue_token
        page = batch_v1.list_job_for_all_namespaces(**kwargs)
        for v1 in page.items:
            light = LightK8sJob.from_v1job(v1)
            if light is not None:
                results.append(light)
        meta = page.metadata
        continue_token = getattr(meta, "_continue", None) if meta else None
        if not continue_token:
            break
    return results


def determine_status(k8s_job: LightK8sJob) -> tuple[str | None, str | None]:
    """Map K8s Job conditions to (DB status, reason).

    Returns a tuple of (status, reason). reason is set for specific failure
    modes (e.g. "DeadlineExceeded") and None otherwise.
    """
    for cond in k8s_job.conditions:
        if cond.type == "Complete" and cond.status == "True":
            return "SUCCEEDED", None
        if cond.type == "Failed" and cond.status == "True":
            return "FAILED", cond.reason

    # Pending Pod を RUNNING と誤判定しないため status.ready で
    # 「実際にコンテナが Running 中の Pod」の存在を確認する (watcher.md §3)。
    if (
        k8s_job.active and k8s_job.active > 0
        and k8s_job.ready and k8s_job.ready > 0
    ):
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


class NamespacePodNodeResolver:
    """Resolve Pod node names for K8s Jobs with a per-namespace cache.

    reconcile_cycle may need node names for many Jobs in the same namespace
    (RUNNING transition, sweep count changes, completion fallback). Rather
    than calling ``list_namespaced_pod()`` once per Job (N+1), this class
    caches all Pods in a namespace on the first lookup and serves subsequent
    lookups from memory. See watcher.md §5.4.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, list[str]]] = {}

    def resolve(self, namespace: str, k8s_job_name: str) -> list[str]:
        ns_map = self._cache.get(namespace)
        if ns_map is None:
            ns_map = self._fetch_namespace(namespace)
            self._cache[namespace] = ns_map
        return list(ns_map.get(k8s_job_name, []))

    @staticmethod
    def _fetch_namespace(namespace: str) -> dict[str, list[str]]:
        core_v1 = k8s_client.CoreV1Api()
        try:
            pods = core_v1.list_namespaced_pod(
                namespace=namespace,
                label_selector="job-name",
            )
        except ApiException as e:
            logger.warning("Failed to fetch Pods for namespace %s: %s", namespace, e)
            return {}

        result: dict[str, list[str]] = {}
        for pod in pods.items:
            meta = pod.metadata
            spec = pod.spec
            if not meta or not spec or not spec.node_name:
                continue
            labels = meta.labels or {}
            job_name = labels.get("job-name")
            if not job_name:
                continue
            node = spec.node_name
            nodes = result.setdefault(job_name, [])
            if node not in nodes:
                nodes.append(node)
        return result


def _merge_node_names(existing: str | None, new_names: list[str]) -> str | None:
    """Merge new node names into existing comma-separated list.

    Returns a sorted, deduplicated comma-separated string, or None if empty.
    """
    current = set(existing.split(",")) if existing else set()
    current.update(new_names)
    if not current:
        return None
    return ",".join(sorted(current))


def _record_resource_usage(session: Session, job: Job):
    """Add resource usage to namespace_daily_usage on RUNNING transition."""
    parallelism = job.parallelism if job.completions is not None else 1
    delta_cpu = job.time_limit_seconds * job.cpu_millicores * parallelism
    delta_mem = job.time_limit_seconds * job.memory_mib * parallelism
    delta_gpu = job.time_limit_seconds * job.gpu * parallelism

    session.execute(
        text(
            "INSERT INTO namespace_daily_usage "
            "(namespace, usage_date, flavor, "
            "cpu_millicores_seconds, memory_mib_seconds, gpu_seconds) "
            "VALUES (:namespace, CURRENT_DATE, :flavor, "
            ":delta_cpu, :delta_mem, :delta_gpu) "
            "ON CONFLICT (namespace, usage_date, flavor) DO UPDATE SET "
            "cpu_millicores_seconds = namespace_daily_usage.cpu_millicores_seconds "
            "+ :delta_cpu, "
            "memory_mib_seconds = namespace_daily_usage.memory_mib_seconds "
            "+ :delta_mem, "
            "gpu_seconds = namespace_daily_usage.gpu_seconds + :delta_gpu"
        ),
        {
            "namespace": job.namespace,
            "flavor": job.flavor,
            "delta_cpu": delta_cpu,
            "delta_mem": delta_mem,
            "delta_gpu": delta_gpu,
        },
    )


def reconcile_cycle(
    session: Session,
    k8s_jobs: list[LightK8sJob],
    *,
    dispatch_grace_sec: int = 30,
):
    """Run one reconciliation cycle.

    ``dispatch_grace_sec`` is the grace period (seconds) applied to
    Step 8's DISPATCHED-disappearance check. Jobs whose ``dispatched_at``
    is newer than ``NOW() - dispatch_grace_sec`` are spared from the
    FAILED transition to tolerate the reconcile-vs-dispatcher race
    (watcher.md §3 Step 8). Production always passes the value from
    ``Settings.WATCHER_DISPATCH_GRACE_SEC``; the default exists only to
    keep test setup terse.
    """
    # Build lookup: (namespace, job_id) -> lightweight K8s Job snapshot
    k8s_map: dict[tuple[str, int], LightK8sJob] = {
        (kj.namespace, kj.job_id): kj for kj in k8s_jobs
    }

    pod_resolver = NamespacePodNodeResolver()

    # Load only the DB jobs that correspond to observed K8s Jobs
    # (watcher.md §5.3: narrow tuple IN instead of namespace-wide fetch).
    db_jobs: dict[tuple[str, int], Job] = {}
    if k8s_map:
        key_pairs = list(k8s_map.keys())
        jobs = (
            session.query(Job)
            .filter(tuple_(Job.namespace, Job.job_id).in_(key_pairs))
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
        kj_name = kj.name
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
        if db_job.completions is not None:
            new_succeeded = kj.succeeded or 0
            new_failed = kj.failed or 0
            new_completed_indexes = kj.completed_indexes or ""
            new_failed_indexes = kj.failed_indexes or ""
            counts_changed = (db_job.succeeded_count != new_succeeded
                              or db_job.failed_count != new_failed)
            if (counts_changed
                    or db_job.completed_indexes != new_completed_indexes
                    or db_job.failed_indexes != new_failed_indexes):
                db_job.succeeded_count = new_succeeded
                db_job.failed_count = new_failed
                db_job.completed_indexes = new_completed_indexes
                db_job.failed_indexes = new_failed_indexes
                if counts_changed and kj_name:
                    new_names = pod_resolver.resolve(ns, kj_name)
                    db_job.node_name = _merge_node_names(
                        db_job.node_name, new_names
                    )

        # Normal status sync (don't overwrite CANCELLED or DELETING)
        new_status, reason = determine_status(kj)

        # For sweep: K8s Complete + failed_count > 0 → FAILED
        if (db_job.completions is not None
                and new_status == "SUCCEEDED"
                and db_job.failed_count
                and db_job.failed_count > 0):
            new_status = "FAILED"

        if new_status and new_status != db_job.status:
            # Defense-in-depth: reject terminal -> RUNNING regressions.
            # The grace period in Step 8 should prevent the race that
            # triggers this, but if a stale FAILED/SUCCEEDED ever coexists
            # with an active K8s Job, block the rollback instead of
            # producing inconsistent (status=RUNNING, finished_at=...) rows.
            if (db_job.status in ("SUCCEEDED", "FAILED")
                    and new_status == "RUNNING"):
                logger.warning(
                    "Refused status regression %s/%d: %s -> %s "
                    "(stale terminal state vs active K8s Job)",
                    ns, jid, db_job.status, new_status,
                )
                continue
            old_status = db_job.status
            db_job.status = new_status
            if new_status == "RUNNING" and db_job.started_at is None:
                db_job.started_at = func.now()
                if kj_name:
                    new_names = pod_resolver.resolve(ns, kj_name)
                    db_job.node_name = _merge_node_names(
                        db_job.node_name, new_names
                    )
                _record_resource_usage(session, db_job)
            if db_job.node_name is None and new_status in ("SUCCEEDED", "FAILED") \
                    and kj_name:
                new_names = pod_resolver.resolve(ns, kj_name)
                db_job.node_name = _merge_node_names(
                    db_job.node_name, new_names
                )
            if new_status in ("SUCCEEDED", "FAILED"):
                if db_job.started_at is None:
                    _record_resource_usage(session, db_job)
                db_job.finished_at = func.now()
                JOBS_COMPLETED_TOTAL.labels(status=new_status.lower()).inc()
            if new_status == "FAILED" and reason == "DeadlineExceeded":
                db_job.last_error = "time limit exceeded"
            session.add(
                JobEvent(namespace=ns, job_id=jid, event_type=new_status)
            )
            logger.info("Updated %s/%d: %s -> %s", ns, jid, old_status, new_status)

    # Step 8: Mark DISPATCHED/RUNNING jobs with no K8s Job as FAILED.
    # First collect only (namespace, job_id) to avoid loading full Job rows
    # for the common case where no jobs disappeared (watcher.md §5.3).
    #
    # DISPATCHED jobs within the grace period are excluded from the query:
    # the Dispatcher may have created their K8s Job after list_cjob_k8s_jobs()
    # snapshotted the cluster, so their absence from k8s_map is not evidence
    # of disappearance (watcher.md §3 Step 8 dispatcher grace period).
    # Cutoff is computed in Python so the comparison works under both
    # PostgreSQL and SQLite (test fixture) without dialect-specific SQL.
    grace_cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=dispatch_grace_sec
    )
    active_keys = session.execute(
        select(Job.namespace, Job.job_id).where(
            or_(
                Job.status == "RUNNING",
                and_(
                    Job.status == "DISPATCHED",
                    or_(
                        Job.dispatched_at.is_(None),
                        Job.dispatched_at <= grace_cutoff,
                    ),
                ),
            )
        )
    ).all()
    disappeared_keys = [
        (ns, jid) for ns, jid in active_keys if (ns, jid) not in k8s_map
    ]
    if disappeared_keys:
        disappeared_jobs = (
            session.query(Job)
            .filter(tuple_(Job.namespace, Job.job_id).in_(disappeared_keys))
            .all()
        )
        for job in disappeared_jobs:
            job.status = "FAILED"
            job.last_error = (
                "K8s Job not found (TTL expired or manually deleted)"
            )
            job.finished_at = func.now()
            JOBS_COMPLETED_TOTAL.labels(status="failed").inc()
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
