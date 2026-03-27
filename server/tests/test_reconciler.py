from unittest.mock import patch

from kubernetes.client import V1Job, V1JobCondition, V1JobStatus, V1ObjectMeta

from cjob.models import Job, NamespaceDailyUsage, UserJobCounter
from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib
from cjob.watcher.reconciler import reconcile_cycle


NS = "user-alice"


def _make_k8s_job(namespace, job_id, name, conditions=None, active=None):
    """Build a minimal K8s V1Job with cjob labels."""
    return V1Job(
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={
                "cjob.io/namespace": namespace,
                "cjob.io/job-id": str(job_id),
            },
        ),
        status=V1JobStatus(conditions=conditions, active=active),
    )


def _insert_job(session, job_id, status="DISPATCHED", namespace=NS, **kwargs):
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


def _insert_counter(session, namespace=NS, next_id=2):
    counter = UserJobCounter(namespace=namespace, next_id=next_id)
    session.add(counter)
    session.flush()


@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestReconcileStatusSync:
    """Test normal status synchronization from K8s to DB."""

    def test_dispatched_to_running(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="DISPATCHED")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "RUNNING"
        assert job.started_at is not None

    def test_running_to_succeeded(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        k8s_jobs = [
            _make_k8s_job(NS, 1, "cjob-alice-1",
                          conditions=[V1JobCondition(type="Complete", status="True")])
        ]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "SUCCEEDED"
        assert job.finished_at is not None

    def test_running_to_failed(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        k8s_jobs = [
            _make_k8s_job(NS, 1, "cjob-alice-1",
                          conditions=[V1JobCondition(type="Failed", status="True")])
        ]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.finished_at is not None

    def test_deadline_exceeded_sets_last_error(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        k8s_jobs = [
            _make_k8s_job(NS, 1, "cjob-alice-1",
                          conditions=[V1JobCondition(
                              type="Failed", status="True", reason="DeadlineExceeded"
                          )])
        ]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.last_error == "time limit exceeded"

    def test_failed_other_reason_no_last_error(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        k8s_jobs = [
            _make_k8s_job(NS, 1, "cjob-alice-1",
                          conditions=[V1JobCondition(
                              type="Failed", status="True", reason="BackoffLimitExceeded"
                          )])
        ]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.last_error is None

    def test_started_at_not_overwritten_on_second_running(self, mock_delete, db_session):
        """started_at should only be set on the first RUNNING transition."""
        from datetime import datetime, timezone
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _insert_job(db_session, 1, status="RUNNING", started_at=started)
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        # Status unchanged (RUNNING -> RUNNING), so started_at stays the same
        # SQLite drops timezone info, so compare naive datetimes
        assert job.started_at.replace(tzinfo=None) == started.replace(tzinfo=None)

    def test_no_status_change_when_same(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "RUNNING"
        # finished_at should still be None
        assert job.finished_at is None


@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestReconcileCancelled:
    """Test that CANCELLED jobs trigger K8s Job deletion without DB status change."""

    def test_cancelled_deletes_k8s_job(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="CANCELLED")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        mock_delete.assert_called_once_with(NS, "cjob-alice-1")
        job = db_session.get(Job, (NS, 1))
        assert job.status == "CANCELLED"


@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestReconcileOrphan:
    """Test orphan K8s Job detection and deletion."""

    def test_orphan_deleted(self, mock_delete, db_session):
        # K8s Job exists but no DB record
        k8s_jobs = [_make_k8s_job(NS, 999, "cjob-alice-999", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        mock_delete.assert_called_once_with(NS, "cjob-alice-999")


@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestReconcileDeleting:
    """Test DELETING phase 1 (K8s Job deletion) and phase 2 (DB cleanup)."""

    def test_deleting_phase1_deletes_k8s_job(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="DELETING")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1")]

        reconcile_cycle(db_session, k8s_jobs)

        mock_delete.assert_called_once_with(NS, "cjob-alice-1")

    def test_deleting_phase2_cleans_up_db(self, mock_delete, db_session):
        """When all K8s Jobs are gone, DB records should be deleted and counter reset."""
        _insert_job(db_session, 1, status="DELETING")
        _insert_job(db_session, 2, status="DELETING")
        _insert_counter(db_session, next_id=3)

        # No K8s Jobs → all gone
        reconcile_cycle(db_session, [])

        # DB records should be deleted
        jobs = db_session.query(Job).filter(Job.namespace == NS).all()
        assert len(jobs) == 0
        # Counter should be reset
        counter = db_session.get(UserJobCounter, NS)
        assert counter.next_id == 1

    def test_deleting_phase2_not_triggered_when_k8s_jobs_remain(self, mock_delete, db_session):
        """Phase 2 should not trigger if K8s Jobs still exist."""
        _insert_job(db_session, 1, status="DELETING")
        _insert_job(db_session, 2, status="DELETING")
        _insert_counter(db_session, next_id=3)

        # Job 1 still exists in K8s
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1")]

        reconcile_cycle(db_session, k8s_jobs)

        # DB records should still exist
        jobs = db_session.query(Job).filter(Job.namespace == NS).all()
        assert len(jobs) == 2

    def test_deleting_phase2_namespace_isolation(self, mock_delete, db_session):
        """Phase 2 cleanup for one namespace should not affect another."""
        _insert_job(db_session, 1, status="DELETING", namespace="user-alice")
        _insert_job(db_session, 1, status="DELETING", namespace="user-bob")
        _insert_counter(db_session, namespace="user-alice", next_id=2)
        _insert_counter(db_session, namespace="user-bob", next_id=2)

        # user-bob's K8s Job still exists
        k8s_jobs = [_make_k8s_job("user-bob", 1, "cjob-bob-1")]

        reconcile_cycle(db_session, k8s_jobs)

        # Alice's records should be cleaned up
        alice_jobs = db_session.query(Job).filter(Job.namespace == "user-alice").all()
        assert len(alice_jobs) == 0
        alice_counter = db_session.get(UserJobCounter, "user-alice")
        assert alice_counter.next_id == 1

        # Bob's records should remain
        bob_jobs = db_session.query(Job).filter(Job.namespace == "user-bob").all()
        assert len(bob_jobs) == 1


# ── parse_cpu_millicores / parse_memory_mib ──


class TestParseCpuMillicores:
    def test_integer_cores(self):
        assert parse_cpu_millicores("2") == 2000

    def test_fractional_cores(self):
        assert parse_cpu_millicores("0.5") == 500

    def test_millicores_suffix(self):
        assert parse_cpu_millicores("500m") == 500

    def test_one_core(self):
        assert parse_cpu_millicores("1") == 1000


class TestParseMemoryMib:
    def test_gi_suffix(self):
        assert parse_memory_mib("4Gi") == 4096

    def test_mi_suffix(self):
        assert parse_memory_mib("500Mi") == 500

    def test_ki_suffix(self):
        assert parse_memory_mib("1024Ki") == 1

    def test_plain_bytes(self):
        assert parse_memory_mib("1048576") == 1  # 1 MiB


# ── Resource usage recording ──


@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestReconcileResourceUsage:
    """Test that RUNNING transition records resource usage."""

    def _get_usage(self, db_session, namespace=NS):
        """Get the daily usage row for today."""
        rows = (
            db_session.query(NamespaceDailyUsage)
            .filter(NamespaceDailyUsage.namespace == namespace)
            .all()
        )
        if not rows:
            return None
        # Sum all rows (should be 1 for today in tests)
        total = NamespaceDailyUsage(
            namespace=namespace,
            usage_date=rows[0].usage_date,
            cpu_millicores_seconds=sum(r.cpu_millicores_seconds for r in rows),
            memory_mib_seconds=sum(r.memory_mib_seconds for r in rows),
            gpu_seconds=sum(r.gpu_seconds for r in rows),
        )
        return total

    def test_running_transition_records_usage(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="DISPATCHED", cpu="2", memory="4Gi",
                     gpu=0, time_limit_seconds=3600)
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        usage = self._get_usage(db_session)
        assert usage is not None
        assert usage.cpu_millicores_seconds == 3600 * 2000  # 7_200_000
        assert usage.memory_mib_seconds == 3600 * 4096      # 14_745_600
        assert usage.gpu_seconds == 0

    def test_second_running_accumulates_usage(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="DISPATCHED", cpu="1", memory="1Gi",
                     time_limit_seconds=100)
        _insert_job(db_session, 2, status="DISPATCHED", cpu="2", memory="2Gi",
                     time_limit_seconds=200)
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        # Second job transitions to RUNNING in next cycle
        db_session.get(Job, (NS, 2)).status = "DISPATCHED"
        db_session.flush()
        k8s_jobs2 = [
            _make_k8s_job(NS, 1, "cjob-alice-1", active=1),
            _make_k8s_job(NS, 2, "cjob-alice-2", active=1),
        ]

        reconcile_cycle(db_session, k8s_jobs2)

        usage = self._get_usage(db_session)
        assert usage.cpu_millicores_seconds == 100 * 1000 + 200 * 2000  # 500_000
        assert usage.memory_mib_seconds == 100 * 1024 + 200 * 2048     # 512_000

    def test_already_running_no_duplicate_usage(self, mock_delete, db_session):
        """No usage recorded when job is already RUNNING (started_at set)."""
        from datetime import datetime, timezone
        _insert_job(db_session, 1, status="RUNNING", cpu="2", memory="4Gi",
                     time_limit_seconds=3600,
                     started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        usage = self._get_usage(db_session)
        assert usage is None  # No usage recorded
