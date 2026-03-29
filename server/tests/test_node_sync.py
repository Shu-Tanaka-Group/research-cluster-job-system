from unittest.mock import MagicMock, patch

from sqlalchemy import text

from cjob.config import Settings
from cjob.watcher.node_sync import sync_node_resources


def _make_settings(**overrides):
    defaults = dict(
        POSTGRES_PASSWORD="test",
        NODE_LABEL_SELECTOR="cluster-job=true",
        GPU_NODE_LABEL_SELECTOR="",
        NODE_RESOURCE_SYNC_INTERVAL_SEC=300,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_node(name, cpu="32", memory="128Gi", gpu="0"):
    """Build a mock K8s Node object."""
    node = MagicMock()
    node.metadata.name = name
    node.status.allocatable = {"cpu": cpu, "memory": memory}
    if gpu != "0":
        node.status.allocatable["nvidia.com/gpu"] = gpu
    return node


def _get_node_names(session):
    rows = session.execute(text("SELECT node_name FROM node_resources ORDER BY node_name"))
    return [row[0] for row in rows]


def _get_node(session, name):
    row = session.execute(
        text("SELECT cpu_millicores, memory_mib, gpu FROM node_resources WHERE node_name = :name"),
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

        n2 = _get_node(db_session, "node-2")
        assert n2[0] == 64000
        assert n2[1] == 262144

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

    def test_gpu_parsing(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [
            _make_node("gpu-node", cpu="32", memory="128Gi", gpu="4"),
        ]

        sync_node_resources(db_session, _make_settings())

        n = _get_node(db_session, "gpu-node")
        assert n[2] == 4  # gpu

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

    def test_uses_label_selector(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = []

        settings = _make_settings(NODE_LABEL_SELECTOR="my-label=true")
        sync_node_resources(db_session, settings)

        mock_api.list_node.assert_called_once_with(label_selector="my-label=true")

    def test_gpu_selector_merges_nodes(self, mock_k8s, db_session):
        """GPU_NODE_LABEL_SELECTOR fetches additional nodes and merges them."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        cpu_response = MagicMock()
        cpu_response.items = [_make_node("cpu-node", cpu="32", memory="128Gi")]
        gpu_response = MagicMock()
        gpu_response.items = [_make_node("gpu-node", cpu="16", memory="64Gi", gpu="4")]

        mock_api.list_node.side_effect = [cpu_response, gpu_response]

        settings = _make_settings(GPU_NODE_LABEL_SELECTOR="cluster-gpu-job=true")
        sync_node_resources(db_session, settings)

        assert _get_node_names(db_session) == ["cpu-node", "gpu-node"]
        n = _get_node(db_session, "gpu-node")
        assert n[2] == 4  # gpu

    def test_gpu_selector_deduplicates(self, mock_k8s, db_session):
        """Nodes matching both selectors appear only once."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        shared_node = _make_node("shared-node", cpu="32", memory="128Gi", gpu="2")
        cpu_response = MagicMock()
        cpu_response.items = [shared_node]
        gpu_response = MagicMock()
        gpu_response.items = [shared_node]

        mock_api.list_node.side_effect = [cpu_response, gpu_response]

        settings = _make_settings(GPU_NODE_LABEL_SELECTOR="cluster-gpu-job=true")
        sync_node_resources(db_session, settings)

        assert _get_node_names(db_session) == ["shared-node"]

    def test_gpu_selector_empty_skips_second_query(self, mock_k8s, db_session):
        """Empty GPU_NODE_LABEL_SELECTOR should not make a second list_node call."""
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api
        mock_api.list_node.return_value.items = [_make_node("node-1")]

        settings = _make_settings(GPU_NODE_LABEL_SELECTOR="")
        sync_node_resources(db_session, settings)

        mock_api.list_node.assert_called_once_with(label_selector="cluster-job=true")

    def test_gpu_selector_api_error_still_syncs_cpu_nodes(self, mock_k8s, db_session):
        """GPU API call failure should not prevent CPU node sync."""
        from kubernetes.client.rest import ApiException

        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        cpu_response = MagicMock()
        cpu_response.items = [_make_node("cpu-node", cpu="32", memory="128Gi")]

        mock_api.list_node.side_effect = [
            cpu_response,
            ApiException(status=500, reason="Internal"),
        ]

        settings = _make_settings(GPU_NODE_LABEL_SELECTOR="cluster-gpu-job=true")
        sync_node_resources(db_session, settings)

        assert _get_node_names(db_session) == ["cpu-node"]
