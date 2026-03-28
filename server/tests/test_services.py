from unittest.mock import patch

import pytest
from fastapi import HTTPException

from cjob.api.schemas import JobSubmitRequest, ResourceSpec
from cjob.api.services import (
    cancel_bulk,
    cancel_single,
    delete_jobs,
    get_job,
    get_usage,
    list_jobs,
    reset,
    submit_job,
)
from cjob.models import Job, JobEvent, NamespaceDailyUsage


NS = "user-alice"

_next_id = 0


@pytest.fixture(autouse=True)
def _mock_allocate_and_events(db_session):
    """Mock allocate_job_id (PostgreSQL-specific SQL) and suppress JobEvent
    insertion (BIGSERIAL incompatible with SQLite) for unit testing."""
    global _next_id
    _next_id = 0

    def fake_allocate(session, namespace):
        global _next_id
        _next_id += 1
        return _next_id

    with patch("cjob.api.services.allocate_job_id", side_effect=fake_allocate):
        # Suppress JobEvent additions by making session.add skip JobEvent objects
        original_add = db_session.add

        def filtered_add(obj):
            if isinstance(obj, JobEvent):
                return
            original_add(obj)

        db_session.add = filtered_add
        yield


def _make_request(**overrides):
    defaults = dict(
        command="python main.py",
        image="test:1.0",
        cwd="/home/jovyan",
        env={},
        resources=ResourceSpec(cpu="1", memory="1Gi", gpu=0),
    )
    defaults.update(overrides)
    return JobSubmitRequest(**defaults)


def _insert_job(session, job_id, status="QUEUED", namespace=NS, **kwargs):
    defaults = dict(
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
        time_limit_seconds=86400,
        status=status,
        log_dir=f"/home/jovyan/.cjob/logs/{job_id}",
    )
    defaults.update(kwargs)
    job = Job(**defaults)
    session.add(job)
    session.flush()
    return job


# ── submit_job ──


class TestSubmitJob:
    def test_basic_submit(self, db_session):
        req = _make_request()
        resp = submit_job(db_session, NS, req)
        assert resp.job_id == 1
        assert resp.status == "QUEUED"

    def test_sequential_ids(self, db_session):
        req = _make_request()
        r1 = submit_job(db_session, NS, req)
        r2 = submit_job(db_session, NS, req)
        assert r1.job_id == 1
        assert r2.job_id == 2

    def test_time_limit_default(self, db_session):
        req = _make_request()
        resp = submit_job(db_session, NS, req)
        job = db_session.get(Job, (NS, resp.job_id))
        assert job.time_limit_seconds == 86400

    def test_time_limit_custom(self, db_session):
        req = _make_request(time_limit_seconds=3600)
        resp = submit_job(db_session, NS, req)
        job = db_session.get(Job, (NS, resp.job_id))
        assert job.time_limit_seconds == 3600

    def test_time_limit_exceeds_max(self, db_session):
        req = _make_request(time_limit_seconds=999999)
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, req)
        assert exc_info.value.status_code == 400
        assert "604800" in exc_info.value.detail

    def test_time_limit_zero(self, db_session):
        req = _make_request(time_limit_seconds=0)
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, req)
        assert exc_info.value.status_code == 400

    def test_time_limit_negative(self, db_session):
        req = _make_request(time_limit_seconds=-1)
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, req)
        assert exc_info.value.status_code == 400

    def test_gpu_rejected(self, db_session):
        req = _make_request(resources=ResourceSpec(cpu="1", memory="1Gi", gpu=1))
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, req)
        assert exc_info.value.status_code == 400

    def test_deleting_blocks_submit(self, db_session):
        _insert_job(db_session, 1, status="DELETING")
        req = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, req)
        assert exc_info.value.status_code == 409

    def test_job_count_limit(self, db_session, settings):
        for i in range(settings.MAX_QUEUED_JOBS_PER_NAMESPACE):
            _insert_job(db_session, i + 1, status="QUEUED")
        req = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, req)
        assert exc_info.value.status_code == 429

    def test_cpu_exceeds_max_node(self, db_session):
        """Job requesting more CPU than the largest node should be rejected."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 32000, 131072, 0)"
            )
        )
        db_session.flush()
        req = _make_request(resources=ResourceSpec(cpu="64", memory="1Gi", gpu=0))
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, req)
        assert exc_info.value.status_code == 400
        assert "CPU" in exc_info.value.detail

    def test_memory_exceeds_max_node(self, db_session):
        """Job requesting more memory than the largest node should be rejected."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 64000, 131072, 0)"
            )
        )
        db_session.flush()
        req = _make_request(resources=ResourceSpec(cpu="1", memory="256Gi", gpu=0))
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, req)
        assert exc_info.value.status_code == 400
        assert "メモリ" in exc_info.value.detail

    def test_resource_within_max_node(self, db_session):
        """Job within node limits should be accepted."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 64000, 262144, 0)"
            )
        )
        db_session.flush()
        req = _make_request(resources=ResourceSpec(cpu="32", memory="128Gi", gpu=0))
        resp = submit_job(db_session, NS, req)
        assert resp.status == "QUEUED"

    def test_resource_check_uses_max_across_nodes(self, db_session):
        """Resource check should use the maximum across all nodes."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('small', 8000, 32768, 0), ('large', 64000, 262144, 0)"
            )
        )
        db_session.flush()
        # 32 cores: exceeds small node but fits large node → should be accepted
        req = _make_request(resources=ResourceSpec(cpu="32", memory="1Gi", gpu=0))
        resp = submit_job(db_session, NS, req)
        assert resp.status == "QUEUED"

    def test_resource_check_skipped_when_empty(self, db_session):
        """When node_resources is empty, resource validation should be skipped."""
        req = _make_request(resources=ResourceSpec(cpu="9999", memory="1Gi", gpu=0))
        resp = submit_job(db_session, NS, req)
        assert resp.status == "QUEUED"


