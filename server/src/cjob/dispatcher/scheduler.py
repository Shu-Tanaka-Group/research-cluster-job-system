import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from cjob.config import Settings
from cjob.metrics import JOBS_COMPLETED_TOTAL
from cjob.models import Job, JobEvent
from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib

logger = logging.getLogger(__name__)


def _cleanup_old_usage(session: Session, settings: Settings):
    """Delete namespace_daily_usage rows outside the retention window."""
    session.execute(
        text(
            "DELETE FROM namespace_daily_usage "
            "WHERE usage_date <= CURRENT_DATE - :retention_days"
        ),
        {"retention_days": settings.USAGE_RETENTION_DAYS},
    )
    session.commit()


def _fetch_flavor_caps(
    session: Session,
) -> dict[str, dict[str, float]]:
    """Fetch per-flavor capacity and DRF weight.

    For each flavor, takes MIN(allocatable total, nominalQuota) as capacity
    and stores drf_weight separately.  If flavor_quotas is empty (Watcher not
    yet synced), falls back to raw allocatable totals with weight 1.0.
    Returns empty dict when node_resources is empty (DRF disabled).
    """
    # Per-flavor allocatable from node_resources
    alloc_rows = session.execute(
        text(
            "SELECT flavor, "
            "  COALESCE(SUM(cpu_millicores), 0) AS total_cpu, "
            "  COALESCE(SUM(memory_mib), 0) AS total_memory, "
            "  COALESCE(SUM(gpu), 0) AS total_gpu "
            "FROM node_resources "
            "GROUP BY flavor"
        )
    ).mappings().all()

    if not alloc_rows:
        return {}

    # Per-flavor nominalQuota and drf_weight from flavor_quotas
    quota_rows = session.execute(
        text("SELECT flavor, cpu, memory, gpu, drf_weight FROM flavor_quotas")
    ).mappings().all()

    quotas: dict[str, dict[str, int | float]] = {}
    for row in quota_rows:
        quotas[row["flavor"]] = {
            "cpu": parse_cpu_millicores(row["cpu"]),
            "mem": parse_memory_mib(row["memory"]),
            "gpu": int(row["gpu"]),
            "weight": float(row["drf_weight"]),
        }

    caps: dict[str, dict[str, float]] = {}
    for row in alloc_rows:
        flavor = row["flavor"]
        alloc_cpu = row["total_cpu"]
        alloc_mem = row["total_memory"]
        alloc_gpu = row["total_gpu"]

        if flavor in quotas:
            caps[flavor] = {
                "cpu": float(min(alloc_cpu, quotas[flavor]["cpu"])),
                "mem": float(min(alloc_mem, quotas[flavor]["mem"])),
                "gpu": float(min(alloc_gpu, quotas[flavor]["gpu"])),
                "weight": quotas[flavor]["weight"],
            }
        else:
            caps[flavor] = {
                "cpu": float(alloc_cpu),
                "mem": float(alloc_mem),
                "gpu": float(alloc_gpu),
                "weight": 1.0,
            }

    return caps


