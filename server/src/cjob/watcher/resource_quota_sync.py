import logging

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from sqlalchemy import text
from sqlalchemy.orm import Session

from cjob.config import Settings
from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib

logger = logging.getLogger(__name__)


def sync_resource_quotas(session: Session, settings: Settings):
    """Sync ResourceQuota status from K8s to DB for all user namespaces."""
    core_v1 = k8s_client.CoreV1Api()

    # Get all user namespaces via label selector
    try:
        ns_list = core_v1.list_namespace(
            label_selector=settings.USER_NAMESPACE_LABEL,
        )
    except ApiException as e:
        logger.error("Failed to list user namespaces: %s", e)
        return

    user_namespaces = {ns.metadata.name for ns in ns_list.items}

    if not user_namespaces:
        session.execute(text("DELETE FROM namespace_resource_quotas"))
        session.commit()
        logger.info("No user namespaces; cleared namespace_resource_quotas")
        return

    # Collect GPU resource names from flavor config
    gpu_resource_names: list[str] = []
    for f in settings.flavors:
        if f.gpu_resource_name and f.gpu_resource_name not in gpu_resource_names:
            gpu_resource_names.append(f.gpu_resource_name)

    # Fetch all ResourceQuotas named RESOURCE_QUOTA_NAME in a single API call
    try:
        rq_list = core_v1.list_resource_quota_for_all_namespaces(
            field_selector=f"metadata.name={settings.RESOURCE_QUOTA_NAME}",
        )
    except ApiException as e:
        logger.error("Failed to list ResourceQuotas: %s", e)
        return

    # Build namespace -> ResourceQuota mapping (user namespaces only)
    rq_map: dict[str, object] = {}
    for rq in rq_list.items:
        if rq.metadata.namespace in user_namespaces:
            rq_map[rq.metadata.namespace] = rq

    synced_count = 0

    for ns in user_namespaces:
        rq = rq_map.get(ns)
        if rq is None:
            # No ResourceQuota for this namespace -> remove row if exists
            session.execute(
                text(
                    "DELETE FROM namespace_resource_quotas "
                    "WHERE namespace = :ns"
                ),
                {"ns": ns},
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
        synced_count += 1

    # Delete rows for namespaces no longer in user namespace set
    ph = ", ".join(f":n{i}" for i in range(len(user_namespaces)))
    params = {f"n{i}": ns for i, ns in enumerate(user_namespaces)}
    session.execute(
        text(
            f"DELETE FROM namespace_resource_quotas "
            f"WHERE namespace NOT IN ({ph})"
        ),
        params,
    )

    session.commit()
    logger.info(
        "Synced resource quotas: %d namespace(s) with quota out of %d user namespaces",
        synced_count,
        len(user_namespaces),
    )
