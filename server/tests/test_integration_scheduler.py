"""Integration tests for dispatcher/scheduler.py against real PostgreSQL.

These tests exercise PostgreSQL-specific SQL (CTEs, ROW_NUMBER, GREATEST,
MAKE_INTERVAL, EXTRACT, FULL OUTER JOIN, NULLS FIRST) that cannot run on
SQLite.  They require a Docker-compatible runtime (Docker Desktop / Colima).

Run with:
    cd server && uv run --extra integration python -m pytest -m integration -v
"""

from datetime import timedelta

import pytest
from sqlalchemy import text

from cjob.dispatcher.scheduler import (
    _cleanup_old_usage,
    estimate_shortest_remaining,
    fetch_dispatchable_jobs,
    fetch_stalled_jobs,
    increment_retry,
)
from cjob.models import Job, NamespaceDailyUsage

pytestmark = pytest.mark.integration

NS_ALICE = "ns-alice"
NS_BOB = "ns-bob"


# ── helpers ──


def _insert_job(session, job_id, namespace=NS_ALICE, status="QUEUED", **kwargs):
    defaults = dict(
        namespace=namespace,
        job_id=job_id,
        user="testuser",
        image="test:1.0",
        command="echo test",
        cwd="/home/jovyan",
        env_json={},
        cpu="1",
        memory="1Gi",
        gpu=0,
        flavor="cpu",
        time_limit_seconds=86400,
        status=status,
        log_dir=f"/home/jovyan/.cjob/logs/{job_id}",
        cpu_millicores=1000,
        memory_mib=1024,
    )
    defaults.update(kwargs)
    job = Job(**defaults)
    session.add(job)
    session.flush()
    return job


def _insert_node(session, node_name, flavor="cpu", cpu=256000, mem=1024000, gpu=0):
    session.execute(
        text(
            "INSERT INTO node_resources (node_name, flavor, cpu_millicores, memory_mib, gpu) "
            "VALUES (:name, :flavor, :cpu, :mem, :gpu)"
        ),
        {"name": node_name, "flavor": flavor, "cpu": cpu, "mem": mem, "gpu": gpu},
    )
    session.flush()


def _insert_quota(session, flavor="cpu", cpu="256", mem="1000Gi", gpu="0", weight=1.0):
    session.execute(
        text(
            "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu, drf_weight) "
            "VALUES (:flavor, :cpu, :mem, :gpu, :weight)"
        ),
        {"flavor": flavor, "cpu": cpu, "mem": mem, "gpu": gpu, "weight": weight},
    )
    session.flush()


def _insert_usage(session, namespace, flavor="cpu", cpu_ms=0, mem_ms=0, gpu_s=0,
                  days_ago=0):
    """Insert a namespace_daily_usage row with usage_date relative to today."""
    session.execute(
        text(
            "INSERT INTO namespace_daily_usage "
            "(namespace, usage_date, flavor, cpu_millicores_seconds, "
            "memory_mib_seconds, gpu_seconds) "
            "VALUES (:ns, CURRENT_DATE - :days_ago, :flavor, :cpu, :mem, :gpu)"
        ),
        {"ns": namespace, "flavor": flavor, "cpu": cpu_ms, "mem": mem_ms,
         "gpu": gpu_s, "days_ago": days_ago},
    )
    session.flush()


def _insert_weight(session, namespace, weight):
    session.execute(
        text(
            "INSERT INTO namespace_weights (namespace, weight) "
            "VALUES (:ns, :weight)"
        ),
        {"ns": namespace, "weight": weight},
    )
    session.flush()


def _pg_now(session):
    """Read NOW() from PostgreSQL."""
    return session.execute(text("SELECT NOW() AS now")).mappings().first()["now"]


def _pg_today(session):
    """Read CURRENT_DATE from PostgreSQL."""
    return session.execute(
        text("SELECT CURRENT_DATE AS today")
    ).mappings().first()["today"]


# ── _cleanup_old_usage ──


class TestCleanupOldUsage:
    def test_deletes_rows_outside_retention_window(self, pg_session, pg_settings):
        # Outside retention (8 days ago, retention=7)
        _insert_usage(pg_session, NS_ALICE, days_ago=8, cpu_ms=1000)
        # Inside retention (6 days ago)
        _insert_usage(pg_session, NS_ALICE, days_ago=6, cpu_ms=2000)

        _cleanup_old_usage(pg_session, pg_settings)

        rows = pg_session.execute(
            text("SELECT * FROM namespace_daily_usage WHERE namespace = :ns"),
            {"ns": NS_ALICE},
        ).mappings().all()
        assert len(rows) == 1
        assert rows[0]["cpu_millicores_seconds"] == 2000

    def test_boundary_date_is_deleted(self, pg_session, pg_settings):
        # Exactly at retention boundary (condition is <=)
        _insert_usage(pg_session, NS_ALICE, days_ago=pg_settings.USAGE_RETENTION_DAYS)
        # One day inside retention
        _insert_usage(
            pg_session, NS_ALICE, days_ago=pg_settings.USAGE_RETENTION_DAYS - 1,
            cpu_ms=100,
        )

        _cleanup_old_usage(pg_session, pg_settings)

        rows = pg_session.execute(
            text("SELECT * FROM namespace_daily_usage WHERE namespace = :ns"),
            {"ns": NS_ALICE},
        ).mappings().all()
        assert len(rows) == 1
        assert rows[0]["cpu_millicores_seconds"] == 100