def fetch_dispatchable_jobs(session: Session, settings: Settings) -> list[Job]:
    """Fetch up to batch_size QUEUED jobs, round-robin across namespaces.

    Uses DRF (Dominant Resource Fairness) to prioritise namespaces with
    lower cumulative resource consumption over a sliding window.
    """
    _cleanup_old_usage(session, settings)

    flavor_caps = _fetch_flavor_caps(session)

    # If node_resources is empty (Watcher not yet running), fall back to
    # simple namespace-name ordering without DRF.
    if not flavor_caps:
        logger.debug("node_resources is empty; DRF disabled, using namespace order")
        result = session.execute(
            text(
                "WITH active AS ("
                "  SELECT namespace, flavor, COUNT(*) AS active_count"
                "  FROM jobs"
                "  WHERE status IN ('DISPATCHING', 'DISPATCHED', 'RUNNING')"
                "  GROUP BY namespace, flavor"
                "), "
                "queued AS ("
                "  SELECT *,"
                "    ROW_NUMBER() OVER ("
                "      PARTITION BY namespace ORDER BY created_at ASC"
                "    ) AS rn,"
                "    ROW_NUMBER() OVER ("
                "      PARTITION BY namespace, flavor ORDER BY created_at ASC"
                "    ) AS flavor_rn"
                "  FROM jobs"
                "  WHERE status = 'QUEUED'"
                "    AND (retry_after IS NULL OR retry_after <= NOW())"
                ") "
                "SELECT q.* FROM queued q"
                "  LEFT JOIN active a"
                "    ON q.namespace = a.namespace AND q.flavor = a.flavor"
                "  LEFT JOIN namespace_weights w ON q.namespace = w.namespace "
                "WHERE COALESCE(a.active_count, 0) < :dispatch_limit "
                "  AND q.flavor_rn <= :dispatch_limit - COALESCE(a.active_count, 0) "
                "  AND COALESCE(w.weight, 1) > 0 "
                "ORDER BY CEIL(q.rn * 1.0 / :round_size) ASC, "
                "  q.namespace ASC "
                "LIMIT :fetch_limit"
            ),
            {
                "dispatch_limit": settings.DISPATCH_BUDGET_PER_NAMESPACE,
                "fetch_limit": (
                    settings.DISPATCH_BATCH_SIZE * settings.DISPATCH_FETCH_MULTIPLIER
                ),
                "round_size": settings.DISPATCH_ROUND_SIZE,
            },
        )
    else:
        # Build VALUES clause for per-flavor capacities
        values_parts = []
        params: dict = {
            "dispatch_limit": settings.DISPATCH_BUDGET_PER_NAMESPACE,
            "fetch_limit": (
                settings.DISPATCH_BATCH_SIZE * settings.DISPATCH_FETCH_MULTIPLIER
            ),
            "round_size": settings.DISPATCH_ROUND_SIZE,
            "window_days": settings.FAIR_SHARE_WINDOW_DAYS,
        }
        for i, (flavor, cap) in enumerate(flavor_caps.items()):
            if i == 0:
                # First row needs explicit CAST to define column types
                row = (
                    f"(CAST(:f_{i} AS TEXT), CAST(:cpu_{i} AS FLOAT),"
                    f" CAST(:mem_{i} AS FLOAT), CAST(:gpu_{i} AS FLOAT),"
                    f" CAST(:w_{i} AS FLOAT))"
                )
            else:
                row = f"(:f_{i}, :cpu_{i}, :mem_{i}, :gpu_{i}, :w_{i})"
            values_parts.append(row)
            params[f"f_{i}"] = flavor
            params[f"cpu_{i}"] = cap["cpu"]
            params[f"mem_{i}"] = cap["mem"]
            params[f"gpu_{i}"] = cap["gpu"]
            params[f"w_{i}"] = cap["weight"]
        values_sql = ", ".join(values_parts)

        result = session.execute(
            text(
                f"WITH flavor_caps(flavor, cap_cpu, cap_mem, cap_gpu, w) AS ("
                f"  VALUES {values_sql}"
                f"), "
                "active AS ("
                "  SELECT namespace, flavor, COUNT(*) AS active_count"
                "  FROM jobs"
                "  WHERE status IN ('DISPATCHING', 'DISPATCHED', 'RUNNING')"
                "  GROUP BY namespace, flavor"
                "), "
                "queued AS ("
                "  SELECT *,"
                "    ROW_NUMBER() OVER ("
                "      PARTITION BY namespace ORDER BY created_at ASC"
                "    ) AS rn,"
                "    ROW_NUMBER() OVER ("
                "      PARTITION BY namespace, flavor ORDER BY created_at ASC"
                "    ) AS flavor_rn"
                "  FROM jobs"
                "  WHERE status = 'QUEUED'"
                "    AND (retry_after IS NULL OR retry_after <= NOW())"
                "), "
                "usage AS ("
                "  SELECT u.namespace, u.flavor,"
                "    SUM(u.cpu_millicores_seconds) AS cpu_ms,"
                "    SUM(u.memory_mib_seconds) AS mem_ms,"
                "    SUM(u.gpu_seconds) AS gpu_s"
                "  FROM namespace_daily_usage u"
                "  WHERE u.usage_date > CURRENT_DATE - :window_days"
                "  GROUP BY u.namespace, u.flavor"
                "), "
                "in_flight AS ("
                "  SELECT j.namespace, j.flavor,"
                "    SUM(j.time_limit_seconds::BIGINT * j.cpu_millicores"
                "        * CASE WHEN j.completions IS NOT NULL THEN j.parallelism ELSE 1 END"
                "    ) AS cpu_ms,"
                "    SUM(j.time_limit_seconds::BIGINT * j.memory_mib"
                "        * CASE WHEN j.completions IS NOT NULL THEN j.parallelism ELSE 1 END"
                "    ) AS mem_ms,"
                "    SUM(j.time_limit_seconds::BIGINT * j.gpu"
                "        * CASE WHEN j.completions IS NOT NULL THEN j.parallelism ELSE 1 END"
                "    ) AS gpu_s"
                "  FROM jobs j"
                "  WHERE j.status IN ('DISPATCHING', 'DISPATCHED')"
                "  GROUP BY j.namespace, j.flavor"
                "), "
                "drf_scores AS ("
                "  SELECT nfc.namespace,"
                "    SUM("
                "      GREATEST("
                "        nfc.total_cpu / fc.cap_cpu,"
                "        nfc.total_mem / fc.cap_mem,"
                "        nfc.total_gpu / NULLIF(fc.cap_gpu, 0)"
                "      ) * fc.w"
                "    ) AS drf_score"
                "  FROM ("
                "    SELECT COALESCE(u.namespace, inf.namespace) AS namespace,"
                "           COALESCE(u.flavor, inf.flavor) AS flavor,"
                "           COALESCE(u.cpu_ms, 0) + COALESCE(inf.cpu_ms, 0) AS total_cpu,"
                "           COALESCE(u.mem_ms, 0) + COALESCE(inf.mem_ms, 0) AS total_mem,"
                "           COALESCE(u.gpu_s, 0) + COALESCE(inf.gpu_s, 0) AS total_gpu"
                "    FROM usage u"
                "    FULL OUTER JOIN in_flight inf"
                "      ON u.namespace = inf.namespace AND u.flavor = inf.flavor"
                "  ) nfc"
                "  JOIN flavor_caps fc ON nfc.flavor = fc.flavor"
                "  GROUP BY nfc.namespace"
                ") "
                "SELECT q.* FROM queued q"
                "  LEFT JOIN active a"
                "    ON q.namespace = a.namespace AND q.flavor = a.flavor"
                "  LEFT JOIN drf_scores d ON q.namespace = d.namespace"
                "  LEFT JOIN namespace_weights w ON q.namespace = w.namespace "
                "WHERE COALESCE(a.active_count, 0) < :dispatch_limit "
                "  AND q.flavor_rn <= :dispatch_limit - COALESCE(a.active_count, 0) "
                "  AND COALESCE(w.weight, 1) > 0 "
                "ORDER BY CEIL(q.rn * 1.0 / :round_size) ASC, "
                "  COALESCE(d.drf_score, 0)"
                "    / COALESCE(w.weight, 1) ASC NULLS FIRST, "
                "  q.namespace ASC "
                "LIMIT :fetch_limit"
            ),
            params,
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
        JOBS_COMPLETED_TOTAL.labels(status="failed").inc()
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


def defer_to_queue(
    session: Session, namespace: str, job_id: int, defer_sec: int
) -> bool:
    """Revert DISPATCHING -> QUEUED without consuming the retry budget.

    Used when a dispatch attempt fails due to a transient race that is
    not the job's fault (e.g. stale ResourceQuota cache causing K8s
    admission to reject with 403 exceeded quota). ``retry_count`` is
    intentionally left unchanged; only ``retry_after`` is set so the
    dispatcher waits for the next cache sync before re-attempting.
    See docs/architecture/dispatcher.md §2.5.
    """
    result = session.execute(
        text(
            "UPDATE jobs SET "
            "retry_after = NOW() + MAKE_INTERVAL(secs => :interval), "
            "status = 'QUEUED' "
            "WHERE namespace = :namespace AND job_id = :job_id "
            "AND status = 'DISPATCHING'"
        ),
        {"namespace": namespace, "job_id": job_id, "interval": defer_sec},
    )
    if result.rowcount > 0:
        session.add(
            JobEvent(namespace=namespace, job_id=job_id, event_type="DEFERRED")
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


def estimate_shortest_remaining(
    session: Session, namespace: str, flavor: str
) -> int | None:
    """Estimate the shortest remaining time (seconds) among RUNNING jobs.

    Scoped to the same (namespace, flavor) so that only jobs competing
    for the same resource pool are considered.
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
            "  AND flavor = :flavor "
            "  AND status = 'RUNNING' "
            "  AND started_at IS NOT NULL"
        ),
        {"namespace": namespace, "flavor": flavor},
    )
    row = result.mappings().first()
    if row is None or row["min_remaining"] is None:
        return None
    remaining = int(row["min_remaining"])
    return max(remaining, 0)


def estimate_available_cluster_resources(
    session: Session, settings: Settings
) -> dict[str, dict[str, int]]:
    """Estimate available ClusterQueue resources per flavor.

    Returns a dict mapping flavor name to available {cpu, mem, gpu}.
    Flavors not in flavor_quotas are omitted (treated as unrestricted).
    """
    # Load nominalQuota per flavor from flavor_quotas table
    quota_rows = session.execute(
        text("SELECT flavor, cpu, memory, gpu FROM flavor_quotas")
    ).mappings().all()

    if not quota_rows:
        return {}

    quotas: dict[str, dict[str, int]] = {}
    for row in quota_rows:
        quotas[row["flavor"]] = {
            "cpu": parse_cpu_millicores(row["cpu"]),
            "mem": parse_memory_mib(row["memory"]),
            "gpu": int(row["gpu"]),
        }

    # Sum resources consumed by RUNNING jobs per flavor
    running_rows = session.execute(
        text(
            "SELECT flavor, "
            "  SUM(cpu_millicores"
            "    * CASE WHEN completions IS NOT NULL THEN parallelism ELSE 1 END"
            "  ) AS total_cpu, "
            "  SUM(memory_mib"
            "    * CASE WHEN completions IS NOT NULL THEN parallelism ELSE 1 END"
            "  ) AS total_mem, "
            "  SUM(gpu"
            "    * CASE WHEN completions IS NOT NULL THEN parallelism ELSE 1 END"
            "  ) AS total_gpu "
            "FROM jobs "
            "WHERE status = 'RUNNING' "
            "GROUP BY flavor"
        )
    ).mappings().all()

    running: dict[str, dict[str, int]] = {}
    for row in running_rows:
        running[row["flavor"]] = {
            "cpu": int(row["total_cpu"] or 0),
            "mem": int(row["total_mem"] or 0),
            "gpu": int(row["total_gpu"] or 0),
        }

    available: dict[str, dict[str, int]] = {}
    for flavor, quota in quotas.items():
        used = running.get(flavor, {"cpu": 0, "mem": 0, "gpu": 0})
        available[flavor] = {
            "cpu": max(quota["cpu"] - used["cpu"], 0),
            "mem": max(quota["mem"] - used["mem"], 0),
            "gpu": max(quota["gpu"] - used["gpu"], 0),
        }

    return available


def apply_gap_filling(
    session: Session, candidates: list[Job], settings: Settings
) -> list[Job]:
    """Filter dispatch candidates based on gap filling logic.

    When stalled jobs (DISPATCHED for too long) exist in a namespace,
    only dispatch QUEUED jobs whose time_limit_seconds fits within the
    estimated remaining time of RUNNING jobs AND whose resource
    requirements fit within available ClusterQueue resources.
    """
    if not settings.GAP_FILLING_ENABLED:
        return candidates

    stalled = fetch_stalled_jobs(session, settings.GAP_FILLING_STALL_THRESHOLD_SEC)
    stalled_keys = {(job.namespace, job.flavor) for job in stalled}

    if not stalled_keys:
        return candidates

    available = estimate_available_cluster_resources(session, settings)

    result = [c for c in candidates if (c.namespace, c.flavor) not in stalled_keys]

    for ns, flv in stalled_keys:
        key_candidates = [
            c for c in candidates if c.namespace == ns and c.flavor == flv
        ]
        if not key_candidates:
            continue

        remaining = estimate_shortest_remaining(session, ns, flv)

        for c in key_candidates:
            # Time check: skip if remaining is known and job doesn't fit.
            # When remaining is None (no RUNNING jobs), skip time check
            # to avoid deadlock.
            if remaining is not None and c.time_limit_seconds > remaining:
                logger.debug(
                    "Gap filling: holding %s/%d (time_limit=%ds > remaining=%ds)",
                    ns, c.job_id, c.time_limit_seconds, remaining,
                )
                continue

            # Resource check: skip if job exceeds available cluster resources.
            multiplier = c.parallelism if c.completions is not None else 1
            job_cpu = c.cpu_millicores * multiplier
            job_mem = c.memory_mib * multiplier
            job_gpu = c.gpu * multiplier

            flavor_avail = available.get(c.flavor)
            if flavor_avail is not None:
                if (job_cpu > flavor_avail["cpu"]
                        or job_mem > flavor_avail["mem"]
                        or job_gpu > flavor_avail["gpu"]):
                    logger.debug(
                        "Gap filling: holding %s/%d "
                        "(resource exceeds available for flavor=%s)",
                        ns, c.job_id, c.flavor,
                    )
                    continue
                # Track cumulative dispatch within this pass
                flavor_avail["cpu"] -= job_cpu
                flavor_avail["mem"] -= job_mem
                flavor_avail["gpu"] -= job_gpu

            result.append(c)

    return result


def filter_by_resource_quota(
    session: Session, candidates: list[Job]
) -> list[Job]:
    """Filter dispatch candidates by namespace ResourceQuota remaining capacity.

    Jobs whose resource requirements exceed the remaining ResourceQuota
    are excluded (left in QUEUED). Namespaces without a quota row are
    treated as unrestricted.
    """
    if not candidates:
        return candidates

    # Load quota data for candidate namespaces
    candidate_namespaces = list({c.namespace for c in candidates})
    ph = ", ".join(f":n{i}" for i in range(len(candidate_namespaces)))
    params = {f"n{i}": ns for i, ns in enumerate(candidate_namespaces)}
    rows = session.execute(
        text(
            f"SELECT namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu, "
            f"used_cpu_millicores, used_memory_mib, used_gpu, "
            f"hard_count, used_count "
            f"FROM namespace_resource_quotas WHERE namespace IN ({ph})"
        ),
        params,
    ).mappings().all()

    quota_map = {}
    for row in rows:
        quota_map[row["namespace"]] = {
            "remaining_cpu": row["hard_cpu_millicores"] - row["used_cpu_millicores"],
            "remaining_mem": row["hard_memory_mib"] - row["used_memory_mib"],
            "remaining_gpu": row["hard_gpu"] - row["used_gpu"],
            "remaining_count": (
                row["hard_count"] - row["used_count"]
                if row["hard_count"] is not None
                else None
            ),
        }

    # Track cumulative dispatched resources per namespace within this cycle
    dispatched: dict[str, dict[str, int]] = {}
    result = []

    for job in candidates:
        ns = job.namespace
        if ns not in quota_map:
            result.append(job)
            continue

        remaining = quota_map[ns]
        prev = dispatched.get(ns, {"cpu": 0, "mem": 0, "gpu": 0, "count": 0})

        multiplier = job.parallelism if job.completions is not None else 1
        job_cpu = job.cpu_millicores * multiplier
        job_mem = job.memory_mib * multiplier
        job_gpu = job.gpu * multiplier

        eff_cpu = remaining["remaining_cpu"] - prev["cpu"]
        eff_mem = remaining["remaining_mem"] - prev["mem"]
        eff_gpu = remaining["remaining_gpu"] - prev["gpu"]

        # Job count: always 1 per job (sweep creates 1 K8s Job object)
        remaining_count = remaining["remaining_count"]
        eff_count = (
            remaining_count - prev["count"]
            if remaining_count is not None
            else None
        )

        resource_ok = eff_cpu >= job_cpu and eff_mem >= job_mem and eff_gpu >= job_gpu
        count_ok = eff_count is None or eff_count >= 1

        if resource_ok and count_ok:
            result.append(job)
            dispatched[ns] = {
                "cpu": prev["cpu"] + job_cpu,
                "mem": prev["mem"] + job_mem,
                "gpu": prev["gpu"] + job_gpu,
                "count": prev["count"] + 1,
            }
        else:
            logger.debug(
                "ResourceQuota: skipping %s/%d "
                "(needs cpu=%d mem=%d gpu=%d count=1, "
                "remaining cpu=%d mem=%d gpu=%d count=%s)",
                ns,
                job.job_id,
                job_cpu,
                job_mem,
                job_gpu,
                eff_cpu,
                eff_mem,
                eff_gpu,
                eff_count if eff_count is not None else "unlimited",
            )

    return result


def _build_node_residuals(
    session: Session,
) -> dict[str, list[dict[str, int | str]]]:
    """Build per-flavor list of per-node residual capacities from node_resources.

    Returns a dict mapping flavor name to a list of dicts:
        {"node_name": str, "cpu": int, "mem": int, "gpu": int}
    """
    rows = (
        session.execute(
            text(
                "SELECT node_name, flavor, cpu_millicores, memory_mib, gpu "
                "FROM node_resources "
                "ORDER BY node_name ASC"
            )
        )
        .mappings()
        .all()
    )
    residuals: dict[str, list[dict[str, int | str]]] = {}
    for row in rows:
        residuals.setdefault(row["flavor"], []).append(
            {
                "node_name": row["node_name"],
                "cpu": int(row["cpu_millicores"]),
                "mem": int(row["memory_mib"]),
                "gpu": int(row["gpu"]),
            }
        )
    return residuals


def _find_least_loaded(
    nodes: list[dict[str, int | str]], cpu: int, mem: int, gpu: int
) -> int | None:
    """Return the index of the node with the largest CPU residual that fits.

    Mirrors kube-scheduler's default LeastAllocated scoring (spread). This
    causes virtual placements to spread across nodes, matching what
    kube-scheduler would actually do. Spread placement is also more
    conservative for subsequent candidates within the same cycle than
    best-fit (concentrate), since residuals shrink across all nodes rather
    than draining one node at a time.
    """
    best_idx: int | None = None
    best_remaining: int | None = None
    for i, n in enumerate(nodes):
        if n["cpu"] >= cpu and n["mem"] >= mem and n["gpu"] >= gpu:
            remaining = int(n["cpu"])
            if best_remaining is None or remaining > best_remaining:
                best_remaining = remaining
                best_idx = i
    return best_idx


def _subtract_running_consumption(
    session: Session, residuals: dict[str, list[dict[str, int | str]]]
) -> None:
    """Subtract RUNNING job consumption from per-node residuals.

    Non-sweep: subtract full per-pod resources from the single recorded node.
    Sweep: distribute parallelism pods evenly across the recorded node list
    (floor + remainder). node_name is a cumulative list (see database.md §1),
    so per-node distribution is an approximation but is correct cluster-wide.
    """
    rows = (
        session.execute(
            text(
                "SELECT namespace, job_id, flavor, node_name, cpu_millicores, "
                "memory_mib, gpu, completions, parallelism "
                "FROM jobs "
                "WHERE status = 'RUNNING' AND node_name IS NOT NULL"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        flavor = row["flavor"]
        nodes = residuals.get(flavor)
        if not nodes:
            continue
        node_names = [
            name.strip() for name in (row["node_name"] or "").split(",") if name.strip()
        ]
        if not node_names:
            continue

        is_sweep = row["completions"] is not None
        cpu_pp = int(row["cpu_millicores"] or 0)
        mem_pp = int(row["memory_mib"] or 0)
        gpu_pp = int(row["gpu"] or 0)
        node_map = {n["node_name"]: n for n in nodes}

        if is_sweep:
            parallelism = int(row["parallelism"] or 1)
            base, remainder = divmod(parallelism, len(node_names))
            for i, name in enumerate(node_names):
                pods = base + (1 if i < remainder else 0)
                if pods == 0:
                    continue
                n = node_map.get(name)
                if n is None:
                    continue
                n["cpu"] = max(int(n["cpu"]) - cpu_pp * pods, 0)
                n["mem"] = max(int(n["mem"]) - mem_pp * pods, 0)
                n["gpu"] = max(int(n["gpu"]) - gpu_pp * pods, 0)
        else:
            # Non-sweep: full consumption on the single node. If node_name
            # contains multiple entries (unexpected), apply to the first match.
            for name in node_names:
                n = node_map.get(name)
                if n is None:
                    continue
                n["cpu"] = max(int(n["cpu"]) - cpu_pp, 0)
                n["mem"] = max(int(n["mem"]) - mem_pp, 0)
                n["gpu"] = max(int(n["gpu"]) - gpu_pp, 0)
                break


def _subtract_in_flight_least_loaded(
    session: Session, residuals: dict[str, list[dict[str, int | str]]]
) -> None:
    """Subtract DISPATCHING/DISPATCHED job consumption via least-loaded placement.

    These jobs have not been observed running by Watcher yet, so node_name
    is unknown. Simulate kube-scheduler's default LeastAllocated scoring
    (spread) so that candidates evaluated later see a residual that already
    accounts for in-flight jobs.

    Also handles RUNNING jobs without node_name (e.g. completion fallback in
    watcher.md §3) for consistency.
    """
    rows = (
        session.execute(
            text(
                "SELECT namespace, job_id, flavor, cpu_millicores, memory_mib, gpu, "
                "completions, parallelism "
                "FROM jobs "
                "WHERE status IN ('DISPATCHING', 'DISPATCHED') "
                "   OR (status = 'RUNNING' AND node_name IS NULL) "
                "ORDER BY namespace ASC, job_id ASC"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        flavor = row["flavor"]
        nodes = residuals.get(flavor)
        if not nodes:
            continue
        is_sweep = row["completions"] is not None
        num_pods = int(row["parallelism"] or 1) if is_sweep else 1
        cpu = int(row["cpu_millicores"] or 0)
        mem = int(row["memory_mib"] or 0)
        gpu = int(row["gpu"] or 0)

        for _ in range(num_pods):
            idx = _find_least_loaded(nodes, cpu, mem, gpu)
            if idx is None:
                # No fit; stop. This matches kube-scheduler behaviour where
                # the remaining pods stay Pending until capacity frees up.
                break
            nodes[idx]["cpu"] = int(nodes[idx]["cpu"]) - cpu
            nodes[idx]["mem"] = int(nodes[idx]["mem"]) - mem
            nodes[idx]["gpu"] = int(nodes[idx]["gpu"]) - gpu


def filter_by_node_capacity(
    session: Session, candidates: list[Job], settings: Settings
) -> list[Job]:
    """Filter dispatch candidates by per-node residual capacity.

    For each candidate, simulate bin-packing of its pods (1 for non-sweep,
    parallelism for sweep) onto per-flavor node residuals via least-loaded
    placement (kube-scheduler's default LeastAllocated). A candidate is
    admitted only when ALL of its pods can be placed.

    Residuals are derived from node_resources minus consumption of RUNNING
    jobs (via node_name) and DISPATCHING/DISPATCHED jobs (via least-loaded
    virtual placement). Same-cycle cumulative tracking subtracts admitted
    candidates from the residuals so that later candidates see the updated
    state.

    See docs/architecture/dispatcher.md §2.6.
    """
    if not settings.NODE_BIN_PACKING_ENABLED:
        return candidates
    if not candidates:
        return candidates

    residuals = _build_node_residuals(session)
    if not residuals:
        # node_resources is empty (Watcher not yet running) → unrestricted
        return candidates

    _subtract_running_consumption(session, residuals)
    _subtract_in_flight_least_loaded(session, residuals)

    result: list[Job] = []
    for job in candidates:
        nodes = residuals.get(job.flavor)
        if nodes is None:
            # flavor not present in node_resources → treat as unrestricted
            result.append(job)
            continue

        is_sweep = job.completions is not None
        num_pods = int(job.parallelism or 1) if is_sweep else 1
        cpu = int(job.cpu_millicores or 0)
        mem = int(job.memory_mib or 0)
        gpu = int(job.gpu or 0)

        # Trial placement: try to place all pods via least-loaded on a copy.
        # Only commit to real residuals if all pods fit.
        trial = [dict(n) for n in nodes]
        placements: list[int] = []
        all_fit = True
        for _ in range(num_pods):
            idx = _find_least_loaded(trial, cpu, mem, gpu)
            if idx is None:
                all_fit = False
                break
            trial[idx]["cpu"] = int(trial[idx]["cpu"]) - cpu
            trial[idx]["mem"] = int(trial[idx]["mem"]) - mem
            trial[idx]["gpu"] = int(trial[idx]["gpu"]) - gpu
            placements.append(idx)

        if not all_fit:
            logger.debug(
                "Node bin-packing: skipping %s/%d (pods=%d cpu=%d mem=%d gpu=%d)",
                job.namespace, job.job_id, num_pods, cpu, mem, gpu,
            )
            continue

        result.append(job)
        for idx in placements:
            nodes[idx]["cpu"] = int(nodes[idx]["cpu"]) - cpu
            nodes[idx]["mem"] = int(nodes[idx]["mem"]) - mem
            nodes[idx]["gpu"] = int(nodes[idx]["gpu"]) - gpu

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