# ── list_jobs ──


class TestListJobs:
    def test_empty(self, db_session):
        resp = list_jobs(db_session, NS)
        assert resp.jobs == []
        assert resp.total_count == 0
        assert resp.log_base_dir == "/home/jovyan/.cjob/logs"

    def test_returns_jobs(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")
        _insert_job(db_session, 2, status="RUNNING")
        resp = list_jobs(db_session, NS)
        assert resp.total_count == 2
        assert [j.job_id for j in resp.jobs] == [1, 2]

    def test_filter_by_status(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")
        _insert_job(db_session, 2, status="RUNNING")
        resp = list_jobs(db_session, NS, status="RUNNING")
        assert resp.total_count == 1
        assert resp.jobs[0].status == "RUNNING"

    def test_limit(self, db_session):
        for i in range(5):
            _insert_job(db_session, i + 1)
        resp = list_jobs(db_session, NS, limit=3)
        assert len(resp.jobs) == 3
        assert resp.total_count == 5
        # Returns newest 3, sorted asc
        assert [j.job_id for j in resp.jobs] == [3, 4, 5]

    def test_order_desc(self, db_session):
        for i in range(3):
            _insert_job(db_session, i + 1)
        resp = list_jobs(db_session, NS, order="desc")
        assert [j.job_id for j in resp.jobs] == [3, 2, 1]

    def test_namespace_isolation(self, db_session):
        _insert_job(db_session, 1, namespace="user-alice")
        _insert_job(db_session, 1, namespace="user-bob")
        resp = list_jobs(db_session, "user-alice")
        assert resp.total_count == 1


# ── get_job ──


class TestGetJob:
    def test_found(self, db_session):
        _insert_job(db_session, 1, time_limit_seconds=3600)
        resp = get_job(db_session, NS, 1)
        assert resp is not None
        assert resp.job_id == 1
        assert resp.time_limit_seconds == 3600
        assert resp.started_at is None

    def test_not_found(self, db_session):
        resp = get_job(db_session, NS, 999)
        assert resp is None

    def test_wrong_namespace(self, db_session):
        _insert_job(db_session, 1, namespace="user-bob")
        resp = get_job(db_session, NS, 1)
        assert resp is None


# ── cancel_single / cancel_bulk ──


class TestCancel:
    def test_cancel_queued(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")
        result = cancel_single(db_session, NS, 1)
        assert result["status"] == "CANCELLED"

    def test_cancel_running(self, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        result = cancel_single(db_session, NS, 1)
        assert result["status"] == "CANCELLED"

    def test_cancel_succeeded_skipped(self, db_session):
        _insert_job(db_session, 1, status="SUCCEEDED")
        result = cancel_single(db_session, NS, 1)
        assert result.get("skipped") is True

    def test_cancel_not_found(self, db_session):
        result = cancel_single(db_session, NS, 999)
        assert result.get("not_found") is True

    def test_cancel_bulk(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")
        _insert_job(db_session, 2, status="SUCCEEDED")
        resp = cancel_bulk(db_session, NS, [1, 2, 999])
        assert resp.cancelled == [1]
        assert resp.skipped == [2]
        assert resp.not_found == [999]


# ── delete_jobs ──


class TestDeleteJobs:
    def test_delete_completed(self, db_session):
        _insert_job(db_session, 1, status="SUCCEEDED")
        resp = delete_jobs(db_session, NS, [1])
        assert resp.deleted == [1]
        assert resp.log_dirs == ["/home/jovyan/.cjob/logs/1"]

    def test_delete_running_skipped(self, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        resp = delete_jobs(db_session, NS, [1])
        assert len(resp.skipped) == 1
        assert resp.skipped[0].reason == "running"
        assert resp.log_dirs == []

    def test_delete_deleting_skipped(self, db_session):
        _insert_job(db_session, 1, status="DELETING")
        resp = delete_jobs(db_session, NS, [1])
        assert len(resp.skipped) == 1
        assert resp.skipped[0].reason == "deleting"
        assert resp.log_dirs == []

    def test_delete_not_found(self, db_session):
        resp = delete_jobs(db_session, NS, [999])
        assert resp.not_found == [999]
        assert resp.log_dirs == []

    def test_delete_all(self, db_session):
        _insert_job(db_session, 1, status="SUCCEEDED")
        _insert_job(db_session, 2, status="FAILED")
        _insert_job(db_session, 3, status="RUNNING")
        resp = delete_jobs(db_session, NS, None)
        assert sorted(resp.deleted) == [1, 2]
        assert len(resp.skipped) == 1
        assert sorted(resp.log_dirs) == ["/home/jovyan/.cjob/logs/1", "/home/jovyan/.cjob/logs/2"]


# ── reset ──


class TestReset:
    def test_reset_success(self, db_session):
        _insert_job(db_session, 1, status="SUCCEEDED")
        _insert_job(db_session, 2, status="FAILED")
        code, body = reset(db_session, NS)
        assert code == 202
        # Verify jobs are now DELETING
        jobs = db_session.query(Job).filter(Job.namespace == NS).all()
        assert all(j.status == "DELETING" for j in jobs)

    def test_reset_blocked_by_active(self, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        _insert_job(db_session, 2, status="SUCCEEDED")
        code, body = reset(db_session, NS)
        assert code == 409
        assert 1 in body["blocking_job_ids"]

    def test_reset_blocked_by_deleting(self, db_session):
        _insert_job(db_session, 1, status="DELETING")
        code, body = reset(db_session, NS)
        assert code == 409
        assert "進行中" in body["message"]


# ── get_usage ──


def _insert_usage(session, namespace, usage_date, cpu=0, mem=0, gpu=0):
    from datetime import date

    if isinstance(usage_date, str):
        usage_date = date.fromisoformat(usage_date)
    usage = NamespaceDailyUsage(
        namespace=namespace,
        usage_date=usage_date,
        cpu_millicores_seconds=cpu,
        memory_mib_seconds=mem,
        gpu_seconds=gpu,
    )
    session.add(usage)
    session.flush()


class TestGetUsage:
    def test_empty(self, db_session):
        resp = get_usage(db_session, NS)
        assert resp.window_days == 7
        assert resp.daily == []
        assert resp.total_cpu_millicores_seconds == 0
        assert resp.total_memory_mib_seconds == 0
        assert resp.total_gpu_seconds == 0

    def test_returns_daily_records(self, db_session):
        # Use dates far in the future to ensure they pass the window filter.
        # SQLite's CURRENT_DATE - int is integer subtraction, not date
        # arithmetic, but future dates will always be "greater than" the result.
        _insert_usage(db_session, NS, "2099-01-01", cpu=1000, mem=2000, gpu=100)
        _insert_usage(db_session, NS, "2099-01-02", cpu=3000, mem=4000, gpu=200)

        resp = get_usage(db_session, NS)
        assert len(resp.daily) == 2
        assert resp.total_cpu_millicores_seconds == 4000
        assert resp.total_memory_mib_seconds == 6000
        assert resp.total_gpu_seconds == 300

    def test_namespace_isolation(self, db_session):
        _insert_usage(db_session, NS, "2099-01-01", cpu=1000, mem=2000, gpu=0)
        _insert_usage(db_session, "user-bob", "2099-01-01", cpu=9999, mem=9999, gpu=0)

        resp = get_usage(db_session, NS)
        assert len(resp.daily) == 1
        assert resp.total_cpu_millicores_seconds == 1000