# ── increment_retry ──


class TestIncrementRetry:
    def test_sets_retry_after_in_future(self, pg_session):
        _insert_job(pg_session, 1, status="DISPATCHING")
        pg_session.flush()

        now_before = _pg_now(pg_session)
        result = increment_retry(pg_session, NS_ALICE, 1, 60)
        now_after = _pg_now(pg_session)

        assert result is True
        job = pg_session.get(Job, (NS_ALICE, 1))
        assert job.status == "QUEUED"
        assert job.retry_after >= now_before + timedelta(seconds=55)
        assert job.retry_after <= now_after + timedelta(seconds=65)

    def test_increments_retry_count(self, pg_session):
        _insert_job(pg_session, 1, status="DISPATCHING")
        pg_session.execute(
            text("UPDATE jobs SET retry_count = 2 "
                 "WHERE namespace = :ns AND job_id = 1"),
            {"ns": NS_ALICE},
        )
        pg_session.flush()

        increment_retry(pg_session, NS_ALICE, 1, 30)

        job = pg_session.get(Job, (NS_ALICE, 1))
        assert job.retry_count == 3

    def test_no_op_when_not_dispatching(self, pg_session):
        _insert_job(pg_session, 1, status="QUEUED")
        pg_session.flush()

        result = increment_retry(pg_session, NS_ALICE, 1, 60)

        assert result is False


# ── fetch_stalled_jobs ──


class TestFetchStalledJobs:
    def test_returns_stalled_dispatched_jobs(self, pg_session):
        _insert_job(pg_session, 1, status="DISPATCHED")
        pg_session.execute(
            text("UPDATE jobs SET dispatched_at = NOW() - INTERVAL '600 seconds' "
                 "WHERE namespace = :ns AND job_id = 1"),
            {"ns": NS_ALICE},
        )
        pg_session.flush()

        stalled = fetch_stalled_jobs(pg_session, 300)
        assert len(stalled) == 1
        assert stalled[0].job_id == 1

    def test_ignores_recently_dispatched(self, pg_session):
        _insert_job(pg_session, 1, status="DISPATCHED")
        pg_session.execute(
            text("UPDATE jobs SET dispatched_at = NOW() "
                 "WHERE namespace = :ns AND job_id = 1"),
            {"ns": NS_ALICE},
        )
        pg_session.flush()

        stalled = fetch_stalled_jobs(pg_session, 300)
        assert len(stalled) == 0

    def test_ignores_non_dispatched_status(self, pg_session):
        _insert_job(pg_session, 1, status="RUNNING")
        pg_session.execute(
            text("UPDATE jobs SET dispatched_at = NOW() - INTERVAL '600 seconds' "
                 "WHERE namespace = :ns AND job_id = 1"),
            {"ns": NS_ALICE},
        )
        pg_session.flush()

        stalled = fetch_stalled_jobs(pg_session, 300)
        assert len(stalled) == 0


# ── estimate_shortest_remaining ──


