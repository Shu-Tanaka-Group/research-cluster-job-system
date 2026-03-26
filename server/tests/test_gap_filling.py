from unittest.mock import patch

from cjob.config import Settings
from cjob.dispatcher.scheduler import apply_gap_filling
from cjob.models import Job


def _make_job(namespace, job_id, time_limit_seconds=86400):
    """Build a minimal Job object for filtering tests."""
    return Job(
        namespace=namespace,
        job_id=job_id,
        user=namespace.removeprefix("user-"),
        image="test:1.0",
        command="python main.py",
        cwd="/home/jovyan",
        env_json={},
        cpu="1",
        memory="1Gi",
        gpu=0,
        time_limit_seconds=time_limit_seconds,
        status="QUEUED",
        log_dir=f"/home/jovyan/.cjob/logs/{job_id}",
    )


def _make_settings(enabled=True, threshold=300):
    return Settings(
        POSTGRES_PASSWORD="test",
        GAP_FILLING_ENABLED=enabled,
        GAP_FILLING_STALL_THRESHOLD_SEC=threshold,
    )


NS = "user-alice"
NS2 = "user-bob"


@patch("cjob.dispatcher.scheduler.estimate_shortest_remaining")
@patch("cjob.dispatcher.scheduler.fetch_stalled_jobs")
class TestApplyGapFilling:

    def test_disabled(self, mock_stalled, mock_remaining, db_session):
        """GAP_FILLING_ENABLED=False should return candidates unchanged."""
        settings = _make_settings(enabled=False)
        candidates = [_make_job(NS, 1), _make_job(NS, 2)]

        result = apply_gap_filling(db_session, candidates, settings)

        assert result == candidates
        mock_stalled.assert_not_called()

    def test_no_stalled_jobs(self, mock_stalled, mock_remaining, db_session):
        """No stalled jobs → candidates unchanged."""
        settings = _make_settings()
        mock_stalled.return_value = []
        candidates = [_make_job(NS, 1), _make_job(NS, 2)]

        result = apply_gap_filling(db_session, candidates, settings)

        assert result == candidates
        mock_remaining.assert_not_called()

    def test_stalled_with_remaining_time(self, mock_stalled, mock_remaining, db_session):
        """Stalled job exists, RUNNING remaining=3600s → only time_limit<=3600 pass."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99, time_limit_seconds=86400)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 3600

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

    def test_stalled_no_running_jobs(self, mock_stalled, mock_remaining, db_session):
        """Stalled job exists, no RUNNING jobs (remaining=None) → all held."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = None

        candidates = [
            _make_job(NS, 1, time_limit_seconds=60),
            _make_job(NS, 2, time_limit_seconds=3600),
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        assert result == []

    def test_mixed_namespaces(self, mock_stalled, mock_remaining, db_session):
        """Only stalled namespace is filtered; other namespaces pass through."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 1800

        candidates = [
            _make_job(NS, 1, time_limit_seconds=3600),    # NS: held (3600 > 1800)
            _make_job(NS, 2, time_limit_seconds=900),     # NS: fits
            _make_job(NS2, 1, time_limit_seconds=86400),  # NS2: unaffected
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        result_ids = [(j.namespace, j.job_id) for j in result]
        assert (NS2, 1) in result_ids   # unaffected
        assert (NS, 2) in result_ids    # fits
        assert (NS, 1) not in result_ids  # held

    def test_stalled_remaining_zero(self, mock_stalled, mock_remaining, db_session):
        """Remaining=0 → only time_limit_seconds=0 would pass, effectively all held."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]
        mock_remaining.return_value = 0

        candidates = [
            _make_job(NS, 1, time_limit_seconds=60),
        ]

        result = apply_gap_filling(db_session, candidates, settings)

        assert result == []

    def test_no_candidates_for_stalled_namespace(self, mock_stalled, mock_remaining, db_session):
        """Stalled namespace has no QUEUED candidates → nothing to filter."""
        settings = _make_settings()
        stalled_job = _make_job(NS, 99)
        mock_stalled.return_value = [stalled_job]

        # Only NS2 has candidates
        candidates = [_make_job(NS2, 1)]

        result = apply_gap_filling(db_session, candidates, settings)

        assert len(result) == 1
        assert result[0].namespace == NS2
        mock_remaining.assert_not_called()
