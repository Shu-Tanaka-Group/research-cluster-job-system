import logging

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from sqlalchemy import text
from sqlalchemy.orm import Session

from cjob.config import Settings
from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib

logger = logging.getLogger(__name__)


def sync_resource_quotas(session: Session, settings: Settings):
    """Sync ResourceQuota status from K8s to DB for active namespaces."""
    # Get namespaces with active or queued jobs
    rows = session.execute(
        text(
            "SELECT DISTINCT namespace FROM jobs "
            "WHERE status IN ('QUEUED', 'DISPATCHING', 'DISPATCHED', 'RUNNING', 'HELD')"
        )
    )
    active_namespaces = [row[0] for row in rows]

    if not active_namespaces:
        session.execute(text("DELETE FROM namespace_resource_quotas"))
        session.commit()
        logger.info("No active namespaces; cleared namespace_resource_quotas")
        return

    # Collect GPU resource names from flavor config
    gpu_resource_names: list[str] = []
    for f in settings.flavors:
        if f.gpu_resource_name and f.gpu_resource_name not in gpu_resource_names:
            gpu_resource_names.append(f.gpu_resource_name)

    core_v1 = k8s_client.CoreV1Api()
    synced_namespaces: set[str] = set()

    for ns in active_namespaces:
        try:
            rq = core_v1.read_namespaced_resource_quota(
                name=settings.RESOURCE_QUOTA_NAME,
                namespace=ns,
            )
        except ApiException as e:
            if e.status == 404:
                # No ResourceQuota -> remove row if exists
                session.execute(
                    text(
                        "DELETE FROM namespace_resource_quotas "
                        "WHERE namespace = :ns"
                    ),
                    {"ns": ns},
                )
                synced_namespaces.add(ns)
                continue
            logger.error(
                "Failed to read ResourceQuota '%s' for %s: %s",
                settings.RESOURCE_QUOTA_NAME,
                ns,
                e,
            )
            continue

        hard = rq.spec.hard or {}
        used = (rq.status.used if rq.status else None) or {}

        # Parse GPU from configured resource names
        hard_gpu = 0
        used_gpu = 0
        for gpu_name in gpu_resource_names:
            rq_key = f"requests.{gpu_name}"
            h = int(hard.get(rq_key, "0"))
            u = int(used.get(rq_key, "0"))
            if h > 0:
                hard_gpu = h
                used_gpu = u
                break

        session.execute(
            text(
                "INSERT INTO namespace_resource_quotas "
                "(namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu, "
                "used_cpu_millicores, used_memory_mib, used_gpu, updated_at) "
                "VALUES (:ns, :h_cpu, :h_mem, :h_gpu, :u_cpu, :u_mem, :u_gpu, NOW()) "
                "ON CONFLICT (namespace) DO UPDATE SET "
                "hard_cpu_millicores = :h_cpu, hard_memory_mib = :h_mem, "
                "hard_gpu = :h_gpu, used_cpu_millicores = :u_cpu, "
                "used_memory_mib = :u_mem, used_gpu = :u_gpu, updated_at = NOW()"
            ),
            {
                "ns": ns,
                "h_cpu": parse_cpu_millicores(hard.get("requests.cpu", "0")),
                "h_mem": parse_memory_mib(hard.get("requests.memory", "0")),
                "h_gpu": hard_gpu,
                "u_cpu": parse_cpu_millicores(used.get("requests.cpu", "0")),
                "u_mem": parse_memory_mib(used.get("requests.memory", "0")),
                "u_gpu": used_gpu,
            },
        )
        synced_namespaces.add(ns)

    # Delete rows for namespaces no longer active
    active_set = set(active_namespaces)
    ph = ", ".join(f":n{i}" for i in range(len(active_set)))
    params = {f"n{i}": ns for i, ns in enumerate(active_set)}
    session.execute(
        text(
            f"DELETE FROM namespace_resource_quotas "
            f"WHERE namespace NOT IN ({ph})"
        ),
        params,
    )

    session.commit()
    logger.info(
        "Synced resource quotas: %d namespace(s) synced out of %d active",
        len(synced_namespaces),
        len(active_namespaces),
    )