class TestEstimateShortestRemaining:
    def test_returns_shortest_remaining(self, pg_session):
        # Job started 100s ago, time_limit=3600 -> remaining ~3500
        _insert_job(pg_session, 1, status="RUNNING", time_limit_seconds=3600)
        pg_session.execute(
            text("UPDATE jobs SET started_at = NOW() - INTERVAL '100 seconds' "
                 "WHERE namespace = :ns AND job_id = 1"),
            {"ns": NS_ALICE},
        )
        # Job started 1000s ago, time_limit=3600 -> remaining ~2600
        _insert_job(pg_session, 2, status="RUNNING", time_limit_seconds=3600)
        pg_session.execute(
            text("UPDATE jobs SET started_at = NOW() - INTERVAL '1000 seconds' "
                 "WHERE namespace = :ns AND job_id = 2"),
            {"ns": NS_ALICE},
        )
        pg_session.flush()

        remaining = estimate_shortest_remaining(pg_session, NS_ALICE, "cpu")

        assert remaining is not None
        assert 2590 <= remaining <= 2610

    def test_returns_none_when_no_running_jobs(self, pg_session):
        _insert_job(pg_session, 1, status="QUEUED")

        remaining = estimate_shortest_remaining(pg_session, NS_ALICE, "cpu")
        assert remaining is None

    def test_scoped_to_namespace_and_flavor(self, pg_session):
        # RUNNING job in different namespace
        _insert_job(pg_session, 1, namespace=NS_BOB, status="RUNNING",
                    time_limit_seconds=3600)
        pg_session.execute(
            text("UPDATE jobs SET started_at = NOW() - INTERVAL '100 seconds' "
                 "WHERE namespace = :ns AND job_id = 1"),
            {"ns": NS_BOB},
        )
        # RUNNING job in same namespace but different flavor
        _insert_job(pg_session, 1, namespace=NS_ALICE, status="RUNNING",
                    flavor="gpu", time_limit_seconds=3600)
        pg_session.execute(
            text("UPDATE jobs SET started_at = NOW() - INTERVAL '100 seconds' "
                 "WHERE namespace = :ns AND job_id = 1"),
            {"ns": NS_ALICE},
        )
        pg_session.flush()

        remaining = estimate_shortest_remaining(pg_session, NS_ALICE, "cpu")
        assert remaining is None


# ── fetch_dispatchable_jobs ──


class TestFetchDispatchableJobsFallback:
    """Tests for the fallback path (no node_resources -> no DRF)."""

    def test_namespace_order_when_no_node_resources(self, pg_session, pg_settings):
        _insert_job(pg_session, 1, namespace=NS_BOB)
        _insert_job(pg_session, 1, namespace=NS_ALICE)

        jobs = fetch_dispatchable_jobs(pg_session, pg_settings)

        assert len(jobs) == 2
        assert jobs[0].namespace == NS_ALICE
        assert jobs[1].namespace == NS_BOB

    def test_budget_exhausted_returns_empty(self, pg_session, pg_settings):
        pg_settings.DISPATCH_BUDGET_PER_NAMESPACE = 2
        # 2 active (budget full)
        _insert_job(pg_session, 1, status="RUNNING")
        _insert_job(pg_session, 2, status="RUNNING")
        # 1 queued (should not be returned)
        _insert_job(pg_session, 3, status="QUEUED")

        jobs = fetch_dispatchable_jobs(pg_session, pg_settings)

        assert len(jobs) == 0


