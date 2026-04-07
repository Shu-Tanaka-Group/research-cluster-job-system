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
                  used_cpu, used_mem, used_gpu,
                  hard_count=None, used_count=None):
    session.execute(
        text(
            "INSERT INTO namespace_resource_quotas "
            "(namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu, "
            "used_cpu_millicores, used_memory_mib, used_gpu, "
            "hard_count, used_count) "
            "VALUES (:ns, :hc, :hm, :hg, :uc, :um, :ug, :h_count, :u_count)"
        ),
        {"ns": namespace, "hc": hard_cpu, "hm": hard_mem, "hg": hard_gpu,
         "uc": used_cpu, "um": used_mem, "ug": used_gpu,
         "h_count": hard_count, "u_count": used_count},
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

    def test_count_null_skips_check(self, db_session):
        """When hard_count is NULL, job count limit is not enforced."""
        _insert_quota(db_session, NS, 300000, 1280000, 0, 20000, 80000, 0)
        j1 = _insert_job(db_session, 1, cpu_millicores=2000, memory_mib=4096)

        result = filter_by_resource_quota(db_session, [j1])

        assert len(result) == 1

    def test_sufficient_count_passes(self, db_session):
        """Job should pass when remaining job count is sufficient."""
        _insert_quota(db_session, NS, 300000, 1280000, 0, 20000, 80000, 0,
                      hard_count=50, used_count=30)
        j1 = _insert_job(db_session, 1, cpu_millicores=2000, memory_mib=4096)

        result = filter_by_resource_quota(db_session, [j1])

        assert len(result) == 1

    def test_insufficient_count_skips(self, db_session):
        """Job should be skipped when remaining job count is 0."""
        _insert_quota(db_session, NS, 300000, 1280000, 0, 20000, 80000, 0,
                      hard_count=50, used_count=50)
        j1 = _insert_job(db_session, 1, cpu_millicores=2000, memory_mib=4096)

        result = filter_by_resource_quota(db_session, [j1])

        assert len(result) == 0

    def test_count_cumulative_tracking(self, db_session):
        """Second job should be skipped when cumulative count exceeds quota."""
        # remaining count = 1; j1 uses 1, j2 would exceed
        _insert_quota(db_session, NS, 300000, 1280000, 0, 0, 0, 0,
                      hard_count=50, used_count=49)
        j1 = _insert_job(db_session, 1, cpu_millicores=1000, memory_mib=1024)
        j2 = _insert_job(db_session, 2, cpu_millicores=1000, memory_mib=1024)

        result = filter_by_resource_quota(db_session, [j1, j2])

        assert len(result) == 1
        assert result[0].job_id == 1

    def test_sweep_count_is_one(self, db_session):
        """Sweep job should count as 1 for count/jobs.batch."""
        # remaining count = 1; sweep with parallelism=4 still uses 1
        _insert_quota(db_session, NS, 300000, 1280000, 0, 0, 0, 0,
                      hard_count=50, used_count=49)
        j1 = _insert_job(db_session, 1, cpu_millicores=1000, memory_mib=1024,
                         completions=20, parallelism=4)

        result = filter_by_resource_quota(db_session, [j1])

        assert len(result) == 1

    def test_count_blocks_but_resources_sufficient(self, db_session):
        """Job should be skipped when count is exhausted even if resources are sufficient."""
        _insert_quota(db_session, NS, 300000, 1280000, 4, 0, 0, 0,
                      hard_count=50, used_count=50)
        j1 = _insert_job(db_session, 1, cpu_millicores=1000, memory_mib=1024)

        result = filter_by_resource_quota(db_session, [j1])

        assert len(result) == 0


# ── fetch_dispatchable_jobs ──


def _fetch_settings(batch_size=50, fetch_multiplier=10):
    return Settings(
        POSTGRES_PASSWORD="test",
        DISPATCH_BATCH_SIZE=batch_size,
        DISPATCH_FETCH_MULTIPLIER=fetch_multiplier,
    )


class TestFetchDispatchableJobsLimit:
    """Verify SQL LIMIT and per-flavor DRF parameters.

    fetch_dispatchable_jobs uses PostgreSQL-specific SQL (CURRENT_DATE,
    NULLS FIRST, MAKE_INTERVAL, ::FLOAT) that doesn't run under SQLite,
    so we mock the session and assert the bound parameters instead.
    """

    def _mock_session(self, alloc_rows, quota_rows=None):
        """Build a mocked Session that handles the 4 execute calls.

        1. _cleanup_old_usage
        2. _fetch_flavor_caps: alloc query (reads .mappings().all())
        3. _fetch_flavor_caps: quota query (reads .mappings().all())
        4. main SELECT (iterated via `for row in result.mappings()`)
        """
        if quota_rows is None:
            quota_rows = []

        cleanup_result = MagicMock()
        alloc_result = MagicMock()
        alloc_result.mappings.return_value.all.return_value = alloc_rows
        quota_result = MagicMock()
        quota_result.mappings.return_value.all.return_value = quota_rows
        main_result = MagicMock()
        main_result.mappings.return_value = iter([])

        session = MagicMock()
        session.execute.side_effect = [
            cleanup_result, alloc_result, quota_result, main_result,
        ]
        return session

    def test_fetch_limit_uses_multiplier_drf_path(self):
        """DRF-enabled branch binds fetch_limit = BATCH_SIZE * FETCH_MULTIPLIER."""
        session = self._mock_session(
            [{"flavor": "cpu", "total_cpu": 1000, "total_memory": 4096, "total_gpu": 0}],
        )
        settings = _fetch_settings(batch_size=50, fetch_multiplier=10)

        fetch_dispatchable_jobs(session, settings)

        # The 4th execute call is the main SELECT; its 2nd positional arg is params.
        main_call = session.execute.call_args_list[3]
        params = main_call.args[1]
        assert params["fetch_limit"] == 500
        assert "batch_size" not in params

    def test_fetch_limit_uses_multiplier_fallback_path(self):
        """Fallback branch (empty node_resources) also uses the multiplier."""
        session = self._mock_session([])
        settings = _fetch_settings(batch_size=20, fetch_multiplier=5)

        fetch_dispatchable_jobs(session, settings)

        # alloc_rows is empty so quota query is skipped; main SELECT is 3rd call.
        main_call = session.execute.call_args_list[2]
        params = main_call.args[1]
        assert params["fetch_limit"] == 100
        assert "batch_size" not in params

    def test_per_flavor_caps_capped_by_nominal_quota(self):
        """Per-flavor caps should be MIN(allocatable, nominalQuota)."""
        session = self._mock_session(
            [{"flavor": "cpu", "total_cpu": 256000, "total_memory": 1048576, "total_gpu": 0}],
            [{"flavor": "cpu", "cpu": "128", "memory": "500Gi", "gpu": "0", "drf_weight": 1.0}],
        )
        settings = _fetch_settings(batch_size=50, fetch_multiplier=10)

        fetch_dispatchable_jobs(session, settings)

        main_call = session.execute.call_args_list[3]
        params = main_call.args[1]
        assert params["f_0"] == "cpu"
        assert params["cpu_0"] == 128000.0
        assert params["mem_0"] == 512000.0
        assert params["gpu_0"] == 0.0
        assert params["w_0"] == 1.0

    def test_per_flavor_caps_uses_allocatable_when_quota_larger(self):
        """When nominalQuota > allocatable, allocatable should be used."""
        session = self._mock_session(
            [{"flavor": "gpu", "total_cpu": 64000, "total_memory": 262144, "total_gpu": 4}],
            [{"flavor": "gpu", "cpu": "128", "memory": "500Gi", "gpu": "8", "drf_weight": 1.0}],
        )
        settings = _fetch_settings(batch_size=50, fetch_multiplier=10)

        fetch_dispatchable_jobs(session, settings)

        main_call = session.execute.call_args_list[3]
        params = main_call.args[1]
        assert params["f_0"] == "gpu"
        assert params["cpu_0"] == 64000.0
        assert params["mem_0"] == 262144.0
        assert params["gpu_0"] == 4.0

    def test_per_flavor_caps_no_quota_uses_allocatable(self):
        """When flavor_quotas is empty, raw allocatable with weight 1.0."""
        session = self._mock_session(
            [{"flavor": "cpu", "total_cpu": 256000, "total_memory": 1048576, "total_gpu": 0}],
            [],  # no quota rows
        )
        settings = _fetch_settings(batch_size=50, fetch_multiplier=10)

        fetch_dispatchable_jobs(session, settings)

        main_call = session.execute.call_args_list[3]
        params = main_call.args[1]
        assert params["f_0"] == "cpu"
        assert params["cpu_0"] == 256000.0
        assert params["mem_0"] == 1048576.0
        assert params["w_0"] == 1.0

    def test_per_flavor_caps_multi_flavor(self):
        """Multiple flavors should each have their own per-flavor params."""
        session = self._mock_session(
            [
                {"flavor": "cpu", "total_cpu": 256000, "total_memory": 1048576, "total_gpu": 0},
                {"flavor": "gpu", "total_cpu": 64000, "total_memory": 524288, "total_gpu": 4},
            ],
            [
                {"flavor": "cpu", "cpu": "200", "memory": "500Gi", "gpu": "0", "drf_weight": 1.0},
                {"flavor": "gpu", "cpu": "128", "memory": "1000Gi", "gpu": "4", "drf_weight": 1.0},
            ],
        )
        settings = _fetch_settings(batch_size=50, fetch_multiplier=10)

        fetch_dispatchable_jobs(session, settings)

        main_call = session.execute.call_args_list[3]
        params = main_call.args[1]
        # Collect per-flavor values from params
        flavor_params = {}
        i = 0
        while f"f_{i}" in params:
            flavor_params[params[f"f_{i}"]] = {
                "cpu": params[f"cpu_{i}"],
                "mem": params[f"mem_{i}"],
                "gpu": params[f"gpu_{i}"],
            }
            i += 1
        assert flavor_params["cpu"]["cpu"] == 200000.0
        assert flavor_params["cpu"]["mem"] == 512000.0
        assert flavor_params["gpu"]["cpu"] == 64000.0
        assert flavor_params["gpu"]["gpu"] == 4.0

    def test_per_flavor_caps_weighted_multi_flavor(self):
        """DRF weight should be passed per flavor (not multiplied into capacity)."""
        session = self._mock_session(
            [
                {"flavor": "cpu", "total_cpu": 128000, "total_memory": 524288, "total_gpu": 0},
                {"flavor": "gpu", "total_cpu": 64000, "total_memory": 262144, "total_gpu": 4},
            ],
            [
                {"flavor": "cpu", "cpu": "256", "memory": "1000Gi", "gpu": "0", "drf_weight": 1.0},
                {"flavor": "gpu", "cpu": "128", "memory": "500Gi", "gpu": "8", "drf_weight": 2.0},
            ],
        )
        settings = _fetch_settings(batch_size=50, fetch_multiplier=10)

        fetch_dispatchable_jobs(session, settings)

        main_call = session.execute.call_args_list[3]
        params = main_call.args[1]
        # Collect per-flavor values from params
        flavor_params = {}
        i = 0
        while f"f_{i}" in params:
            flavor_params[params[f"f_{i}"]] = {
                "cpu": params[f"cpu_{i}"],
                "mem": params[f"mem_{i}"],
                "gpu": params[f"gpu_{i}"],
                "weight": params[f"w_{i}"],
            }
            i += 1
        # cpu: MIN(128000, 256000) = 128000, weight = 1.0
        assert flavor_params["cpu"]["cpu"] == 128000.0
        assert flavor_params["cpu"]["weight"] == 1.0
        # gpu: MIN(64000, 128000) = 64000, weight = 2.0
        assert flavor_params["gpu"]["cpu"] == 64000.0
        assert flavor_params["gpu"]["weight"] == 2.0
