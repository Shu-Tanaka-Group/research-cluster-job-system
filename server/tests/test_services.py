from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from cjob.api.schemas import JobSubmitRequest, ResourceSpec, SweepSubmitRequest
from cjob.metrics import JOBS_COMPLETED_TOTAL, JOBS_SUBMITTED_TOTAL
from cjob.api.services import (
    cancel_bulk,
    cancel_single,
    delete_jobs,
    get_job,
    get_usage,
    hold_bulk,
    hold_single,
    list_flavors,
    list_jobs,
    release_bulk,
    release_single,
    reset,
    submit_job,
    submit_sweep,
)
from cjob.models import Job, JobEvent, NamespaceDailyUsage


NS = "alice"

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


# ── submit_job ──


class TestSubmitJob:
    def test_basic_submit(self, db_session):
        req = _make_request()
        resp = submit_job(db_session, NS, "alice", req)
        assert resp.job_id == 1
        assert resp.status == "QUEUED"

    def test_sequential_ids(self, db_session):
        req = _make_request()
        r1 = submit_job(db_session, NS, "alice", req)
        r2 = submit_job(db_session, NS, "alice", req)
        assert r1.job_id == 1
        assert r2.job_id == 2

    def test_time_limit_default(self, db_session):
        req = _make_request()
        resp = submit_job(db_session, NS, "alice", req)
        job = db_session.get(Job, (NS, resp.job_id))
        assert job.time_limit_seconds == 86400

    def test_time_limit_custom(self, db_session):
        req = _make_request(time_limit_seconds=3600)
        resp = submit_job(db_session, NS, "alice", req)
        job = db_session.get(Job, (NS, resp.job_id))
        assert job.time_limit_seconds == 3600

    def test_time_limit_exceeds_max(self, db_session):
        req = _make_request(time_limit_seconds=999999)
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "604800" in exc_info.value.detail

    def test_time_limit_zero(self, db_session):
        req = _make_request(time_limit_seconds=0)
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400

    def test_time_limit_negative(self, db_session):
        req = _make_request(time_limit_seconds=-1)
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400

    def test_gpu_accepted(self, db_session):
        """GPU job should be accepted when GPU nodes exist in the gpu flavor."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('gpu-node', 32000, 131072, 4, 'gpu')"
            )
        )
        db_session.flush()
        req = _make_request(resources=ResourceSpec(cpu="1", memory="1Gi", gpu=1, flavor="gpu"))
        resp = submit_job(db_session, NS, "alice", req)
        assert resp.status == "QUEUED"

    def test_gpu_no_gpu_nodes(self, db_session):
        """GPU job should be rejected when no GPU nodes exist in the gpu flavor."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('cpu-node', 32000, 131072, 0, 'cpu')"
            )
        )
        db_session.flush()
        req = _make_request(resources=ResourceSpec(cpu="1", memory="1Gi", gpu=1, flavor="gpu"))
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "GPU ノード" in exc_info.value.detail

    def test_gpu_on_cpu_flavor_rejected(self, db_session):
        """GPU job on a flavor without gpu_resource_name should be rejected."""
        req = _make_request(resources=ResourceSpec(cpu="1", memory="1Gi", gpu=1, flavor="cpu"))
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "GPU をサポートしていません" in exc_info.value.detail

    def test_unknown_flavor_rejected(self, db_session):
        """Job with an unknown flavor should be rejected."""
        req = _make_request(resources=ResourceSpec(cpu="1", memory="1Gi", flavor="nonexistent"))
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "nonexistent" in exc_info.value.detail

    def test_gpu_exceeds_max_node(self, db_session):
        """GPU job requesting more GPUs than max node should be rejected."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('gpu-node', 32000, 131072, 2, 'gpu')"
            )
        )
        db_session.flush()
        req = _make_request(resources=ResourceSpec(cpu="1", memory="1Gi", gpu=4, flavor="gpu"))
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "GPU" in exc_info.value.detail

    def test_flavor_stored_in_job(self, db_session):
        """Flavor should be stored in the Job record."""
        req = _make_request(resources=ResourceSpec(cpu="1", memory="1Gi", flavor="cpu"))
        resp = submit_job(db_session, NS, "alice", req)
        job = db_session.get(Job, (NS, resp.job_id))
        assert job.flavor == "cpu"

    def test_default_flavor(self, db_session):
        """When flavor is omitted, DEFAULT_FLAVOR should be used."""
        req = _make_request(resources=ResourceSpec(cpu="1", memory="1Gi"))
        resp = submit_job(db_session, NS, "alice", req)
        job = db_session.get(Job, (NS, resp.job_id))
        assert job.flavor == "cpu"

    def test_deleting_blocks_submit(self, db_session):
        _insert_job(db_session, 1, status="DELETING")
        req = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 409

    def test_job_count_limit(self, db_session, settings):
        for i in range(settings.MAX_QUEUED_JOBS_PER_NAMESPACE):
            _insert_job(db_session, i + 1, status="QUEUED")
        req = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 429

    def test_running_jobs_not_counted_toward_limit(self, db_session, settings):
        """RUNNING jobs should not count toward MAX_QUEUED_JOBS_PER_NAMESPACE."""
        from unittest.mock import patch as _patch

        limit = settings.MAX_QUEUED_JOBS_PER_NAMESPACE
        for i in range(limit - 1):
            _insert_job(db_session, i + 1, status="QUEUED")
        _insert_job(db_session, limit, status="RUNNING")
        # limit - 1 QUEUED + 1 RUNNING = limit total rows, but RUNNING is excluded
        # Mock allocate_job_id to avoid counter/job_id collision
        with _patch("cjob.api.services.allocate_job_id", return_value=limit + 1):
            req = _make_request()
            result = submit_job(db_session, NS, "alice", req)
        assert result.status == "QUEUED"

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
            submit_job(db_session, NS, "alice", req)
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
            submit_job(db_session, NS, "alice", req)
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
        resp = submit_job(db_session, NS, "alice", req)
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
        resp = submit_job(db_session, NS, "alice", req)
        assert resp.status == "QUEUED"

    def test_resource_check_skipped_when_empty(self, db_session):
        """When node_resources is empty, resource validation should be skipped."""
        req = _make_request(resources=ResourceSpec(cpu="9999", memory="1Gi", gpu=0))
        resp = submit_job(db_session, NS, "alice", req)
        assert resp.status == "QUEUED"

    def test_cpu_millicores_and_memory_mib(self, db_session):
        """submit_job should set cpu_millicores and memory_mib from resource strings."""
        req = _make_request(resources=ResourceSpec(cpu="500m", memory="4Gi", gpu=0))
        resp = submit_job(db_session, NS, "alice", req)
        job = db_session.get(Job, (NS, resp.job_id))
        assert job.cpu_millicores == 500
        assert job.memory_mib == 4096

    def test_cpu_exceeds_quota(self, db_session):
        """Job within node allocatable but exceeding nominalQuota CPU should be rejected."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 64000, 262144, 0)"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu) "
                "VALUES ('cpu', '16', '1000Gi', '0')"
            )
        )
        db_session.flush()
        req = _make_request(resources=ResourceSpec(cpu="32", memory="1Gi", gpu=0))
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "クォータ" in exc_info.value.detail
        assert "CPU" in exc_info.value.detail

    def test_memory_exceeds_quota(self, db_session):
        """Job within node allocatable but exceeding nominalQuota memory should be rejected."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 64000, 262144, 0)"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu) "
                "VALUES ('cpu', '256', '64Gi', '0')"
            )
        )
        db_session.flush()
        req = _make_request(resources=ResourceSpec(cpu="1", memory="128Gi", gpu=0))
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "クォータ" in exc_info.value.detail
        assert "メモリ" in exc_info.value.detail

    def test_gpu_exceeds_quota(self, db_session):
        """Job within node GPU but exceeding nominalQuota GPU should be rejected."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('gpu-node', 32000, 131072, 8, 'gpu')"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu) "
                "VALUES ('gpu', '256', '1000Gi', '4')"
            )
        )
        db_session.flush()
        req = _make_request(resources=ResourceSpec(cpu="1", memory="1Gi", gpu=6, flavor="gpu"))
        with pytest.raises(HTTPException) as exc_info:
            submit_job(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "クォータ" in exc_info.value.detail
        assert "GPU" in exc_info.value.detail

    def test_resource_within_quota_and_node(self, db_session):
        """Job within both node allocatable and nominalQuota should be accepted."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 64000, 262144, 0)"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu) "
                "VALUES ('cpu', '32', '128Gi', '0')"
            )
        )
        db_session.flush()
        req = _make_request(resources=ResourceSpec(cpu="16", memory="64Gi", gpu=0))
        resp = submit_job(db_session, NS, "alice", req)
        assert resp.status == "QUEUED"

    def test_quota_missing_falls_back_to_node(self, db_session):
        """When flavor_quotas is empty, validation should use node_resources only."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 64000, 262144, 0)"
            )
        )
        db_session.flush()
        # Within node limits, no quota → should be accepted
        req = _make_request(resources=ResourceSpec(cpu="32", memory="128Gi", gpu=0))
        resp = submit_job(db_session, NS, "alice", req)
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
        _insert_job(db_session, 1, namespace="alice", user="alice")
        _insert_job(db_session, 1, namespace="bob", user="bob")
        resp = list_jobs(db_session, "alice")
        assert resp.total_count == 1

    def test_time_limit_ge(self, db_session):
        _insert_job(db_session, 1, time_limit_seconds=3600)
        _insert_job(db_session, 2, time_limit_seconds=21600)
        _insert_job(db_session, 3, time_limit_seconds=86400)
        resp = list_jobs(db_session, NS, time_limit_ge=21600)
        assert resp.total_count == 2
        assert [j.job_id for j in resp.jobs] == [2, 3]

    def test_time_limit_lt(self, db_session):
        _insert_job(db_session, 1, time_limit_seconds=3600)
        _insert_job(db_session, 2, time_limit_seconds=21600)
        _insert_job(db_session, 3, time_limit_seconds=86400)
        resp = list_jobs(db_session, NS, time_limit_lt=21600)
        assert resp.total_count == 1
        assert resp.jobs[0].job_id == 1

    def test_time_limit_range(self, db_session):
        _insert_job(db_session, 1, time_limit_seconds=3600)
        _insert_job(db_session, 2, time_limit_seconds=21600)
        _insert_job(db_session, 3, time_limit_seconds=86400)
        resp = list_jobs(db_session, NS, time_limit_ge=3600, time_limit_lt=86400)
        assert resp.total_count == 2
        assert [j.job_id for j in resp.jobs] == [1, 2]

    def test_time_limit_seconds_in_summary(self, db_session):
        _insert_job(db_session, 1, time_limit_seconds=7200)
        resp = list_jobs(db_session, NS)
        assert resp.jobs[0].time_limit_seconds == 7200