class TestFetchDispatchableJobsDRF:
    """Tests for the DRF path (node_resources populated)."""

    def test_drf_prioritizes_low_consumption_namespace(self, pg_session, pg_settings):
        _insert_node(pg_session, "node-1", flavor="cpu")
        _insert_quota(pg_session, flavor="cpu")
        # Alice has high usage, Bob has none
        _insert_usage(pg_session, NS_ALICE, cpu_ms=100_000_000, mem_ms=50_000_000)
        _insert_job(pg_session, 1, namespace=NS_ALICE)
        _insert_job(pg_session, 1, namespace=NS_BOB)

        # Use large round_size so DRF controls ordering
        pg_settings.DISPATCH_ROUND_SIZE = pg_settings.DISPATCH_BUDGET_PER_NAMESPACE

        jobs = fetch_dispatchable_jobs(pg_session, pg_settings)

        assert len(jobs) == 2
        # Bob (zero usage) should come first
        assert jobs[0].namespace == NS_BOB
        assert jobs[1].namespace == NS_ALICE

    def test_drf_round_robin_interleaving(self, pg_session, pg_settings):
        _insert_node(pg_session, "node-1", flavor="cpu")
        _insert_quota(pg_session, flavor="cpu")
        pg_settings.DISPATCH_ROUND_SIZE = 1

        for i in range(1, 4):
            _insert_job(pg_session, i, namespace=NS_ALICE)
            _insert_job(pg_session, i, namespace=NS_BOB)

        jobs = fetch_dispatchable_jobs(pg_session, pg_settings)

        # With round_size=1, jobs alternate between namespaces
        assert len(jobs) == 6
        namespaces = [j.namespace for j in jobs]
        # Each round has 1 job from each namespace
        round_1 = set(namespaces[0:2])
        round_2 = set(namespaces[2:4])
        round_3 = set(namespaces[4:6])
        assert round_1 == {NS_ALICE, NS_BOB}
        assert round_2 == {NS_ALICE, NS_BOB}
        assert round_3 == {NS_ALICE, NS_BOB}

    def test_drf_weight_amplification(self, pg_session, pg_settings):
        _insert_node(pg_session, "node-1", flavor="cpu")
        _insert_quota(pg_session, flavor="cpu")
        pg_settings.DISPATCH_ROUND_SIZE = pg_settings.DISPATCH_BUDGET_PER_NAMESPACE

        # Both namespaces have equal usage
        _insert_usage(pg_session, NS_ALICE, cpu_ms=10_000_000, mem_ms=5_000_000)
        _insert_usage(pg_session, NS_BOB, cpu_ms=10_000_000, mem_ms=5_000_000)
        # Alice has weight=2 (drf_score / 2), Bob has weight=1 (drf_score / 1)
        _insert_weight(pg_session, NS_ALICE, 2.0)
        _insert_weight(pg_session, NS_BOB, 1.0)

        _insert_job(pg_session, 1, namespace=NS_ALICE)
        _insert_job(pg_session, 1, namespace=NS_BOB)

        jobs = fetch_dispatchable_jobs(pg_session, pg_settings)

        assert len(jobs) == 2
        # Alice's effective score is lower (score/2 < score/1)
        assert jobs[0].namespace == NS_ALICE
        assert jobs[1].namespace == NS_BOB

    def test_drf_budget_per_flavor(self, pg_session, pg_settings):
        _insert_node(pg_session, "node-1", flavor="cpu")
        _insert_quota(pg_session, flavor="cpu")
        pg_settings.DISPATCH_BUDGET_PER_NAMESPACE = 2

        # 2 active jobs (budget full for cpu flavor)
        _insert_job(pg_session, 1, status="RUNNING")
        _insert_job(pg_session, 2, status="RUNNING")
        # 1 queued job in same flavor
        _insert_job(pg_session, 3, status="QUEUED")

        jobs = fetch_dispatchable_jobs(pg_session, pg_settings)

        assert len(jobs) == 0

    def test_drf_zero_weight_excluded(self, pg_session, pg_settings):
        _insert_node(pg_session, "node-1", flavor="cpu")
        _insert_quota(pg_session, flavor="cpu")
        _insert_weight(pg_session, NS_ALICE, 0)
        _insert_job(pg_session, 1, namespace=NS_ALICE)
        _insert_job(pg_session, 1, namespace=NS_BOB)

        jobs = fetch_dispatchable_jobs(pg_session, pg_settings)

        assert len(jobs) == 1
        assert jobs[0].namespace == NS_BOB

    def test_drf_in_flight_counted_in_score(self, pg_session, pg_settings):
        _insert_node(pg_session, "node-1", flavor="cpu")
        _insert_quota(pg_session, flavor="cpu")
        pg_settings.DISPATCH_ROUND_SIZE = pg_settings.DISPATCH_BUDGET_PER_NAMESPACE

        # Alice has a DISPATCHING job (in-flight, large resource cost)
        _insert_job(pg_session, 1, namespace=NS_ALICE, status="DISPATCHING",
                    cpu_millicores=10000, memory_mib=10000,
                    time_limit_seconds=3600)
        # Alice has a QUEUED job
        _insert_job(pg_session, 2, namespace=NS_ALICE, status="QUEUED")
        # Bob has a QUEUED job and no in-flight
        _insert_job(pg_session, 1, namespace=NS_BOB, status="QUEUED")

        jobs = fetch_dispatchable_jobs(pg_session, pg_settings)

        queued_jobs = [j for j in jobs if j.status == "QUEUED"]
        assert len(queued_jobs) == 2
        # Bob should be prioritized (lower DRF score)
        assert queued_jobs[0].namespace == NS_BOB
        assert queued_jobs[1].namespace == NS_ALICE

    def test_retry_after_future_excludes_job(self, pg_session, pg_settings):
        _insert_node(pg_session, "node-1", flavor="cpu")
        _insert_quota(pg_session, flavor="cpu")

        _insert_job(pg_session, 1, namespace=NS_ALICE)
        # Set retry_after far in the future
        pg_session.execute(
            text("UPDATE jobs SET retry_after = NOW() + INTERVAL '3600 seconds' "
                 "WHERE namespace = :ns AND job_id = 1"),
            {"ns": NS_ALICE},
        )
        pg_session.flush()

        jobs = fetch_dispatchable_jobs(pg_session, pg_settings)

        assert len(jobs) == 0

    def test_cleanup_old_usage_called(self, pg_session, pg_settings):
        _insert_node(pg_session, "node-1", flavor="cpu")
        _insert_quota(pg_session, flavor="cpu")

        # Insert old usage outside retention window
        _insert_usage(pg_session, NS_ALICE, days_ago=8, cpu_ms=999)
        _insert_job(pg_session, 1, namespace=NS_ALICE)

        fetch_dispatchable_jobs(pg_session, pg_settings)

        rows = pg_session.execute(
            text("SELECT * FROM namespace_daily_usage "
                 "WHERE namespace = :ns AND cpu_millicores_seconds = 999"),
            {"ns": NS_ALICE},
        ).mappings().all()
        assert len(rows) == 0
