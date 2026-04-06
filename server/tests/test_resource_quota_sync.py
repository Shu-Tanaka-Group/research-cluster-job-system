import json
from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException
from sqlalchemy import text

from cjob.config import Settings
from cjob.watcher.resource_quota_sync import sync_resource_quotas


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


def _make_settings_with_gpu():
    return _make_settings(
        RESOURCE_FLAVORS=json.dumps([
            {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
            {"name": "gpu-a100", "label_selector": "cjob.io/flavor=gpu-a100", "gpu_resource_name": "nvidia.com/gpu"},
        ]),
    )


def _make_namespace(name):
    """Build a mock K8s Namespace object."""
    ns = MagicMock()
    ns.metadata.name = name
    return ns


def _make_ns_list(names):
    """Build a mock K8s NamespaceList."""
    ns_list = MagicMock()
    ns_list.items = [_make_namespace(n) for n in names]
    return ns_list


def _make_resource_quota(namespace, hard, used):
    """Build a mock K8s ResourceQuota object."""
    rq = MagicMock()
    rq.metadata.namespace = namespace
    rq.spec.hard = hard
    rq.status.used = used
    return rq


def _make_rq_list(items):
    """Build a mock K8s ResourceQuotaList."""
    rq_list = MagicMock()
    rq_list.items = items
    return rq_list


def _get_quota_row(session, namespace):
    return session.execute(
        text(
            "SELECT hard_cpu_millicores, hard_memory_mib, hard_gpu, "
            "used_cpu_millicores, used_memory_mib, used_gpu, "
            "hard_count, used_count "
            "FROM namespace_resource_quotas WHERE namespace = :ns"
        ),
        {"ns": namespace},
    ).first()


def _get_all_namespaces(session):
    rows = session.execute(
        text("SELECT namespace FROM namespace_resource_quotas ORDER BY namespace")
    )
    return [row[0] for row in rows]


def _setup_mock_api(mock_k8s, user_namespaces, rq_items):
    """Set up mock CoreV1Api with namespace list and ResourceQuota list."""
    mock_api = MagicMock()
    mock_k8s.CoreV1Api.return_value = mock_api
    mock_api.list_namespace.return_value = _make_ns_list(user_namespaces)
    mock_api.list_resource_quota_for_all_namespaces.return_value = _make_rq_list(rq_items)
    return mock_api


@patch("cjob.watcher.resource_quota_sync.k8s_client")
class TestSyncResourceQuotas:
    def test_inserts_for_user_namespaces(self, mock_k8s, db_session):
        _setup_mock_api(mock_k8s, ["user-alice", "user-bob"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
            _make_resource_quota(
                "user-bob",
                {"requests.cpu": "100", "requests.memory": "500Gi"},
                {"requests.cpu": "50", "requests.memory": "200Gi"},
            ),
        ])

        sync_resource_quotas(db_session, _make_settings())

        assert _get_all_namespaces(db_session) == ["user-alice", "user-bob"]

        alice = _get_quota_row(db_session, "user-alice")
        assert alice[0] == 300000   # hard_cpu_millicores
        assert alice[1] == 1280000  # hard_memory_mib (1250 * 1024)
        assert alice[3] == 20000    # used_cpu_millicores
        assert alice[4] == 81920    # used_memory_mib (80 * 1024)

        bob = _get_quota_row(db_session, "user-bob")
        assert bob[0] == 100000
        assert bob[3] == 50000

    def test_updates_existing_quotas(self, mock_k8s, db_session):
        # First sync
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice")[3] == 20000

        # Second sync with changed usage
        mock_api = mock_k8s.CoreV1Api.return_value
        mock_api.list_resource_quota_for_all_namespaces.return_value = _make_rq_list([
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "100", "requests.memory": "400Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice")[3] == 100000

    def test_deletes_removed_user_namespaces(self, mock_k8s, db_session):
        # First sync: two namespaces
        _setup_mock_api(mock_k8s, ["user-alice", "user-bob"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
            _make_resource_quota(
                "user-bob",
                {"requests.cpu": "100", "requests.memory": "500Gi"},
                {"requests.cpu": "50", "requests.memory": "200Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())
        assert len(_get_all_namespaces(db_session)) == 2

        # Second sync: bob's namespace label removed
        mock_api = mock_k8s.CoreV1Api.return_value
        mock_api.list_namespace.return_value = _make_ns_list(["user-alice"])
        mock_api.list_resource_quota_for_all_namespaces.return_value = _make_rq_list([
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())

        assert _get_all_namespaces(db_session) == ["user-alice"]

    def test_no_rq_for_namespace_deletes_row(self, mock_k8s, db_session):
        # First sync: ResourceQuota exists
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice") is not None

        # Second sync: ResourceQuota removed
        mock_api = mock_k8s.CoreV1Api.return_value
        mock_api.list_resource_quota_for_all_namespaces.return_value = _make_rq_list([])
        sync_resource_quotas(db_session, _make_settings())

        assert _get_quota_row(db_session, "user-alice") is None

    def test_namespace_list_api_error_preserves_data(self, mock_k8s, db_session):
        # First sync succeeds
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice")[3] == 20000

        # Second sync: namespace list fails
        mock_api = mock_k8s.CoreV1Api.return_value
        mock_api.list_namespace.side_effect = ApiException(status=500, reason="Internal")
        sync_resource_quotas(db_session, _make_settings())

        # Data preserved
        assert _get_quota_row(db_session, "user-alice")[3] == 20000

    def test_rq_list_api_error_preserves_data(self, mock_k8s, db_session):
        # First sync succeeds
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice")[3] == 20000

        # Second sync: ResourceQuota list fails
        mock_api = mock_k8s.CoreV1Api.return_value
        mock_api.list_resource_quota_for_all_namespaces.side_effect = ApiException(
            status=500, reason="Internal"
        )
        sync_resource_quotas(db_session, _make_settings())

        # Data preserved
        assert _get_quota_row(db_session, "user-alice")[3] == 20000

    def test_no_user_namespaces_clears_all(self, mock_k8s, db_session):
        # Manually insert a stale row
        db_session.execute(
            text(
                "INSERT INTO namespace_resource_quotas "
                "(namespace, hard_cpu_millicores, hard_memory_mib, "
                "used_cpu_millicores, used_memory_mib) "
                "VALUES ('user-old', 100000, 512000, 50000, 256000)"
            )
        )
        db_session.flush()
        assert len(_get_all_namespaces(db_session)) == 1

        _setup_mock_api(mock_k8s, [], [])
        sync_resource_quotas(db_session, _make_settings())

        assert _get_all_namespaces(db_session) == []

    def test_parses_cpu_memory_correctly(self, mock_k8s, db_session):
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "4", "requests.memory": "16Gi"},
                {"requests.cpu": "500m", "requests.memory": "2Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())

        row = _get_quota_row(db_session, "user-alice")
        assert row[0] == 4000    # hard_cpu: "4" -> 4000 millicores
        assert row[1] == 16384   # hard_mem: "16Gi" -> 16384 MiB
        assert row[3] == 500     # used_cpu: "500m" -> 500 millicores
        assert row[4] == 2048    # used_mem: "2Gi" -> 2048 MiB

    def test_gpu_from_flavor_config(self, mock_k8s, db_session):
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "64", "requests.memory": "500Gi", "requests.nvidia.com/gpu": "4"},
                {"requests.cpu": "16", "requests.memory": "128Gi", "requests.nvidia.com/gpu": "1"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings_with_gpu())

        row = _get_quota_row(db_session, "user-alice")
        assert row[2] == 4  # hard_gpu
        assert row[5] == 1  # used_gpu

    def test_uses_field_selector_with_resource_quota_name(self, mock_k8s, db_session):
        mock_api = _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "100", "requests.memory": "500Gi"},
                {"requests.cpu": "10", "requests.memory": "50Gi"},
            ),
        ])

        settings = _make_settings(RESOURCE_QUOTA_NAME="custom-quota")
        sync_resource_quotas(db_session, settings)

        call_args = mock_api.list_resource_quota_for_all_namespaces.call_args
        assert call_args.kwargs["field_selector"] == "metadata.name=custom-quota"

    def test_uses_user_namespace_label(self, mock_k8s, db_session):
        mock_api = _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "100", "requests.memory": "500Gi"},
                {"requests.cpu": "10", "requests.memory": "50Gi"},
            ),
        ])

        settings = _make_settings(USER_NAMESPACE_LABEL="type=user")
        sync_resource_quotas(db_session, settings)

        call_args = mock_api.list_namespace.call_args
        assert call_args.kwargs["label_selector"] == "type=user"

    def test_ignores_non_user_namespaces_in_rq_list(self, mock_k8s, db_session):
        """ResourceQuotas from non-user namespaces should be ignored."""
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
            _make_resource_quota(
                "cjob-system",
                {"requests.cpu": "100", "requests.memory": "500Gi"},
                {"requests.cpu": "10", "requests.memory": "50Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())

        assert _get_all_namespaces(db_session) == ["user-alice"]

    def test_tracks_namespace_without_active_jobs(self, mock_k8s, db_session):
        """User namespaces should be tracked even without active CJob jobs."""
        # No jobs inserted - namespace only exists in K8s
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "200", "requests.memory": "800Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())

        row = _get_quota_row(db_session, "user-alice")
        assert row is not None
        assert row[3] == 200000  # used_cpu - JupyterHub consuming resources

    def test_syncs_count_jobs_batch(self, mock_k8s, db_session):
        """count/jobs.batch should be synced to hard_count/used_count."""
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi",
                 "count/jobs.batch": "50"},
                {"requests.cpu": "20", "requests.memory": "80Gi",
                 "count/jobs.batch": "10"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())

        row = _get_quota_row(db_session, "user-alice")
        assert row[6] == 50   # hard_count
        assert row[7] == 10   # used_count

    def test_count_null_when_not_set(self, mock_k8s, db_session):
        """hard_count/used_count should be NULL when count/jobs.batch is absent."""
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())

        row = _get_quota_row(db_session, "user-alice")
        assert row[6] is None  # hard_count
        assert row[7] is None  # used_count

    def test_count_updates_on_resync(self, mock_k8s, db_session):
        """used_count should be updated on resync."""
        _setup_mock_api(mock_k8s, ["user-alice"], [
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi",
                 "count/jobs.batch": "50"},
                {"requests.cpu": "20", "requests.memory": "80Gi",
                 "count/jobs.batch": "5"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice")[7] == 5

        # Resync with updated used count
        mock_api = mock_k8s.CoreV1Api.return_value
        mock_api.list_resource_quota_for_all_namespaces.return_value = _make_rq_list([
            _make_resource_quota(
                "user-alice",
                {"requests.cpu": "300", "requests.memory": "1250Gi",
                 "count/jobs.batch": "50"},
                {"requests.cpu": "20", "requests.memory": "80Gi",
                 "count/jobs.batch": "20"},
            ),
        ])
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice")[7] == 20
