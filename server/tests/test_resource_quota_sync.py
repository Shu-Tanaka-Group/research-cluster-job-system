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
            {"name": "cpu", "label_selector": "cluster-job=true"},
        ]),
        DEFAULT_FLAVOR="cpu",
        NODE_RESOURCE_SYNC_INTERVAL_SEC=300,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_settings_with_gpu():
    return _make_settings(
        RESOURCE_FLAVORS=json.dumps([
            {"name": "cpu", "label_selector": "cluster-job=true"},
            {"name": "gpu-a100", "label_selector": "cluster-gpu-a100=true", "gpu_resource_name": "nvidia.com/gpu"},
        ]),
    )


def _make_resource_quota(hard, used):
    """Build a mock K8s ResourceQuota object.

    hard/used: dict of resource name -> value string,
    e.g. {"requests.cpu": "300", "requests.memory": "1250Gi"}
    """
    rq = MagicMock()
    rq.spec.hard = hard
    rq.status.used = used
    return rq


def _insert_job(session, namespace, status="QUEUED"):
    """Insert a minimal job to make a namespace 'active'."""
    # Get next job_id for this namespace
    row = session.execute(
        text("SELECT COALESCE(MAX(job_id), 0) + 1 FROM jobs WHERE namespace = :ns"),
        {"ns": namespace},
    ).scalar()
    session.execute(
        text(
            "INSERT INTO jobs (namespace, job_id, \"user\", image, command, cwd, "
            "cpu, memory, gpu, time_limit_seconds, status) "
            "VALUES (:ns, :jid, 'testuser', 'img', 'echo', '/tmp', "
            "'1', '1Gi', 0, 3600, :status)"
        ),
        {"ns": namespace, "jid": row, "status": status},
    )
    session.flush()


def _get_quota_row(session, namespace):
    return session.execute(
        text(
            "SELECT hard_cpu_millicores, hard_memory_mib, hard_gpu, "
            "used_cpu_millicores, used_memory_mib, used_gpu "
            "FROM namespace_resource_quotas WHERE namespace = :ns"
        ),
        {"ns": namespace},
    ).first()


def _get_all_namespaces(session):
    rows = session.execute(
        text("SELECT namespace FROM namespace_resource_quotas ORDER BY namespace")
    )
    return [row[0] for row in rows]


