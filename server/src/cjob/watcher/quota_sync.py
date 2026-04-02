import logging

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from sqlalchemy import text
from sqlalchemy.orm import Session

from cjob.config import Settings

logger = logging.getLogger(__name__)


def sync_flavor_quotas(session: Session, settings: Settings):
    """Fetch ClusterQueue nominalQuota from K8s API and sync to DB."""
    api = k8s_client.CustomObjectsApi()

    try:
        cq = api.get_cluster_custom_object(
            group="kueue.x-k8s.io",
            version="v1beta2",
            plural="clusterqueues",
            name=settings.CLUSTER_QUEUE_NAME,
        )
    except ApiException as e:
        logger.error(
            "Failed to get ClusterQueue '%s': %s",
            settings.CLUSTER_QUEUE_NAME, e,
        )
        return

    resource_groups = cq.get("spec", {}).get("resourceGroups", [])
    if not resource_groups:
        logger.warning("ClusterQueue '%s' has no resourceGroups", settings.CLUSTER_QUEUE_NAME)
        return

    current_flavors: set[str] = set()

    for group in resource_groups:
        for flavor in group.get("flavors", []):
            flavor_name = flavor.get("name", "")
            if not flavor_name:
                continue

            cpu = "0"
            memory = "0"
            gpu = "0"

            for res in flavor.get("resources", []):
                res_name = res.get("name", "")
                nominal = res.get("nominalQuota", "0")
                if res_name == "cpu":
                    cpu = nominal
                elif res_name == "memory":
                    memory = nominal
                else:
                    gpu = nominal

            current_flavors.add(flavor_name)
            session.execute(
                text(
                    "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu, updated_at) "
                    "VALUES (:flavor, :cpu, :memory, :gpu, NOW()) "
                    "ON CONFLICT (flavor) DO UPDATE SET "
                    "cpu = :cpu, memory = :memory, gpu = :gpu, updated_at = NOW()"
                ),
                {"flavor": flavor_name, "cpu": cpu, "memory": memory, "gpu": gpu},
            )

    # Delete flavors that no longer exist in ClusterQueue
    if current_flavors:
        placeholders = ", ".join(f":f{i}" for i in range(len(current_flavors)))
        params = {f"f{i}": name for i, name in enumerate(current_flavors)}
        session.execute(
            text(f"DELETE FROM flavor_quotas WHERE flavor NOT IN ({placeholders})"),
            params,
        )
    else:
        session.execute(text("DELETE FROM flavor_quotas"))

    session.commit()
    logger.info(
        "Synced flavor quotas: %d flavor(s) [%s]",
        len(current_flavors),
        ", ".join(sorted(current_flavors)),
    )
