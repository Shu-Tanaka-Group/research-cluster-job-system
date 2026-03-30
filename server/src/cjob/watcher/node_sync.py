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

    tagged_items: list[tuple] = []
    seen_names: set[str] = set()
    successful_queries = 0

    for flavor_def in settings.flavors:
        try:
            nodes = core_v1.list_node(label_selector=flavor_def.label_selector)
        except ApiException as e:
            logger.error(
                "Failed to list nodes for flavor '%s' (selector=%s): %s",
                flavor_def.name, flavor_def.label_selector, e,
            )
            continue

        successful_queries += 1
        for node in nodes.items:
            if node.metadata.name not in seen_names:
                tagged_items.append((node, flavor_def))
                seen_names.add(node.metadata.name)

    if successful_queries == 0 and settings.flavors:
        # All flavor queries failed; preserve existing DB data
        logger.warning("All flavor node queries failed; skipping sync")
        return

    current_nodes: set[str] = set()

    for node, flavor_def in tagged_items:
        name = node.metadata.name
        alloc = node.status.allocatable or {}
        cpu = parse_cpu_millicores(alloc.get("cpu", "0"))
        mem = parse_memory_mib(alloc.get("memory", "0"))
        gpu_resource = flavor_def.gpu_resource_name
        gpu = int(alloc.get(gpu_resource, "0")) if gpu_resource else 0
        current_nodes.add(name)

        session.execute(
            text(
                "INSERT INTO node_resources "
                "(node_name, cpu_millicores, memory_mib, gpu, flavor, updated_at) "
                "VALUES (:name, :cpu, :mem, :gpu, :flavor, NOW()) "
                "ON CONFLICT (node_name) DO UPDATE SET "
                "cpu_millicores = :cpu, memory_mib = :mem, gpu = :gpu, "
                "flavor = :flavor, updated_at = NOW()"
            ),
            {"name": name, "cpu": cpu, "mem": mem, "gpu": gpu, "flavor": flavor_def.name},
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
    selectors = ", ".join(f"{f.name}({f.label_selector})" for f in settings.flavors)
    logger.info(
        "Synced node resources: %d node(s) from %d flavor(s) [%s]",
        len(current_nodes),
        len(settings.flavors),
        selectors,
    )
