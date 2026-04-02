from unittest.mock import MagicMock, patch

from sqlalchemy import text

from cjob.config import Settings
from cjob.watcher.quota_sync import sync_flavor_quotas


def _make_settings(**overrides):
    defaults = dict(
        POSTGRES_PASSWORD="test",
        RESOURCE_FLAVORS='[{"name": "cpu", "label_selector": "cluster-job=true"}]',
        DEFAULT_FLAVOR="cpu",
        CLUSTER_QUEUE_NAME="cjob-cluster-queue",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_cluster_queue(flavors):
    """Build a mock ClusterQueue dict.

    flavors: list of (name, {resource_name: nominalQuota}) tuples.
    Example: [("cpu", {"cpu": "256", "memory": "1000Gi", "nvidia.com/gpu": "0"})]
    """
    flavor_entries = []
    for name, resources in flavors:
        resource_entries = [
            {"name": res_name, "nominalQuota": quota}
            for res_name, quota in resources.items()
        ]
        flavor_entries.append({"name": name, "resources": resource_entries})

    return {
        "spec": {
            "resourceGroups": [
                {
                    "coveredResources": ["cpu", "memory", "nvidia.com/gpu"],
                    "flavors": flavor_entries,
                }
            ]
        }
    }


def _get_quota(session, flavor):
    row = session.execute(
        text("SELECT cpu, memory, gpu FROM flavor_quotas WHERE flavor = :flavor"),
        {"flavor": flavor},
    ).first()
    return row


def _get_all_flavors(session):
    rows = session.execute(
        text("SELECT flavor FROM flavor_quotas ORDER BY flavor")
    )
    return [row[0] for row in rows]


@patch("cjob.watcher.quota_sync.k8s_client")
class TestSyncFlavorQuotas:
    def test_inserts_new_quotas(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CustomObjectsApi.return_value = mock_api
        mock_api.get_cluster_custom_object.return_value = _make_cluster_queue([
            ("cpu", {"cpu": "256", "memory": "1000Gi", "nvidia.com/gpu": "0"}),
        ])

        sync_flavor_quotas(db_session, _make_settings())

        q = _get_quota(db_session, "cpu")
        assert q[0] == "256"      # cpu
        assert q[1] == "1000Gi"   # memory
        assert q[2] == "0"        # gpu

    def test_inserts_multi_flavor(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CustomObjectsApi.return_value = mock_api
        mock_api.get_cluster_custom_object.return_value = _make_cluster_queue([
            ("cpu", {"cpu": "256", "memory": "1000Gi", "nvidia.com/gpu": "0"}),
            ("gpu-a100", {"cpu": "64", "memory": "500Gi", "nvidia.com/gpu": "4"}),
        ])

        sync_flavor_quotas(db_session, _make_settings())

        assert _get_all_flavors(db_session) == ["cpu", "gpu-a100"]
        gpu_q = _get_quota(db_session, "gpu-a100")
        assert gpu_q[0] == "64"
        assert gpu_q[1] == "500Gi"
        assert gpu_q[2] == "4"

    def test_updates_existing_quotas(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CustomObjectsApi.return_value = mock_api

        # First sync
        mock_api.get_cluster_custom_object.return_value = _make_cluster_queue([
            ("cpu", {"cpu": "128", "memory": "500Gi", "nvidia.com/gpu": "0"}),
        ])
        sync_flavor_quotas(db_session, _make_settings())

        # Second sync with updated values
        mock_api.get_cluster_custom_object.return_value = _make_cluster_queue([
            ("cpu", {"cpu": "256", "memory": "1000Gi", "nvidia.com/gpu": "0"}),
        ])
        sync_flavor_quotas(db_session, _make_settings())

        q = _get_quota(db_session, "cpu")
        assert q[0] == "256"
        assert q[1] == "1000Gi"

    def test_deletes_removed_flavors(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CustomObjectsApi.return_value = mock_api

        # First sync: two flavors
        mock_api.get_cluster_custom_object.return_value = _make_cluster_queue([
            ("cpu", {"cpu": "256", "memory": "1000Gi", "nvidia.com/gpu": "0"}),
            ("gpu-a100", {"cpu": "64", "memory": "500Gi", "nvidia.com/gpu": "4"}),
        ])
        sync_flavor_quotas(db_session, _make_settings())
        assert len(_get_all_flavors(db_session)) == 2

        # Second sync: gpu-a100 removed
        mock_api.get_cluster_custom_object.return_value = _make_cluster_queue([
            ("cpu", {"cpu": "256", "memory": "1000Gi", "nvidia.com/gpu": "0"}),
        ])
        sync_flavor_quotas(db_session, _make_settings())
        assert _get_all_flavors(db_session) == ["cpu"]

    def test_api_error_preserves_existing_data(self, mock_k8s, db_session):
        from kubernetes.client.rest import ApiException

        mock_api = MagicMock()
        mock_k8s.CustomObjectsApi.return_value = mock_api

        # First sync succeeds
        mock_api.get_cluster_custom_object.return_value = _make_cluster_queue([
            ("cpu", {"cpu": "256", "memory": "1000Gi", "nvidia.com/gpu": "0"}),
        ])
        sync_flavor_quotas(db_session, _make_settings())
        assert len(_get_all_flavors(db_session)) == 1

        # Second sync fails
        mock_api.get_cluster_custom_object.side_effect = ApiException(
            status=500, reason="Internal"
        )
        sync_flavor_quotas(db_session, _make_settings())

        # Data should remain
        assert _get_all_flavors(db_session) == ["cpu"]

    def test_empty_resource_groups_preserves_data(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CustomObjectsApi.return_value = mock_api

        # First sync succeeds
        mock_api.get_cluster_custom_object.return_value = _make_cluster_queue([
            ("cpu", {"cpu": "256", "memory": "1000Gi", "nvidia.com/gpu": "0"}),
        ])
        sync_flavor_quotas(db_session, _make_settings())

        # Second sync returns empty resourceGroups
        mock_api.get_cluster_custom_object.return_value = {"spec": {"resourceGroups": []}}
        sync_flavor_quotas(db_session, _make_settings())

        # Data should remain (early return before DELETE)
        assert _get_all_flavors(db_session) == ["cpu"]

    def test_uses_cluster_queue_name_from_settings(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CustomObjectsApi.return_value = mock_api
        mock_api.get_cluster_custom_object.return_value = _make_cluster_queue([
            ("cpu", {"cpu": "256", "memory": "1000Gi", "nvidia.com/gpu": "0"}),
        ])

        settings = _make_settings(CLUSTER_QUEUE_NAME="my-custom-queue")
        sync_flavor_quotas(db_session, settings)

        call_kwargs = mock_api.get_cluster_custom_object.call_args.kwargs
        assert call_kwargs["name"] == "my-custom-queue"