# ── get_job ──


class TestGetJob:
    def test_found(self, db_session):
        _insert_job(db_session, 1, time_limit_seconds=3600)
        resp = get_job(db_session, NS, 1)
        assert resp is not None
        assert resp.job_id == 1
        assert resp.time_limit_seconds == 3600
        assert resp.started_at is None
        assert resp.last_error is None

    def test_found_with_last_error(self, db_session):
        _insert_job(
            db_session, 1, status="FAILED",
            last_error="K8s API permanent error 403: Forbidden",
        )
        resp = get_job(db_session, NS, 1)
        assert resp is not None
        assert resp.last_error == "K8s API permanent error 403: Forbidden"

    def test_not_found(self, db_session):
        resp = get_job(db_session, NS, 999)
        assert resp is None

    def test_wrong_namespace(self, db_session):
        _insert_job(db_session, 1, namespace="bob", user="bob")
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

    def test_cancel_held(self, db_session):
        _insert_job(db_session, 1, status="HELD")
        result = cancel_single(db_session, NS, 1)
        assert result["status"] == "CANCELLED"


# ── hold_single / hold_bulk ──


class TestHold:
    def test_hold_queued(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")
        result = hold_single(db_session, NS, 1)
        assert result["status"] == "HELD"
        job = db_session.get(Job, (NS, 1))
        assert job.status == "HELD"

    def test_hold_running_skipped(self, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        result = hold_single(db_session, NS, 1)
        assert result.get("skipped") is True

    def test_hold_held_skipped(self, db_session):
        _insert_job(db_session, 1, status="HELD")
        result = hold_single(db_session, NS, 1)
        assert result.get("skipped") is True

    def test_hold_not_found(self, db_session):
        result = hold_single(db_session, NS, 999)
        assert result.get("not_found") is True

    def test_hold_bulk(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")
        _insert_job(db_session, 2, status="RUNNING")
        resp = hold_bulk(db_session, NS, [1, 2, 999])
        assert resp.held == [1]
        assert resp.skipped == [2]
        assert resp.not_found == [999]

    def test_hold_bulk_all(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")
        _insert_job(db_session, 2, status="QUEUED")
        _insert_job(db_session, 3, status="RUNNING")
        resp = hold_bulk(db_session, NS, None)
        assert sorted(resp.held) == [1, 2]
        assert resp.skipped == []
        assert resp.not_found == []

    def test_hold_bulk_all_empty(self, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        resp = hold_bulk(db_session, NS, None)
        assert resp.held == []


# ── release_single / release_bulk ──


class TestRelease:
    def test_release_held(self, db_session):
        _insert_job(db_session, 1, status="HELD")
        result = release_single(db_session, NS, 1)
        assert result["status"] == "QUEUED"
        job = db_session.get(Job, (NS, 1))
        assert job.status == "QUEUED"

    def test_release_queued_skipped(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")
        result = release_single(db_session, NS, 1)
        assert result.get("skipped") is True

    def test_release_not_found(self, db_session):
        result = release_single(db_session, NS, 999)
        assert result.get("not_found") is True

    def test_release_bulk(self, db_session):
        _insert_job(db_session, 1, status="HELD")
        _insert_job(db_session, 2, status="QUEUED")
        resp = release_bulk(db_session, NS, [1, 2, 999])
        assert resp.released == [1]
        assert resp.skipped == [2]
        assert resp.not_found == [999]

    def test_release_bulk_all(self, db_session):
        _insert_job(db_session, 1, status="HELD")
        _insert_job(db_session, 2, status="HELD")
        _insert_job(db_session, 3, status="QUEUED")
        resp = release_bulk(db_session, NS, None)
        assert sorted(resp.released) == [1, 2]
        assert resp.skipped == []
        assert resp.not_found == []

    def test_release_bulk_all_empty(self, db_session):
        _insert_job(db_session, 1, status="QUEUED")
        resp = release_bulk(db_session, NS, None)
        assert resp.released == []


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

    def test_delete_held_skipped(self, db_session):
        _insert_job(db_session, 1, status="HELD")
        resp = delete_jobs(db_session, NS, [1])
        assert len(resp.skipped) == 1
        assert resp.skipped[0].reason == "held"
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

    def test_reset_blocked_by_held(self, db_session):
        _insert_job(db_session, 1, status="HELD")
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
        _insert_usage(db_session, "bob", "2099-01-01", cpu=9999, mem=9999, gpu=0)

        resp = get_usage(db_session, NS)
        assert len(resp.daily) == 1
        assert resp.total_cpu_millicores_seconds == 1000

    def test_resource_quota_present(self, db_session):
        _insert_quota(db_session, NS, 300000, 1280000, 4, 280000, 819200, 1)

        resp = get_usage(db_session, NS)
        q = resp.resource_quota
        assert q is not None
        assert q.hard_cpu_millicores == 300000
        assert q.hard_memory_mib == 1280000
        assert q.hard_gpu == 4
        assert q.used_cpu_millicores == 280000
        assert q.used_memory_mib == 819200
        assert q.used_gpu == 1

    def test_resource_quota_absent(self, db_session):
        resp = get_usage(db_session, NS)
        assert resp.resource_quota is None

    def test_resource_quota_namespace_isolation(self, db_session):
        _insert_quota(db_session, "other-ns", 300000, 1280000, 4, 280000, 819200, 1)

        resp = get_usage(db_session, NS)
        assert resp.resource_quota is None


# ── submit_sweep ──


def _make_sweep_request(**overrides):
    defaults = dict(
        command="python main.py --trial $CJOB_INDEX",
        image="test:1.0",
        cwd="/home/jovyan",
        env={},
        resources=ResourceSpec(cpu="1", memory="1Gi", gpu=0),
        completions=10,
        parallelism=2,
    )
    defaults.update(overrides)
    return SweepSubmitRequest(**defaults)


class TestSubmitSweep:
    def test_basic_sweep(self, db_session):
        req = _make_sweep_request()
        resp = submit_sweep(db_session, NS, "alice", req)
        assert resp.job_id == 1
        assert resp.status == "QUEUED"
        job = db_session.get(Job, (NS, 1))
        assert job.completions == 10
        assert job.parallelism == 2
        assert job.succeeded_count == 0
        assert job.failed_count == 0

    def test_sweep_completions_zero(self, db_session):
        req = _make_sweep_request(completions=0)
        with pytest.raises(HTTPException) as exc_info:
            submit_sweep(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "completions" in exc_info.value.detail

    def test_sweep_completions_exceeds_max(self, db_session):
        req = _make_sweep_request(completions=9999)
        with pytest.raises(HTTPException) as exc_info:
            submit_sweep(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "1000" in exc_info.value.detail

    def test_sweep_parallelism_zero(self, db_session):
        req = _make_sweep_request(parallelism=0)
        with pytest.raises(HTTPException) as exc_info:
            submit_sweep(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "parallelism" in exc_info.value.detail

    def test_sweep_parallelism_exceeds_completions(self, db_session):
        req = _make_sweep_request(completions=5, parallelism=10)
        with pytest.raises(HTTPException) as exc_info:
            submit_sweep(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "parallelism" in exc_info.value.detail

    def test_sweep_cluster_resource_exceeded(self, db_session):
        from sqlalchemy import text
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 8000, 32768, 0)"
            )
        )
        db_session.flush()
        # parallelism=10, cpu=2 -> 20000m > 8000m cluster total
        req = _make_sweep_request(
            completions=100, parallelism=10,
            resources=ResourceSpec(cpu="2", memory="1Gi", gpu=0),
        )
        with pytest.raises(HTTPException) as exc_info:
            submit_sweep(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "CPU" in exc_info.value.detail

    def test_sweep_cluster_memory_exceeded(self, db_session):
        from sqlalchemy import text
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 64000, 32768, 0)"
            )
        )
        db_session.flush()
        # parallelism=10, memory=4Gi -> 40960Mi > 32768Mi cluster total
        req = _make_sweep_request(
            completions=100, parallelism=10,
            resources=ResourceSpec(cpu="1", memory="4Gi", gpu=0),
        )
        with pytest.raises(HTTPException) as exc_info:
            submit_sweep(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "メモリ" in exc_info.value.detail

    def test_sweep_deleting_blocks(self, db_session):
        _insert_job(db_session, 1, status="DELETING")
        req = _make_sweep_request()
        with pytest.raises(HTTPException) as exc_info:
            submit_sweep(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 409

    def test_sweep_gpu_accepted(self, db_session):
        """Sweep with GPU should be accepted when GPU nodes exist in the gpu flavor."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('gpu-node', 32000, 131072, 4, 'gpu')"
            )
        )
        db_session.flush()
        req = _make_sweep_request(
            completions=10, parallelism=2,
            resources=ResourceSpec(cpu="1", memory="1Gi", gpu=1, flavor="gpu"),
        )
        resp = submit_sweep(db_session, NS, "alice", req)
        assert resp.status == "QUEUED"

    def test_sweep_gpu_exceeds_flavor_total(self, db_session):
        """Sweep where parallelism * gpu exceeds flavor total should be rejected."""
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('gpu-node', 32000, 131072, 4, 'gpu')"
            )
        )
        db_session.flush()
        # parallelism=5, gpu=1 -> 5 > 4 flavor total GPU
        req = _make_sweep_request(
            completions=10, parallelism=5,
            resources=ResourceSpec(cpu="1", memory="1Gi", gpu=1, flavor="gpu"),
        )
        with pytest.raises(HTTPException) as exc_info:
            submit_sweep(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "GPU" in exc_info.value.detail

    def test_sweep_time_limit_default(self, db_session):
        req = _make_sweep_request()
        resp = submit_sweep(db_session, NS, "alice", req)
        job = db_session.get(Job, (NS, resp.job_id))
        assert job.time_limit_seconds == 86400

    def test_sweep_cluster_check_skipped_when_empty(self, db_session):
        """When node_resources is empty, cluster total check should be skipped."""
        req = _make_sweep_request(completions=100, parallelism=50,
                                   resources=ResourceSpec(cpu="32", memory="128Gi", gpu=0))
        resp = submit_sweep(db_session, NS, "alice", req)
        assert resp.status == "QUEUED"

    def test_sweep_cpu_millicores_and_memory_mib(self, db_session):
        """submit_sweep should set cpu_millicores and memory_mib from resource strings."""
        req = _make_sweep_request(resources=ResourceSpec(cpu="2", memory="8Gi", gpu=0))
        resp = submit_sweep(db_session, NS, "alice", req)
        job = db_session.get(Job, (NS, resp.job_id))
        assert job.cpu_millicores == 2000
        assert job.memory_mib == 8192

    def test_sweep_cpu_exceeds_quota(self, db_session):
        """Sweep within node total but exceeding nominalQuota should be rejected."""
        from sqlalchemy import text

        # Node has 64 cores total, but quota is only 16 cores
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 64000, 262144, 0)"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu) "
                "VALUES ('cpu', '16', '1000Gi', '0')"
            )
        )
        db_session.flush()
        # parallelism=10, cpu=2 -> 20000m > 16000m quota
        req = _make_sweep_request(
            completions=100, parallelism=10,
            resources=ResourceSpec(cpu="2", memory="1Gi", gpu=0),
        )
        with pytest.raises(HTTPException) as exc_info:
            submit_sweep(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "CPU" in exc_info.value.detail

    def test_sweep_gpu_exceeds_quota(self, db_session):
        """Sweep within node GPU total but exceeding nominalQuota GPU should be rejected."""
        from sqlalchemy import text

        # Node has 8 GPUs, but quota is only 4
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('gpu-node', 32000, 131072, 8, 'gpu')"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu) "
                "VALUES ('gpu', '256', '1000Gi', '4')"
            )
        )
        db_session.flush()
        # parallelism=5, gpu=1 -> 5 > 4 quota
        req = _make_sweep_request(
            completions=10, parallelism=5,
            resources=ResourceSpec(cpu="1", memory="1Gi", gpu=1, flavor="gpu"),
        )
        with pytest.raises(HTTPException) as exc_info:
            submit_sweep(db_session, NS, "alice", req)
        assert exc_info.value.status_code == 400
        assert "GPU" in exc_info.value.detail


# ── list_jobs / get_job with sweep fields ──


class TestSweepFieldsInResponses:
    def test_list_jobs_sweep_fields(self, db_session):
        _insert_job(db_session, 1, status="RUNNING",
                     completions=100, parallelism=10,
                     succeeded_count=48, failed_count=2)
        resp = list_jobs(db_session, NS)
        assert resp.jobs[0].completions == 100
        assert resp.jobs[0].parallelism == 10
        assert resp.jobs[0].succeeded_count == 48
        assert resp.jobs[0].failed_count == 2

    def test_list_jobs_normal_fields_null(self, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        resp = list_jobs(db_session, NS)
        assert resp.jobs[0].completions is None
        assert resp.jobs[0].parallelism is None

    def test_get_job_sweep_fields(self, db_session):
        _insert_job(db_session, 1, status="RUNNING",
                     completions=100, parallelism=10,
                     succeeded_count=48, failed_count=2,
                     completed_indexes="0-47",
                     failed_indexes="12,37")
        resp = get_job(db_session, NS, 1)
        assert resp.completions == 100
        assert resp.parallelism == 10
        assert resp.succeeded_count == 48
        assert resp.failed_count == 2
        assert resp.completed_indexes == "0-47"
        assert resp.failed_indexes == "12,37"

    def test_get_job_normal_fields_null(self, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        resp = get_job(db_session, NS, 1)
        assert resp.completions is None
        assert resp.completed_indexes is None


class TestListFlavors:
    def test_returns_quota_when_present(self, db_session):
        """list_flavors should include quota from flavor_quotas table."""
        from sqlalchemy import text as sql_text
        db_session.execute(
            sql_text(
                "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu) "
                "VALUES ('cpu', '256', '1000Gi', '0')"
            )
        )
        db_session.commit()

        resp = list_flavors(db_session)
        cpu_flavor = next(f for f in resp.flavors if f.name == "cpu")
        assert cpu_flavor.quota is not None
        assert cpu_flavor.quota.cpu == "256"
        assert cpu_flavor.quota.memory == "1000Gi"
        assert cpu_flavor.quota.gpu == "0"

    def test_returns_none_quota_when_absent(self, db_session):
        """list_flavors should return quota=None when flavor_quotas is empty."""
        resp = list_flavors(db_session)
        for f in resp.flavors:
            assert f.quota is None

    def test_returns_nodes_and_quota(self, db_session):
        """list_flavors should include both nodes and quota."""
        from sqlalchemy import text as sql_text
        db_session.execute(
            sql_text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('worker01', 128000, 515481, 0, 'cpu')"
            )
        )
        db_session.execute(
            sql_text(
                "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu) "
                "VALUES ('cpu', '256', '1000Gi', '0')"
            )
        )
        db_session.commit()

        resp = list_flavors(db_session)
        cpu_flavor = next(f for f in resp.flavors if f.name == "cpu")
        assert len(cpu_flavor.nodes) == 1
        assert cpu_flavor.nodes[0].node_name == "worker01"
        assert cpu_flavor.quota is not None
        assert cpu_flavor.quota.cpu == "256"


# ── Prometheus metrics ──


class TestMetrics:
    def test_submit_job_increments_submitted_counter(self, db_session):
        before = JOBS_SUBMITTED_TOTAL._value.get()
        req = _make_request()
        submit_job(db_session, NS, "alice", req)
        assert JOBS_SUBMITTED_TOTAL._value.get() - before == 1

    def test_submit_sweep_increments_submitted_counter(self, db_session):
        before = JOBS_SUBMITTED_TOTAL._value.get()
        req = _make_sweep_request()
        submit_sweep(db_session, NS, "alice", req)
        assert JOBS_SUBMITTED_TOTAL._value.get() - before == 1

    def test_cancel_single_increments_completed_counter(self, db_session):
        _insert_job(db_session, 100, status="QUEUED")
        before = JOBS_COMPLETED_TOTAL.labels(status="cancelled")._value.get()
        cancel_single(db_session, NS, 100)
        assert JOBS_COMPLETED_TOTAL.labels(status="cancelled")._value.get() - before == 1

    def test_cancel_skipped_does_not_increment_counter(self, db_session):
        _insert_job(db_session, 101, status="SUCCEEDED")
        before = JOBS_COMPLETED_TOTAL.labels(status="cancelled")._value.get()
        cancel_single(db_session, NS, 101)
        assert JOBS_COMPLETED_TOTAL.labels(status="cancelled")._value.get() - before == 0
