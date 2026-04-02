from unittest.mock import patch

from cjob.config import Settings
from cjob.dispatcher.scheduler import apply_gap_filling
from cjob.models import Job


def _make_job(
    namespace,
    job_id,
    user="alice",
    time_limit_seconds=86400,
    cpu_millicores=1000,
    memory_mib=1024,
    gpu=0,
    flavor="cpu",
    completions=None,
    parallelism=None,
):
    """Build a minimal Job object for filtering tests."""
    return Job(
        namespace=namespace,
        job_id=job_id,
        user=user,
        image="test:1.0",
        command="python main.py",
        cwd="/home/jovyan",
        env_json={},
        cpu="1",
        memory="1Gi",
        gpu=gpu,
        flavor=flavor,
        time_limit_seconds=time_limit_seconds,
        status="QUEUED",
        log_dir=f"/home/jovyan/.cjob/logs/{job_id}",
        cpu_millicores=cpu_millicores,
        memory_mib=memory_mib,
        completions=completions,
        parallelism=parallelism,
    )


def _make_settings(enabled=True, threshold=300):
    return Settings(
        POSTGRES_PASSWORD="test",
        GAP_FILLING_ENABLED=enabled,
        GAP_FILLING_STALL_THRESHOLD_SEC=threshold,
    )


NS = "alice"
NS2 = "bob"


