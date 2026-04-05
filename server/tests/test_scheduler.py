from unittest.mock import MagicMock

from sqlalchemy import text

from cjob.config import Settings
from cjob.dispatcher.scheduler import (
    cas_update_to_dispatching,
    fetch_dispatchable_jobs,
    filter_by_resource_quota,
    mark_dispatched,
    mark_failed,
    reset_stale_dispatching,
)
from cjob.metrics import JOBS_COMPLETED_TOTAL
from cjob.models import Job


NS = "alice"


def _insert_job(session, job_id, status="QUEUED", namespace=NS, user="alice", **kwargs):
    defaults = dict(
        namespace=namespace,
        job_id=job_id,
        user=user,
        image="test:1.0",
        command="python main.py",
        cwd="/home/jovyan",
        env_json={},
        cpu="1",
        memory="1Gi",
        gpu=0,
        time_limit_seconds=86400,
        status=status,
        log_dir=f"/home/jovyan/.cjob/logs/{job_id}",
    )
    defaults.update(kwargs)
    job = Job(**defaults)
    session.add(job)
    session.flush()
    return job


# ── cas_update_to_dispatching ──


class TestCasUpdateToDispatching:
    def test_queued_to_dispatching(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")

        result = cas_update_to_dispatching(db_session, NS, 1)

        assert result is True
        job = db_session.get(Job, (NS, 1))
        assert job.status == "DISPATCHING"

    def test_fails_if_not_queued(self, db_session):
        _insert_job(db_session, 1, status="RUNNING")

        result = cas_update_to_dispatching(db_session, NS, 1)

        assert result is False
        job = db_session.get(Job, (NS, 1))
        assert job.status == "RUNNING"

    def test_fails_if_cancelled(self, db_session):
        _insert_job(db_session, 1, status="CANCELLED")

        result = cas_update_to_dispatching(db_session, NS, 1)

        assert result is False
        job = db_session.get(Job, (NS, 1))
        assert job.status == "CANCELLED"

    def test_fails_if_not_found(self, db_session):
        result = cas_update_to_dispatching(db_session, NS, 999)

        assert result is False


# ── mark_dispatched ──


class TestMarkDispatched:
    def test_dispatching_to_dispatched(self, db_session):
        _insert_job(db_session, 1, status="DISPATCHING")

        result = mark_dispatched(db_session, NS, 1, "cjob-alice-1")

        assert result is True
        job = db_session.get(Job, (NS, 1))
        assert job.status == "DISPATCHED"
        assert job.k8s_job_name == "cjob-alice-1"

    def test_fails_if_cancelled(self, db_session):
        """If cancel API changed status to CANCELLED, mark_dispatched should fail."""
        _insert_job(db_session, 1, status="CANCELLED")

        result = mark_dispatched(db_session, NS, 1, "cjob-alice-1")

        assert result is False
        job = db_session.get(Job, (NS, 1))
        assert job.status == "CANCELLED"

    def test_fails_if_not_dispatching(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")

        result = mark_dispatched(db_session, NS, 1, "cjob-alice-1")

        assert result is False


# ── mark_failed ──


class TestMarkFailed:
    def test_dispatching_to_failed(self, db_session):
        _insert_job(db_session, 1, status="DISPATCHING")

        result = mark_failed(db_session, NS, 1, "permanent error")

        assert result is True
        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.last_error == "permanent error"

    def test_fails_if_cancelled(self, db_session):
        _insert_job(db_session, 1, status="CANCELLED")

        result = mark_failed(db_session, NS, 1, "error")

        assert result is False
        job = db_session.get(Job, (NS, 1))
        assert job.status == "CANCELLED"

    def test_fails_if_not_dispatching(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")

        result = mark_failed(db_session, NS, 1, "error")

        assert result is False

    def test_increments_completed_total_counter(self, db_session):
        _insert_job(db_session, 1, status="DISPATCHING")
        before = JOBS_COMPLETED_TOTAL.labels(status="failed")._value.get()

        mark_failed(db_session, NS, 1, "permanent error")

        assert JOBS_COMPLETED_TOTAL.labels(status="failed")._value.get() - before == 1

    def test_does_not_increment_counter_when_no_update(self, db_session):
        _insert_job(db_session, 1, status="CANCELLED")
        before = JOBS_COMPLETED_TOTAL.labels(status="failed")._value.get()

        mark_failed(db_session, NS, 1, "error")

        assert JOBS_COMPLETED_TOTAL.labels(status="failed")._value.get() - before == 0


# ── reset_stale_dispatching ──


class TestResetStaleDispatching:
    def test_resets_dispatching_to_queued(self, db_session):
        _insert_job(db_session, 1, status="DISPATCHING")
        _insert_job(db_session, 2, status="DISPATCHING")
        _insert_job(db_session, 3, status="RUNNING")

        count = reset_stale_dispatching(db_session)

        assert count == 2
        assert db_session.get(Job, (NS, 1)).status == "QUEUED"
        assert db_session.get(Job, (NS, 2)).status == "QUEUED"
        assert db_session.get(Job, (NS, 3)).status == "RUNNING"

    def test_no_stale_jobs(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")

        count = reset_stale_dispatching(db_session)

        assert count == 0

    def test_clears_retry_after(self, db_session):
        from datetime import datetime, timezone
        _insert_job(db_session, 1, status="DISPATCHING",
                     retry_after=datetime(2026, 1, 1, tzinfo=timezone.utc))

        reset_stale_dispatching(db_session)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "QUEUED"
        assert job.retry_after is None


# ── filter_by_resource_quota ──


def _insert_quota(session, namespace, hard_cpu, hard_mem, hard_gpu,
                  used_cpu, used_mem, used_gpu):
    session.execute(
        text(
            "INSERT INTO namespace_resource_quotas "
            "(namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu, "
            "used_cpu_millicores, used_memory_mib, used_gpu) "
            "VALUES (:ns, :hc, :hm, :hg, :uc, :um, :ug)"
        ),
        {"ns": namespace, "hc": hard_cpu, "hm": hard_mem, "hg": hard_gpu,
         "uc": used_cpu, "um": used_mem, "ug": used_gpu},
    )
    session.flush()


class TestFilterByResourceQuota:
    def test_no_quota_row_passes_all(self, db_session):
        """Namespaces without quota rows should pass all candidates."""
        j1 = _insert_job(db_session, 1, cpu_millicores=2000, memory_mib=4096)
        j2 = _insert_job(db_session, 2, cpu_millicores=4000, memory_mib=8192)

        result = filter_by_resource_quota(db_session, [j1, j2])

        assert len(result) == 2

    def test_sufficient_quota_passes(self, db_session):
        """Jobs that fit within remaining quota should pass."""
        _insert_quota(db_session, NS, 300000, 1280000, 0, 20000, 80000, 0)
        j1 = _insert_job(db_session, 1, cpu_millicores=2000, memory_mib=4096)

        result = filter_by_resource_quota(db_session, [j1])

        assert len(result) == 1

    def test_insufficient_cpu_skips(self, db_session):
        """Job requiring more CPU than remaining should be skipped."""
        # remaining cpu = 300000 - 298000 = 2000
        _insert_quota(db_session, NS, 300000, 1280000, 0, 298000, 80000, 0)
        j1 = _insert_job(db_session, 1, cpu_millicores=4000, memory_mib=1024)

        result = filter_by_resource_quota(db_session, [j1])

        assert len(result) == 0

    def test_insufficient_memory_skips(self, db_session):
        """Job requiring more memory than remaining should be skipped."""
        # remaining mem = 1280000 - 1279000 = 1000
        _insert_quota(db_session, NS, 300000, 1280000, 0, 0, 1279000, 0)
        j1 = _insert_job(db_session, 1, cpu_millicores=1000, memory_mib=2048)

        result = filter_by_resource_quota(db_session, [j1])

        assert len(result) == 0

    def test_insufficient_gpu_skips(self, db_session):
        """Job requiring more GPU than remaining should be skipped."""
        # remaining gpu = 4 - 4 = 0
        _insert_quota(db_session, NS, 300000, 1280000, 4, 0, 0, 4)
        j1 = _insert_job(db_session, 1, cpu_millicores=1000, memory_mib=1024, gpu=1)

        result = filter_by_resource_quota(db_session, [j1])

        assert len(result) == 0

    def test_sweep_multiplies_by_parallelism(self, db_session):
        """Sweep jobs should multiply resource requirements by parallelism."""
        # remaining cpu = 10000, job needs 2000 * 4 = 8000 -> fits
        _insert_quota(db_session, NS, 300000, 1280000, 0, 290000, 0, 0)
        j1 = _insert_job(db_session, 1, cpu_millicores=2000, memory_mib=1024,
                         completions=20, parallelism=4)

        result = filter_by_resource_quota(db_session, [j1])
        assert len(result) == 1

        # remaining cpu = 6000, job needs 2000 * 4 = 8000 -> doesn't fit
        db_session.execute(text("DELETE FROM namespace_resource_quotas"))
        db_session.flush()
        _insert_quota(db_session, NS, 300000, 1280000, 0, 294000, 0, 0)

        result = filter_by_resource_quota(db_session, [j1])
        assert len(result) == 0

    def test_cumulative_tracking(self, db_session):
        """Second job should be skipped when cumulative resources exceed quota."""
        # remaining cpu = 5000; j1 needs 3000, j2 needs 3000
        _insert_quota(db_session, NS, 300000, 1280000, 0, 295000, 0, 0)
        j1 = _insert_job(db_session, 1, cpu_millicores=3000, memory_mib=1024)
        j2 = _insert_job(db_session, 2, cpu_millicores=3000, memory_mib=1024)

        result = filter_by_resource_quota(db_session, [j1, j2])

        assert len(result) == 1
        assert result[0].job_id == 1

    def test_mixed_namespaces(self, db_session):
        """Namespaces with and without quota rows should be handled correctly."""
        _insert_quota(db_session, NS, 300000, 1280000, 0, 299000, 0, 0)
        # alice: remaining cpu = 1000 (insufficient for 2000)
        j1 = _insert_job(db_session, 1, namespace=NS, cpu_millicores=2000,
                         memory_mib=1024)
        # bob: no quota row -> unrestricted
        j2 = _insert_job(db_session, 1, namespace="bob", cpu_millicores=2000,
                         memory_mib=1024)

        result = filter_by_resource_quota(db_session, [j1, j2])

        assert len(result) == 1
        assert result[0].namespace == "bob"

    def test_empty_candidates(self, db_session):
        """Empty candidate list should return empty list."""
        result = filter_by_resource_quota(db_session, [])

        assert result == []


# ── fetch_dispatchable_jobs ──


def _fetch_settings(batch_size=50, fetch_multiplier=10):
    return Settings(
        POSTGRES_PASSWORD="test",
        DISPATCH_BATCH_SIZE=batch_size,
        DISPATCH_FETCH_MULTIPLIER=fetch_multiplier,
    )


class TestFetchDispatchableJobsLimit:
    """Verify SQL LIMIT is BATCH_SIZE * FETCH_MULTIPLIER (issue #136).

    fetch_dispatchable_jobs uses PostgreSQL-specific SQL (CURRENT_DATE,
    NULLS FIRST, MAKE_INTERVAL, ::float) that doesn't run under SQLite,
    so we mock the session and assert the bound parameters instead.
    """

    def _mock_session(self, cluster_totals_row):
        """Build a mocked Session that handles the 3 execute calls.

        1. _cleanup_old_usage
        2. _fetch_cluster_totals (reads .mappings().first())
        3. main SELECT (iterated via `for row in result.mappings()`)
        """
        cleanup_result = MagicMock()
        totals_result = MagicMock()
        totals_result.mappings.return_value.first.return_value = cluster_totals_row
        main_result = MagicMock()
        main_result.mappings.return_value = iter([])

        session = MagicMock()
        session.execute.side_effect = [cleanup_result, totals_result, main_result]
        return session

    def test_fetch_limit_uses_multiplier_drf_path(self):
        """DRF-enabled branch binds fetch_limit = BATCH_SIZE * FETCH_MULTIPLIER."""
        session = self._mock_session(
            {"total_cpu": 1000, "total_memory": 4096, "total_gpu": 0}
        )
        settings = _fetch_settings(batch_size=50, fetch_multiplier=10)

        fetch_dispatchable_jobs(session, settings)

        # The 3rd execute call is the main SELECT; its 2nd positional arg is params.
        main_call = session.execute.call_args_list[2]
        params = main_call.args[1]
        assert params["fetch_limit"] == 500
        assert "batch_size" not in params

    def test_fetch_limit_uses_multiplier_fallback_path(self):
        """Fallback branch (empty node_resources) also uses the multiplier."""
        session = self._mock_session(
            {"total_cpu": 0, "total_memory": 0, "total_gpu": 0}
        )
        settings = _fetch_settings(batch_size=20, fetch_multiplier=5)

        fetch_dispatchable_jobs(session, settings)

        main_call = session.execute.call_args_list[2]
        params = main_call.args[1]
        assert params["fetch_limit"] == 100
        assert "batch_size" not in params
