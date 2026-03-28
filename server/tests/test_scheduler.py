from cjob.dispatcher.scheduler import (
    cas_update_to_dispatching,
    mark_dispatched,
    mark_failed,
    reset_stale_dispatching,
)
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
