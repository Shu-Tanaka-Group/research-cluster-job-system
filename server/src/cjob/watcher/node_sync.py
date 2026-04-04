import logging

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from sqlalchemy import text
from sqlalchemy.orm import Session

from cjob.config import Settings
from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib

logger = logging.getLogger(__name__)


def _fetch_daemonset_reservations(core_v1) -> dict[str, tuple[int, int]]:
    """Sum CPU/memory requests of DaemonSet Pods per node.

    Returns a mapping of node_name -> (cpu_millicores, memory_mib) summed
    across all containers of DaemonSet-owned Pods scheduled on that node.
    Raises ApiException if the Pod list API call fails.
    """
    pods = core_v1.list_pod_for_all_namespaces(watch=False)

    by_node: dict[str, tuple[int, int]] = {}
    for pod in pods.items:
        if not pod.spec or not pod.spec.node_name:
            continue
        phase = pod.status.phase if pod.status else None
        if phase not in ("Pending", "Running"):
            continue
        owners = (pod.metadata.owner_references or []) if pod.metadata else []
        if not any(ref.kind == "DaemonSet" for ref in owners):
            continue

        cpu_sum = 0
        mem_sum = 0
        for c in pod.spec.containers or []:
            if not c.resources or not c.resources.requests:
                continue
            req = c.resources.requests
            if "cpu" in req:
                cpu_sum += parse_cpu_millicores(req["cpu"])
            if "memory" in req:
                mem_sum += parse_memory_mib(req["memory"])

        node = pod.spec.node_name
        prev_cpu, prev_mem = by_node.get(node, (0, 0))
        by_node[node] = (prev_cpu + cpu_sum, prev_mem + mem_sum)

    return by_node


def sync_node_resources(session: Session, settings: Settings):
    """Fetch node allocatable resources from K8s API and sync to DB."""
    core_v1 = k8s_client.CoreV1Api()

    tagged_items: list[tuple] = []
    seen_names: set[str] = set()
    successful_queries = 0
    synced_flavors: set[str] = set()

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
        synced_flavors.add(flavor_def.name)
        for node in nodes.items:
            if node.metadata.name not in seen_names:
                tagged_items.append((node, flavor_def))
                seen_names.add(node.metadata.name)

    if successful_queries == 0 and settings.flavors:
        # All flavor queries failed; preserve existing DB data
        logger.warning("All flavor node queries failed; skipping sync")
        return

    try:
        ds_reservations = _fetch_daemonset_reservations(core_v1)
    except ApiException as e:
        logger.error(
            "Failed to list pods for DaemonSet reservation; skipping sync: %s", e,
        )
        return

    current_nodes: set[str] = set()

    for node, flavor_def in tagged_items:
        name = node.metadata.name
        alloc = node.status.allocatable or {}
        cpu_raw = parse_cpu_millicores(alloc.get("cpu", "0"))
        mem_raw = parse_memory_mib(alloc.get("memory", "0"))
        ds_cpu, ds_mem = ds_reservations.get(name, (0, 0))
        cpu = max(0, cpu_raw - ds_cpu)
        mem = max(0, mem_raw - ds_mem)
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

    # Delete stale nodes only for successfully-queried flavors.
    # This preserves DB data for flavors whose K8s API queries failed.
    if synced_flavors:
        flavor_ph = ", ".join(f":f{i}" for i in range(len(synced_flavors)))
        flavor_params = {f"f{i}": name for i, name in enumerate(synced_flavors)}

        if current_nodes:
            node_ph = ", ".join(f":n{i}" for i in range(len(current_nodes)))
            node_params = {f"n{i}": name for i, name in enumerate(current_nodes)}
            session.execute(
                text(
                    f"DELETE FROM node_resources "
                    f"WHERE flavor IN ({flavor_ph}) "
                    f"AND node_name NOT IN ({node_ph})"
                ),
                {**flavor_params, **node_params},
            )
        else:
            session.execute(
                text(f"DELETE FROM node_resources WHERE flavor IN ({flavor_ph})"),
                flavor_params,
            )

    session.commit()
    selectors = ", ".join(f"{f.name}({f.label_selector})" for f in settings.flavors)
    logger.info(
        "Synced node resources: %d node(s) from %d flavor(s) [%s]",
        len(current_nodes),
        len(settings.flavors),
        selectors,
    )
