import json
from unittest.mock import MagicMock, patch

from sqlalchemy import text

from cjob.config import Settings
from cjob.watcher.node_sync import sync_node_resources


def _make_settings(**overrides):
    defaults = dict(
        POSTGRES_PASSWORD="test",
        RESOURCE_FLAVORS=json.dumps([
            {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
        ]),
        DEFAULT_FLAVOR="cpu",
        NODE_RESOURCE_SYNC_INTERVAL_SEC=300,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_settings_multi_flavor():
    return _make_settings(
        RESOURCE_FLAVORS=json.dumps([
            {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
            {"name": "gpu-a100", "label_selector": "cjob.io/flavor=gpu-a100", "gpu_resource_name": "nvidia.com/gpu"},
        ]),
    )


def _make_node(name, cpu="32", memory="128Gi", gpu_resources=None):
    """Build a mock K8s Node object.

    gpu_resources: dict of resource_name -> value, e.g. {"nvidia.com/gpu": "4"}
    """
    node = MagicMock()
    node.metadata.name = name
    node.status.allocatable = {"cpu": cpu, "memory": memory}
    if gpu_resources:
        node.status.allocatable.update(gpu_resources)
    return node


def _make_pod(node_name, owner_kind="DaemonSet", phase="Running",
              container_requests=None):
    """Build a mock K8s Pod object.

    container_requests: list of dicts, one per container, e.g.
        [{"cpu": "500m", "memory": "256Mi"}, {"cpu": "100m"}].
        Pass None for no containers; pass [] for a container list with no
        requests set.
    """
    pod = MagicMock()
    pod.spec.node_name = node_name
    pod.status.phase = phase

    if owner_kind is None:
        pod.metadata.owner_references = []
    else:
        owner = MagicMock()
        owner.kind = owner_kind
        pod.metadata.owner_references = [owner]

    containers = []
    if container_requests is not None:
        for req in container_requests:
            c = MagicMock()
            c.resources.requests = req
            containers.append(c)
    pod.spec.containers = containers
    return pod


def _pod_list(*pods):
    resp = MagicMock()
    resp.items = list(pods)
    return resp


def _no_pods():
    return _pod_list()


def _get_node_names(session):
    rows = session.execute(text("SELECT node_name FROM node_resources ORDER BY node_name"))
    return [row[0] for row in rows]


def _get_node(session, name):
    row = session.execute(
        text("SELECT cpu_millicores, memory_mib, gpu, flavor FROM node_resources WHERE node_name = :name"),
        {"name": name},
    ).first()
    return row


@patch("cjob.watcher.node_sync.k8s_client")
class TestSyncNodeResources:
    def test_inserts_new_nodes(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
            _make_node("node-2", cpu="64", memory="256Gi"),
        ]

        sync_node_resources(db_session, _make_settings())

        assert _get_node_names(db_session) == ["node-1", "node-2"]
        n1 = _get_node(db_session, "node-1")
        assert n1[0] == 32000  # cpu_millicores
        assert n1[1] == 131072  # memory_mib
        assert n1[2] == 0  # gpu
        assert n1[3] == "cpu"  # flavor

        n2 = _get_node(db_session, "node-2")
        assert n2[0] == 64000
        assert n2[1] == 262144
        assert n2[3] == "cpu"  # flavor

    def test_updates_existing_nodes(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        # First sync
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
        ]
        sync_node_resources(db_session, _make_settings())

        # Second sync with changed values
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="64", memory="256Gi"),
        ]
        sync_node_resources(db_session, _make_settings())

        n1 = _get_node(db_session, "node-1")
        assert n1[0] == 64000
        assert n1[1] == 262144

    def test_deletes_removed_nodes(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        # First sync: two nodes
        mock_api.list_node.return_value.items = [
            _make_node("node-1"),
            _make_node("node-2"),
        ]
        sync_node_resources(db_session, _make_settings())
        assert len(_get_node_names(db_session)) == 2

        # Second sync: node-2 removed
        mock_api.list_node.return_value.items = [
            _make_node("node-1"),
        ]
        sync_node_resources(db_session, _make_settings())
        assert _get_node_names(db_session) == ["node-1"]

    def test_deletes_all_when_no_nodes(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        # First sync: one node
        mock_api.list_node.return_value.items = [_make_node("node-1")]
        sync_node_resources(db_session, _make_settings())
        assert len(_get_node_names(db_session)) == 1

        # Second sync: no nodes
        mock_api.list_node.return_value.items = []
        sync_node_resources(db_session, _make_settings())
        assert _get_node_names(db_session) == []

    def test_api_error_does_not_clear_db(self, mock_k8s, db_session):
        """K8s API failure should leave existing data intact."""
        from kubernetes.client.rest import ApiException

        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        # First sync succeeds
        mock_api.list_node.return_value.items = [_make_node("node-1")]
        sync_node_resources(db_session, _make_settings())
        assert len(_get_node_names(db_session)) == 1

        # Second sync fails
        mock_api.list_node.side_effect = ApiException(status=500, reason="Internal")
        sync_node_resources(db_session, _make_settings())

        # Data should remain
        assert _get_node_names(db_session) == ["node-1"]

    def test_uses_flavor_label_selectors(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        cpu_response = MagicMock()
        cpu_response.items = []
        gpu_response = MagicMock()
        gpu_response.items = []
        mock_api.list_node.side_effect = [cpu_response, gpu_response]

        sync_node_resources(db_session, _make_settings_multi_flavor())

        calls = mock_api.list_node.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["label_selector"] == "cjob.io/flavor=cpu"
        assert calls[1].kwargs["label_selector"] == "cjob.io/flavor=gpu-a100"

    def test_multi_flavor_merges_nodes(self, mock_k8s, db_session):
        """Multiple flavors fetch nodes with correct flavor tags."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        cpu_response = MagicMock()
        cpu_response.items = [_make_node("cpu-node", cpu="32", memory="128Gi")]
        gpu_response = MagicMock()
        gpu_response.items = [_make_node("gpu-node", cpu="16", memory="64Gi", gpu_resources={"nvidia.com/gpu": "4"})]

        mock_api.list_node.side_effect = [cpu_response, gpu_response]

        sync_node_resources(db_session, _make_settings_multi_flavor())

        assert _get_node_names(db_session) == ["cpu-node", "gpu-node"]
        cpu_n = _get_node(db_session, "cpu-node")
        assert cpu_n[3] == "cpu"  # flavor
        gpu_n = _get_node(db_session, "gpu-node")
        assert gpu_n[2] == 4  # gpu
        assert gpu_n[3] == "gpu-a100"  # flavor

    def test_multi_flavor_deduplicates(self, mock_k8s, db_session):
        """Nodes matching both flavors appear only once (first flavor wins)."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        shared_node = _make_node("shared-node", cpu="32", memory="128Gi", gpu_resources={"nvidia.com/gpu": "2"})
        cpu_response = MagicMock()
        cpu_response.items = [shared_node]
        gpu_response = MagicMock()
        gpu_response.items = [shared_node]

        mock_api.list_node.side_effect = [cpu_response, gpu_response]

        sync_node_resources(db_session, _make_settings_multi_flavor())

        assert _get_node_names(db_session) == ["shared-node"]
        n = _get_node(db_session, "shared-node")
        assert n[3] == "cpu"  # first flavor wins

    def test_single_flavor_only_one_query(self, mock_k8s, db_session):
        """Single flavor settings should make only one list_node call."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [_make_node("node-1")]

        sync_node_resources(db_session, _make_settings())

        mock_api.list_node.assert_called_once_with(label_selector="cjob.io/flavor=cpu")

    def test_flavor_api_error_still_syncs_other_flavors(self, mock_k8s, db_session):
        """API failure for one flavor should not prevent other flavor sync."""
        from kubernetes.client.rest import ApiException

        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        cpu_response = MagicMock()
        cpu_response.items = [_make_node("cpu-node", cpu="32", memory="128Gi")]

        mock_api.list_node.side_effect = [
            cpu_response,
            ApiException(status=500, reason="Internal"),
        ]

        sync_node_resources(db_session, _make_settings_multi_flavor())

        assert _get_node_names(db_session) == ["cpu-node"]

    def test_partial_failure_preserves_failed_flavor_nodes(self, mock_k8s, db_session):
        """When one flavor query fails, its previously-synced nodes are preserved."""
        from kubernetes.client.rest import ApiException

        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        # First sync: both flavors succeed
        cpu_response = MagicMock()
        cpu_response.items = [_make_node("cpu-node", cpu="32", memory="128Gi")]
        gpu_response = MagicMock()
        gpu_response.items = [_make_node("gpu-node", cpu="16", memory="64Gi", gpu_resources={"nvidia.com/gpu": "4"})]

        mock_api.list_node.side_effect = [cpu_response, gpu_response]
        sync_node_resources(db_session, _make_settings_multi_flavor())
        assert sorted(_get_node_names(db_session)) == ["cpu-node", "gpu-node"]

        # Second sync: cpu succeeds, gpu fails
        cpu_response2 = MagicMock()
        cpu_response2.items = [_make_node("cpu-node", cpu="32", memory="128Gi")]
        mock_api.list_node.side_effect = [
            cpu_response2,
            ApiException(status=500, reason="Internal"),
        ]
        sync_node_resources(db_session, _make_settings_multi_flavor())

        # Both nodes should still exist (gpu-node preserved from previous sync)
        assert sorted(_get_node_names(db_session)) == ["cpu-node", "gpu-node"]
        gpu_n = _get_node(db_session, "gpu-node")
        assert gpu_n[3] == "gpu-a100"  # flavor preserved

    def test_partial_failure_deletes_stale_nodes_in_successful_flavor(self, mock_k8s, db_session):
        """Stale nodes from a successful flavor are still removed during partial failure."""
        from kubernetes.client.rest import ApiException

        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        # First sync: cpu has two nodes, gpu has one node
        cpu_response = MagicMock()
        cpu_response.items = [
            _make_node("cpu-node-1", cpu="32", memory="128Gi"),
            _make_node("cpu-node-2", cpu="32", memory="128Gi"),
        ]
        gpu_response = MagicMock()
        gpu_response.items = [_make_node("gpu-node", cpu="16", memory="64Gi", gpu_resources={"nvidia.com/gpu": "4"})]
        mock_api.list_node.side_effect = [cpu_response, gpu_response]
        sync_node_resources(db_session, _make_settings_multi_flavor())
        assert len(_get_node_names(db_session)) == 3

        # Second sync: cpu-node-2 removed, gpu query fails
        cpu_response2 = MagicMock()
        cpu_response2.items = [_make_node("cpu-node-1", cpu="32", memory="128Gi")]
        mock_api.list_node.side_effect = [
            cpu_response2,
            ApiException(status=500, reason="Internal"),
        ]
        sync_node_resources(db_session, _make_settings_multi_flavor())

        # cpu-node-2 should be deleted, gpu-node preserved
        assert sorted(_get_node_names(db_session)) == ["cpu-node-1", "gpu-node"]

    def test_gpu_resource_name_from_flavor_definition(self, mock_k8s, db_session):
        """GPU count is read from the flavor's gpu_resource_name."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        # Flavor with amd.com/gpu resource name
        settings = _make_settings(
            RESOURCE_FLAVORS=json.dumps([
                {"name": "gpu-amd", "label_selector": "cjob.io/flavor=gpu-amd", "gpu_resource_name": "amd.com/gpu"},
            ]),
        )

        mock_api.list_node.return_value.items = [
            _make_node("amd-node", cpu="32", memory="128Gi", gpu_resources={"amd.com/gpu": "2"}),
        ]

        sync_node_resources(db_session, settings)

        n = _get_node(db_session, "amd-node")
        assert n[2] == 2  # gpu count from amd.com/gpu
        assert n[3] == "gpu-amd"  # flavor

    def test_no_gpu_resource_name_records_zero_gpu(self, mock_k8s, db_session):
        """Flavor without gpu_resource_name records gpu=0 even if node has GPUs."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        # CPU flavor (no gpu_resource_name) on a node that has GPUs
        mock_api.list_node.return_value.items = [
            _make_node("node-with-gpu", cpu="32", memory="128Gi", gpu_resources={"nvidia.com/gpu": "4"}),
        ]

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "node-with-gpu")
        assert n[2] == 0  # gpu should be 0 because cpu flavor has no gpu_resource_name

    def test_subtracts_daemonset_pod_requests(self, mock_k8s, db_session):
        """DaemonSet Pod CPU/memory requests are subtracted from allocatable."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("node-1", container_requests=[{"cpu": "500m", "memory": "256Mi"}]),
        )

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "node-1")
        assert n[0] == 32000 - 500  # 32 cores - 500m
        assert n[1] == 131072 - 256  # 128Gi - 256Mi

    def test_sums_multiple_daemonset_pods_per_node(self, mock_k8s, db_session):
        """Multiple DaemonSet Pods on the same node are summed together."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("node-1", container_requests=[{"cpu": "500m", "memory": "256Mi"}]),
            _make_pod("node-1", container_requests=[{"cpu": "200m", "memory": "128Mi"}]),
            _make_pod("node-1", container_requests=[{"cpu": "100m", "memory": "64Mi"}]),
        )

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "node-1")
        assert n[0] == 32000 - 800  # 500+200+100
        assert n[1] == 131072 - 448  # 256+128+64

    def test_sums_multiple_containers_in_daemonset_pod(self, mock_k8s, db_session):
        """Sums requests across all containers in a single DaemonSet Pod."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("node-1", container_requests=[
                {"cpu": "500m", "memory": "256Mi"},
                {"cpu": "250m", "memory": "128Mi"},
            ]),
        )

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "node-1")
        assert n[0] == 32000 - 750
        assert n[1] == 131072 - 384

    def test_ignores_non_daemonset_pods(self, mock_k8s, db_session):
        """Pods owned by non-DaemonSet controllers are not subtracted."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("node-1", owner_kind="ReplicaSet",
                      container_requests=[{"cpu": "1000m", "memory": "1Gi"}]),
            _make_pod("node-1", owner_kind="Job",
                      container_requests=[{"cpu": "2000m", "memory": "2Gi"}]),
            _make_pod("node-1", owner_kind="StatefulSet",
                      container_requests=[{"cpu": "500m", "memory": "512Mi"}]),
        )

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "node-1")
        assert n[0] == 32000  # untouched
        assert n[1] == 131072  # untouched

    def test_ignores_pods_without_owner_references(self, mock_k8s, db_session):
        """Pods without owner references are not subtracted (bare Pods)."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("node-1", owner_kind=None,
                      container_requests=[{"cpu": "1000m", "memory": "1Gi"}]),
        )

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "node-1")
        assert n[0] == 32000
        assert n[1] == 131072

    def test_ignores_terminated_daemonset_pods(self, mock_k8s, db_session):
        """DaemonSet Pods in Succeeded/Failed/Unknown phase are not subtracted."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("node-1", phase="Succeeded",
                      container_requests=[{"cpu": "500m", "memory": "256Mi"}]),
            _make_pod("node-1", phase="Failed",
                      container_requests=[{"cpu": "500m", "memory": "256Mi"}]),
            _make_pod("node-1", phase="Unknown",
                      container_requests=[{"cpu": "500m", "memory": "256Mi"}]),
        )

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "node-1")
        assert n[0] == 32000
        assert n[1] == 131072

    def test_counts_pending_daemonset_pods(self, mock_k8s, db_session):
        """DaemonSet Pods in Pending phase are counted (already scheduled)."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("node-1", phase="Pending",
                      container_requests=[{"cpu": "500m", "memory": "256Mi"}]),
        )

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "node-1")
        assert n[0] == 32000 - 500
        assert n[1] == 131072 - 256

    def test_daemonset_pod_without_requests(self, mock_k8s, db_session):
        """Containers without requests set contribute 0 to the subtraction."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            # Pod with a container that has no requests at all
            _make_pod("node-1", container_requests=[{}]),
            # Pod with only cpu request (no memory)
            _make_pod("node-1", container_requests=[{"cpu": "300m"}]),
        )

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "node-1")
        assert n[0] == 32000 - 300  # only the 300m cpu is subtracted
        assert n[1] == 131072  # memory untouched

    def test_effective_allocatable_clamps_at_zero(self, mock_k8s, db_session):
        """Subtraction never produces negative effective allocatable values."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="1", memory="100Mi"),
        ]
        # Request more than the node has
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("node-1", container_requests=[{"cpu": "5", "memory": "1Gi"}]),
        )

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "node-1")
        assert n[0] == 0
        assert n[1] == 0

    def test_daemonset_across_multiple_nodes(self, mock_k8s, db_session):
        """Each node is adjusted independently based on its own DaemonSet Pods."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
            _make_node("node-2", cpu="64", memory="256Gi"),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("node-1", container_requests=[{"cpu": "500m", "memory": "256Mi"}]),
            _make_pod("node-2", container_requests=[{"cpu": "1000m", "memory": "512Mi"}]),
            _make_pod("node-2", container_requests=[{"cpu": "200m", "memory": "128Mi"}]),
        )

        sync_node_resources(db_session, _make_settings())

        n1 = _get_node(db_session, "node-1")
        assert n1[0] == 32000 - 500
        assert n1[1] == 131072 - 256

        n2 = _get_node(db_session, "node-2")
        assert n2[0] == 64000 - 1200
        assert n2[1] == 262144 - 640

    def test_pod_list_api_error_preserves_db(self, mock_k8s, db_session):
        """If list_pod_for_all_namespaces fails, existing DB data is preserved."""
        from kubernetes.client.rest import ApiException

        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        # First sync succeeds (no DaemonSet Pods)
        mock_api.list_node.return_value.items = [
            _make_node("node-1", cpu="32", memory="128Gi"),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _no_pods()
        sync_node_resources(db_session, _make_settings())
        n = _get_node(db_session, "node-1")
        assert n[0] == 32000

        # Second sync: node list succeeds but pod list fails
        mock_api.list_pod_for_all_namespaces.side_effect = ApiException(
            status=500, reason="Internal"
        )
        sync_node_resources(db_session, _make_settings())

        # Existing data is preserved (not overwritten with raw allocatable)
        n = _get_node(db_session, "node-1")
        assert n[0] == 32000

    def test_gpu_is_not_adjusted_by_daemonset_pods(self, mock_k8s, db_session):
        """GPU count is not reduced even if a DaemonSet Pod requests GPU."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        settings = _make_settings(
            RESOURCE_FLAVORS=json.dumps([
                {"name": "gpu", "label_selector": "cjob.io/flavor=gpu",
                 "gpu_resource_name": "nvidia.com/gpu"},
            ]),
            DEFAULT_FLAVOR="gpu",
        )
        mock_api.list_node.return_value.items = [
            _make_node("gpu-node", cpu="32", memory="128Gi",
                       gpu_resources={"nvidia.com/gpu": "4"}),
        ]
        mock_api.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("gpu-node", container_requests=[
                {"cpu": "100m", "memory": "64Mi", "nvidia.com/gpu": "1"},
            ]),
        )

        sync_node_resources(db_session, settings)

        n = _get_node(db_session, "gpu-node")
        assert n[0] == 32000 - 100
        assert n[1] == 131072 - 64
        assert n[2] == 4  # GPU not reduced
