import logging

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from sqlalchemy import text
from sqlalchemy.orm import Session

from cjob.config import Settings
from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib

logger = logging.getLogger(__name__)


def sync_node_resources(session: Session, settings: Settings):
    """Fetch node allocatable resources from K8s API and sync to DB."""
    core_v1 = k8s_client.CoreV1Api()

    try:
        nodes = core_v1.list_node(label_selector=settings.NODE_LABEL_SELECTOR)
    except ApiException as e:
        logger.error("Failed to list nodes (selector=%s): %s", settings.NODE_LABEL_SELECTOR, e)
        return

    current_nodes: set[str] = set()

    for node in nodes.items:
        name = node.metadata.name
        alloc = node.status.allocatable or {}
        cpu = parse_cpu_millicores(alloc.get("cpu", "0"))
        mem = parse_memory_mib(alloc.get("memory", "0"))
        gpu = int(alloc.get("nvidia.com/gpu", "0"))
        current_nodes.add(name)

        session.execute(
            text(
                "INSERT INTO node_resources "
                "(node_name, cpu_millicores, memory_mib, gpu, updated_at) "
                "VALUES (:name, :cpu, :mem, :gpu, NOW()) "
                "ON CONFLICT (node_name) DO UPDATE SET "
                "cpu_millicores = :cpu, memory_mib = :mem, gpu = :gpu, "
                "updated_at = NOW()"
            ),
            {"name": name, "cpu": cpu, "mem": mem, "gpu": gpu},
        )

    # Delete nodes that no longer exist in K8s
    if current_nodes:
        # Build parameterised placeholders for the IN clause
        placeholders = ", ".join(f":n{i}" for i in range(len(current_nodes)))
        params = {f"n{i}": name for i, name in enumerate(current_nodes)}
        session.execute(
            text(f"DELETE FROM node_resources WHERE node_name NOT IN ({placeholders})"),
            params,
        )
    else:
        session.execute(text("DELETE FROM node_resources"))

    session.commit()
    logger.info(
        "Synced node resources: %d node(s) from selector '%s'",
        len(current_nodes),
        settings.NODE_LABEL_SELECTOR,
    )