@patch("cjob.dispatcher.scheduler.estimate_available_cluster_resources")
@patch("cjob.dispatcher.scheduler.estimate_shortest_remaining")
@patch("cjob.dispatcher.scheduler.fetch_stalled_jobs")
class TestApplyGapFilling:

    def test_disabled(self, mock_stalled, mock_remaining, mock_available, db_session):
        """GAP_FILLING_ENABLED=False should return candidates unchanged."""
        settings = _make_settings(enabled=False)
        candidates = [_make_job(NS, 1), _make_job(NS, 2)]

        result = apply_gap_filling(db_session, candidates, settings)

        assert result == candidates
        mock_stalled.assert_not_called()

    def test_no_stalled_jobs(self, mock_stalled, mock_remaining, mock_available, db_session):
        """No stalled jobs → candidates unchanged."""
        settings = _make_settings()
        mock_stalled.return_value = []
        candidates = [_make_job(NS, 1), _make_job(NS, 2)]

        result = apply_gap_filling(db_session, candidates, settings)

        assert result == candidates
        mock_remaining.assert_not_called()

    def test_stalled_with_remaining_time(self, mock_stalled, mock_remaining, mock_available, db_session):
        """Stalled job exists, RUNNING remaining=3600s → only time_limit<=3600 pass."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99, time_limit_seconds=86400)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 3600
        mock_available.return_value = {}  # No quota info → unrestricted

        candidates = [
            _make_job(NS, 1, time_limit_seconds=1800),   # fits
            _make_job(NS, 2, time_limit_seconds=3600),   # fits (exactly)
            _make_job(NS, 3, time_limit_seconds=7200),   # held
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        result_ids = [j.job_id for j in result]
        assert 1 in result_ids
        assert 2 in result_ids
        assert 3 not in result_ids

    def test_stalled_no_running_jobs(self, mock_stalled, mock_remaining, mock_available, db_session):
        """Stalled job exists, no RUNNING jobs (remaining=None) → time check bypassed."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = None
        mock_available.return_value = {}  # No quota info → unrestricted

        candidates = [
            _make_job(NS, 1, time_limit_seconds=60),
            _make_job(NS, 2, time_limit_seconds=3600),
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        assert len(result) == 2
        result_ids = [j.job_id for j in result]
        assert 1 in result_ids
        assert 2 in result_ids

    def test_mixed_namespaces(self, mock_stalled, mock_remaining, mock_available, db_session):
        """Only stalled namespace is filtered; other namespaces pass through."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 1800
        mock_available.return_value = {}

        candidates = [
            _make_job(NS, 1, time_limit_seconds=3600),    # NS: held (3600 > 1800)
            _make_job(NS, 2, time_limit_seconds=900),     # NS: fits
            _make_job(NS2, 1, user="bob", time_limit_seconds=86400),  # NS2: unaffected
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        result_ids = [(j.namespace, j.job_id) for j in result]
        assert (NS2, 1) in result_ids   # unaffected
        assert (NS, 2) in result_ids    # fits
        assert (NS, 1) not in result_ids  # held

    def test_stalled_remaining_zero(self, mock_stalled, mock_remaining, mock_available, db_session):
        """Remaining=0 → only time_limit_seconds=0 would pass, effectively all held."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 0
        mock_available.return_value = {}

        candidates = [
            _make_job(NS, 1, time_limit_seconds=60),
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        assert result == []

    def test_no_candidates_for_stalled_namespace(self, mock_stalled, mock_remaining, mock_available, db_session):
        """Stalled namespace has no QUEUED candidates → nothing to filter."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_available.return_value = {}

        # Only NS2 has candidates
        candidates = [_make_job(NS2, 1, user="bob")]

        result = apply_gap_filling(db_session, candidates, settings)

        assert len(result) == 1
        assert result[0].namespace == NS2
        mock_remaining.assert_not_called()

    # --- Resource check tests ---

    def test_resource_exceeds_available(self, mock_stalled, mock_remaining, mock_available, db_session):
        """Jobs exceeding available ClusterQueue resources are held."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 86400  # Time is not the bottleneck
        mock_available.return_value = {
            "cpu": {"cpu": 2000, "mem": 4096, "gpu": 0},
        }

        candidates = [
            _make_job(NS, 1, cpu_millicores=1000, memory_mib=1024),  # fits
            _make_job(NS, 2, cpu_millicores=4000, memory_mib=1024),  # CPU exceeds
            _make_job(NS, 3, cpu_millicores=1000, memory_mib=8192),  # memory exceeds
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        result_ids = [j.job_id for j in result]
        assert 1 in result_ids
        assert 2 not in result_ids
        assert 3 not in result_ids

    def test_resource_no_quota_info(self, mock_stalled, mock_remaining, mock_available, db_session):
        """When flavor_quotas is empty, all candidates pass resource check."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 86400
        mock_available.return_value = {}  # No quota info

        candidates = [
            _make_job(NS, 1, cpu_millicores=99999, memory_mib=99999),
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        assert len(result) == 1

    def test_resource_unknown_flavor(self, mock_stalled, mock_remaining, mock_available, db_session):
        """Jobs with a flavor not in flavor_quotas pass resource check (unrestricted)."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 86400
        mock_available.return_value = {
            "cpu": {"cpu": 1000, "mem": 1024, "gpu": 0},
        }

        candidates = [
            _make_job(NS, 1, flavor="gpu-a100", cpu_millicores=99999),  # unknown flavor → pass
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        assert len(result) == 1

    def test_resource_cumulative_tracking(self, mock_stalled, mock_remaining, mock_available, db_session):
        """Cumulative resource tracking within a pass prevents over-dispatch."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 86400
        mock_available.return_value = {
            "cpu": {"cpu": 3000, "mem": 8192, "gpu": 0},
        }

        candidates = [
            _make_job(NS, 1, cpu_millicores=2000, memory_mib=1024),  # fits (remaining: 1000)
            _make_job(NS, 2, cpu_millicores=2000, memory_mib=1024),  # exceeds cumulative
            _make_job(NS, 3, cpu_millicores=1000, memory_mib=1024),  # fits remaining 1000
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        result_ids = [j.job_id for j in result]
        assert 1 in result_ids
        assert 2 not in result_ids
        assert 3 in result_ids

    def test_resource_sweep_parallelism(self, mock_stalled, mock_remaining, mock_available, db_session):
        """Sweep jobs consume parallelism * resources."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 86400
        mock_available.return_value = {
            "cpu": {"cpu": 3000, "mem": 8192, "gpu": 0},
        }

        candidates = [
            # sweep: 4 parallel pods × 1000m = 4000m → exceeds 3000m
            _make_job(NS, 1, cpu_millicores=1000, memory_mib=1024,
                      completions=10, parallelism=4),
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        assert len(result) == 0

    def test_resource_check_with_no_running_jobs(self, mock_stalled, mock_remaining, mock_available, db_session):
        """When remaining=None, time check is bypassed but resource check still applies."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = None  # No RUNNING jobs
        mock_available.return_value = {
            "cpu": {"cpu": 2000, "mem": 4096, "gpu": 0},
        }

        candidates = [
            _make_job(NS, 1, cpu_millicores=1000, memory_mib=1024),  # fits resources
            _make_job(NS, 2, cpu_millicores=4000, memory_mib=1024),  # exceeds resources
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        result_ids = [j.job_id for j in result]
        assert 1 in result_ids
        assert 2 not in result_ids

    def test_resource_gpu_check(self, mock_stalled, mock_remaining, mock_available, db_session):
        """GPU resources are checked per flavor."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 86400
        mock_available.return_value = {
            "gpu-a100": {"cpu": 64000, "mem": 512000, "gpu": 1},
        }

        candidates = [
            _make_job(NS, 1, flavor="gpu-a100", gpu=1,
                      cpu_millicores=4000, memory_mib=8192),  # fits
            _make_job(NS, 2, flavor="gpu-a100", gpu=2,
                      cpu_millicores=4000, memory_mib=8192),  # GPU exceeds
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        result_ids = [j.job_id for j in result]
        assert 1 in result_ids
        assert 2 not in result_ids
