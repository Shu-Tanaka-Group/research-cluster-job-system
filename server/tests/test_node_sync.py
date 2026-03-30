import json
from unittest.mock import MagicMock, patch

from sqlalchemy import text

from cjob.config import Settings
from cjob.watcher.node_sync import sync_node_resources


def _make_settings(**overrides):
    defaults = dict(
        POSTGRES_PASSWORD="test",
        RESOURCE_FLAVORS=json.dumps([
            {"name": "cpu", "label_selector": "cluster-job=true"},
        ]),
        DEFAULT_FLAVOR="cpu",
        NODE_RESOURCE_SYNC_INTERVAL_SEC=300,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_settings_multi_flavor():
    return _make_settings(
        RESOURCE_FLAVORS=json.dumps([
            {"name": "cpu", "label_selector": "cluster-job=true"},
            {"name": "gpu-a100", "label_selector": "cluster-gpu-a100=true", "gpu_resource_name": "nvidia.com/gpu"},
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
        assert calls[0].kwargs["label_selector"] == "cluster-job=true"
        assert calls[1].kwargs["label_selector"] == "cluster-gpu-a100=true"

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

        mock_api.list_node.assert_called_once_with(label_selector="cluster-job=true")

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

    def test_gpu_resource_name_from_flavor_definition(self, mock_k8s, db_session):
        """GPU count is read from the flavor's gpu_resource_name."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        # Flavor with amd.com/gpu resource name
        settings = _make_settings(
            RESOURCE_FLAVORS=json.dumps([
                {"name": "gpu-amd", "label_selector": "cluster-gpu-amd=true", "gpu_resource_name": "amd.com/gpu"},
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