@patch("cjob.watcher.resource_quota_sync.k8s_client")
class TestSyncResourceQuotas:
    def test_inserts_for_active_namespaces(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        _insert_job(db_session, "user-alice", "QUEUED")
        _insert_job(db_session, "user-bob", "RUNNING")

        mock_api.read_namespaced_resource_quota.side_effect = [
            _make_resource_quota(
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
            _make_resource_quota(
                {"requests.cpu": "100", "requests.memory": "500Gi"},
                {"requests.cpu": "50", "requests.memory": "200Gi"},
            ),
        ]

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
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        _insert_job(db_session, "user-alice", "QUEUED")

        # First sync
        mock_api.read_namespaced_resource_quota.return_value = _make_resource_quota(
            {"requests.cpu": "300", "requests.memory": "1250Gi"},
            {"requests.cpu": "20", "requests.memory": "80Gi"},
        )
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice")[3] == 20000  # used_cpu

        # Second sync with changed usage
        mock_api.read_namespaced_resource_quota.return_value = _make_resource_quota(
            {"requests.cpu": "300", "requests.memory": "1250Gi"},
            {"requests.cpu": "100", "requests.memory": "400Gi"},
        )
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice")[3] == 100000  # used_cpu updated

    def test_deletes_inactive_namespaces(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        _insert_job(db_session, "user-alice", "QUEUED")
        _insert_job(db_session, "user-bob", "RUNNING")

        mock_api.read_namespaced_resource_quota.side_effect = [
            _make_resource_quota(
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
            _make_resource_quota(
                {"requests.cpu": "100", "requests.memory": "500Gi"},
                {"requests.cpu": "50", "requests.memory": "200Gi"},
            ),
        ]
        sync_resource_quotas(db_session, _make_settings())
        assert len(_get_all_namespaces(db_session)) == 2

        # Bob's job completes -> no longer active
        db_session.execute(
            text("UPDATE jobs SET status = 'SUCCEEDED' WHERE namespace = 'user-bob'")
        )
        db_session.flush()

        mock_api.read_namespaced_resource_quota.side_effect = [
            _make_resource_quota(
                {"requests.cpu": "300", "requests.memory": "1250Gi"},
                {"requests.cpu": "20", "requests.memory": "80Gi"},
            ),
        ]
        sync_resource_quotas(db_session, _make_settings())

        assert _get_all_namespaces(db_session) == ["user-alice"]

    def test_404_deletes_row(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        _insert_job(db_session, "user-alice", "QUEUED")

        # First sync: ResourceQuota exists
        mock_api.read_namespaced_resource_quota.return_value = _make_resource_quota(
            {"requests.cpu": "300", "requests.memory": "1250Gi"},
            {"requests.cpu": "20", "requests.memory": "80Gi"},
        )
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice") is not None

        # Second sync: ResourceQuota removed (404)
        mock_api.read_namespaced_resource_quota.side_effect = ApiException(
            status=404, reason="Not Found"
        )
        sync_resource_quotas(db_session, _make_settings())

        assert _get_quota_row(db_session, "user-alice") is None

    def test_api_error_preserves_existing_data(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        _insert_job(db_session, "user-alice", "QUEUED")

        # First sync succeeds
        mock_api.read_namespaced_resource_quota.return_value = _make_resource_quota(
            {"requests.cpu": "300", "requests.memory": "1250Gi"},
            {"requests.cpu": "20", "requests.memory": "80Gi"},
        )
        sync_resource_quotas(db_session, _make_settings())
        assert _get_quota_row(db_session, "user-alice")[3] == 20000

        # Second sync fails with 500
        mock_api.read_namespaced_resource_quota.side_effect = ApiException(
            status=500, reason="Internal"
        )
        sync_resource_quotas(db_session, _make_settings())

        # Data preserved
        assert _get_quota_row(db_session, "user-alice")[3] == 20000

    def test_no_active_namespaces_clears_all(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

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

        # No active jobs -> clears all
        sync_resource_quotas(db_session, _make_settings())

        assert _get_all_namespaces(db_session) == []

    def test_parses_cpu_memory_correctly(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        _insert_job(db_session, "user-alice", "QUEUED")

        mock_api.read_namespaced_resource_quota.return_value = _make_resource_quota(
            {"requests.cpu": "4", "requests.memory": "16Gi"},
            {"requests.cpu": "500m", "requests.memory": "2Gi"},
        )
        sync_resource_quotas(db_session, _make_settings())

        row = _get_quota_row(db_session, "user-alice")
        assert row[0] == 4000    # hard_cpu: "4" -> 4000 millicores
        assert row[1] == 16384   # hard_mem: "16Gi" -> 16384 MiB
        assert row[3] == 500     # used_cpu: "500m" -> 500 millicores
        assert row[4] == 2048    # used_mem: "2Gi" -> 2048 MiB

    def test_gpu_from_flavor_config(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        _insert_job(db_session, "user-alice", "QUEUED")

        mock_api.read_namespaced_resource_quota.return_value = _make_resource_quota(
            {"requests.cpu": "64", "requests.memory": "500Gi", "requests.nvidia.com/gpu": "4"},
            {"requests.cpu": "16", "requests.memory": "128Gi", "requests.nvidia.com/gpu": "1"},
        )
        sync_resource_quotas(db_session, _make_settings_with_gpu())

        row = _get_quota_row(db_session, "user-alice")
        assert row[2] == 4  # hard_gpu
        assert row[5] == 1  # used_gpu

    def test_uses_resource_quota_name_from_settings(self, mock_k8s, db_session):
        mock_api = MagicMock()
        mock_k8s.CoreV1Api.return_value = mock_api

        _insert_job(db_session, "user-alice", "QUEUED")

        mock_api.read_namespaced_resource_quota.return_value = _make_resource_quota(
            {"requests.cpu": "100", "requests.memory": "500Gi"},
            {"requests.cpu": "10", "requests.memory": "50Gi"},
        )

        settings = _make_settings(RESOURCE_QUOTA_NAME="custom-quota")
        sync_resource_quotas(db_session, settings)

        call_args = mock_api.read_namespaced_resource_quota.call_args
        assert call_args.kwargs["name"] == "custom-quota"
