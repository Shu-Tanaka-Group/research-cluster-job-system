from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import (
    V1Job,
    V1JobCondition,
    V1JobStatus,
    V1ListMeta,
    V1ObjectMeta,
)
from kubernetes.client.rest import ApiException

from cjob.metrics import JOBS_COMPLETED_TOTAL
from cjob.models import Job, NamespaceDailyUsage, UserJobCounter
from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib
from cjob.watcher.reconciler import (
    LightJobCondition,
    LightK8sJob,
    NamespacePodNodeResolver,
    _merge_node_names,
    list_cjob_k8s_jobs,
    reconcile_cycle,
)


NS = "alice"


def _light_condition(cond: V1JobCondition) -> LightJobCondition:
    return LightJobCondition(
        type=cond.type or "",
        status=cond.status or "",
        reason=cond.reason,
    )


def _make_k8s_job(namespace, job_id, name, conditions=None, active=None):
    """Build a LightK8sJob with the fields reconcile_cycle uses."""
    return LightK8sJob(
        namespace=namespace,
        job_id=job_id,
        name=name,
        conditions=tuple(_light_condition(c) for c in (conditions or [])),
        active=active,
        succeeded=None,
        failed=None,
        completed_indexes=None,
        failed_indexes=None,
    )


def _insert_job(session, job_id, status="DISPATCHED", namespace=NS, user="alice", **kwargs):
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
        cpu_millicores=1000,
        memory_mib=1024,
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


@patch.object(NamespacePodNodeResolver, "resolve", return_value=[])
@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestReconcileStatusSync:
    """Test normal status synchronization from K8s to DB."""

    def test_dispatched_to_running(self, mock_delete, mock_fetch_nodes, db_session):
        _insert_job(db_session, 1, status="DISPATCHED")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "RUNNING"
        assert job.started_at is not None

    def test_dispatched_to_running_records_node_name(self, mock_delete, mock_fetch_nodes, db_session):
        mock_fetch_nodes.return_value = ["node-compute-01"]
        _insert_job(db_session, 1, status="DISPATCHED")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.node_name == "node-compute-01"
        mock_fetch_nodes.assert_called_once_with(NS, "cjob-alice-1")

    def test_dispatched_to_running_node_name_none_when_unavailable(self, mock_delete, mock_fetch_nodes, db_session):
        _insert_job(db_session, 1, status="DISPATCHED")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.node_name is None

    def test_node_name_recorded_on_succeeded_if_missed_running(self, mock_delete, mock_fetch_nodes, db_session):
        """If RUNNING was skipped, node_name should be recorded on SUCCEEDED."""
        mock_fetch_nodes.return_value = ["node-compute-02"]
        _insert_job(db_session, 1, status="DISPATCHED")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1",
                                   conditions=[V1JobCondition(type="Complete", status="True")])]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "SUCCEEDED"
        assert job.node_name == "node-compute-02"

    def test_node_name_recorded_on_failed_if_missed_running(self, mock_delete, mock_fetch_nodes, db_session):
        """If RUNNING was skipped, node_name should be recorded on FAILED."""
        mock_fetch_nodes.return_value = ["node-gpu-01"]
        _insert_job(db_session, 1, status="DISPATCHED")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1",
                                   conditions=[V1JobCondition(type="Failed", status="True")])]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.node_name == "node-gpu-01"

    def test_node_name_not_overwritten_on_succeeded(self, mock_delete, mock_fetch_nodes, db_session):
        """If node_name is already set, it should not be overwritten on completion."""
        _insert_job(db_session, 1, status="RUNNING", node_name="node-compute-01")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1",
                                   conditions=[V1JobCondition(type="Complete", status="True")])]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.node_name == "node-compute-01"
        mock_fetch_nodes.assert_not_called()

    def test_running_to_succeeded(self, mock_delete, mock_fetch_nodes, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        k8s_jobs = [
            _make_k8s_job(NS, 1, "cjob-alice-1",
                          conditions=[V1JobCondition(type="Complete", status="True")])
        ]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "SUCCEEDED"
        assert job.finished_at is not None

    def test_running_to_failed(self, mock_delete, mock_fetch_nodes, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        k8s_jobs = [
            _make_k8s_job(NS, 1, "cjob-alice-1",
                          conditions=[V1JobCondition(type="Failed", status="True")])
        ]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.finished_at is not None

    def test_deadline_exceeded_sets_last_error(self, mock_delete, mock_fetch_nodes, db_session):
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

    def test_failed_other_reason_no_last_error(self, mock_delete, mock_fetch_nodes, db_session):
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

    def test_started_at_not_overwritten_on_second_running(self, mock_delete, mock_fetch_nodes, db_session):
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

    def test_no_status_change_when_same(self, mock_delete, mock_fetch_nodes, db_session):
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


@patch.object(NamespacePodNodeResolver, "resolve", return_value=[])
@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestReconcileResourceUsage:
    """Test that RUNNING transition records resource usage."""

    def _get_usage(self, db_session, namespace=NS):
        """Get the total daily usage across all flavors for today."""
        rows = (
            db_session.query(NamespaceDailyUsage)
            .filter(NamespaceDailyUsage.namespace == namespace)
            .all()
        )
        if not rows:
            return None
        # Sum all rows across flavors
        total = NamespaceDailyUsage(
            namespace=namespace,
            usage_date=rows[0].usage_date,
            flavor=rows[0].flavor,
            cpu_millicores_seconds=sum(r.cpu_millicores_seconds for r in rows),
            memory_mib_seconds=sum(r.memory_mib_seconds for r in rows),
            gpu_seconds=sum(r.gpu_seconds for r in rows),
        )
        return total

    def _get_usage_by_flavor(self, db_session, namespace=NS):
        """Get daily usage rows keyed by flavor."""
        rows = (
            db_session.query(NamespaceDailyUsage)
            .filter(NamespaceDailyUsage.namespace == namespace)
            .all()
        )
        return {r.flavor: r for r in rows}

    def test_running_transition_records_usage(self, mock_delete, mock_fetch_nodes, db_session):
        _insert_job(db_session, 1, status="DISPATCHED", cpu="2", memory="4Gi",
                     gpu=0, time_limit_seconds=3600,
                     cpu_millicores=2000, memory_mib=4096)
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        usage = self._get_usage(db_session)
        assert usage is not None
        assert usage.cpu_millicores_seconds == 3600 * 2000  # 7_200_000
        assert usage.memory_mib_seconds == 3600 * 4096      # 14_745_600
        assert usage.gpu_seconds == 0

    def test_second_running_accumulates_usage(self, mock_delete, mock_fetch_nodes, db_session):
        _insert_job(db_session, 1, status="DISPATCHED", cpu="1", memory="1Gi",
                     time_limit_seconds=100,
                     cpu_millicores=1000, memory_mib=1024)
        _insert_job(db_session, 2, status="DISPATCHED", cpu="2", memory="2Gi",
                     time_limit_seconds=200,
                     cpu_millicores=2000, memory_mib=2048)
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

    def test_already_running_no_duplicate_usage(self, mock_delete, mock_fetch_nodes, db_session):
        """No usage recorded when job is already RUNNING (started_at set)."""
        from datetime import datetime, timezone
        _insert_job(db_session, 1, status="RUNNING", cpu="2", memory="4Gi",
                     time_limit_seconds=3600,
                     started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        usage = self._get_usage(db_session)
        assert usage is None  # No usage recorded

    def test_usage_recorded_with_flavor(self, mock_delete, mock_fetch_nodes, db_session):
        """Usage should be recorded per flavor."""
        _insert_job(db_session, 1, status="DISPATCHED", cpu="2", memory="4Gi",
                     gpu=0, time_limit_seconds=3600, flavor="gpu-a100",
                     cpu_millicores=2000, memory_mib=4096)
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        by_flavor = self._get_usage_by_flavor(db_session)
        assert "gpu-a100" in by_flavor
        assert by_flavor["gpu-a100"].cpu_millicores_seconds == 3600 * 2000

    def test_different_flavors_separate_rows(self, mock_delete, mock_fetch_nodes, db_session):
        """Jobs with different flavors should create separate usage rows."""
        _insert_job(db_session, 1, status="DISPATCHED", cpu="1", memory="1Gi",
                     time_limit_seconds=100, flavor="cpu",
                     cpu_millicores=1000, memory_mib=1024)
        _insert_job(db_session, 2, status="DISPATCHED", cpu="2", memory="2Gi",
                     time_limit_seconds=200, flavor="gpu-a100",
                     cpu_millicores=2000, memory_mib=2048)
        k8s_jobs = [
            _make_k8s_job(NS, 1, "cjob-alice-1", active=1),
            _make_k8s_job(NS, 2, "cjob-alice-2", active=1),
        ]

        reconcile_cycle(db_session, k8s_jobs)

        by_flavor = self._get_usage_by_flavor(db_session)
        assert len(by_flavor) == 2
        assert by_flavor["cpu"].cpu_millicores_seconds == 100 * 1000
        assert by_flavor["gpu-a100"].cpu_millicores_seconds == 200 * 2000

    def test_sweep_resource_usage_multiplied_by_parallelism(self, mock_delete, mock_fetch_nodes, db_session):
        """Sweep resource usage should be multiplied by parallelism."""
        _insert_job(db_session, 1, status="DISPATCHED", cpu="2", memory="4Gi",
                     gpu=0, time_limit_seconds=3600,
                     completions=100, parallelism=10,
                     succeeded_count=0, failed_count=0,
                     cpu_millicores=2000, memory_mib=4096)
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=10)]

        reconcile_cycle(db_session, k8s_jobs)

        usage = self._get_usage(db_session)
        assert usage is not None
        # 3600 * 2000m * 10 = 72_000_000
        assert usage.cpu_millicores_seconds == 3600 * 2000 * 10
        # 3600 * 4096Mi * 10 = 147_456_000
        assert usage.memory_mib_seconds == 3600 * 4096 * 10

    def test_usage_fallback_on_direct_succeeded(self, mock_delete, mock_fetch_nodes, db_session):
        """Jobs that complete within 1 scan cycle (never observed RUNNING)
        should still record usage on SUCCEEDED transition."""
        _insert_job(db_session, 1, status="DISPATCHED", cpu="2", memory="4Gi",
                     gpu=0, time_limit_seconds=3600,
                     cpu_millicores=2000, memory_mib=4096)
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1",
                                   conditions=[V1JobCondition(type="Complete", status="True")])]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "SUCCEEDED"
        assert job.started_at is None

        usage = self._get_usage(db_session)
        assert usage is not None
        assert usage.cpu_millicores_seconds == 3600 * 2000
        assert usage.memory_mib_seconds == 3600 * 4096

    def test_usage_fallback_on_direct_failed(self, mock_delete, mock_fetch_nodes, db_session):
        """Jobs that fail within 1 scan cycle (never observed RUNNING)
        should still record usage on FAILED transition."""
        _insert_job(db_session, 1, status="DISPATCHED", cpu="1", memory="1Gi",
                     gpu=0, time_limit_seconds=100,
                     cpu_millicores=1000, memory_mib=1024)
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1",
                                   conditions=[V1JobCondition(type="Failed", status="True")])]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.started_at is None

        usage = self._get_usage(db_session)
        assert usage is not None
        assert usage.cpu_millicores_seconds == 100 * 1000
        assert usage.memory_mib_seconds == 100 * 1024

    def test_usage_not_duplicated_when_running_observed(self, mock_delete, mock_fetch_nodes, db_session):
        """When RUNNING was already observed, completion should not record usage again."""
        from datetime import datetime, timezone
        _insert_job(db_session, 1, status="RUNNING", cpu="2", memory="4Gi",
                     time_limit_seconds=3600,
                     cpu_millicores=2000, memory_mib=4096,
                     started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1",
                                   conditions=[V1JobCondition(type="Complete", status="True")])]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "SUCCEEDED"

        usage = self._get_usage(db_session)
        assert usage is None

    def test_usage_fallback_on_sweep_direct_failed(self, mock_delete, mock_fetch_nodes, db_session):
        """Sweep jobs that complete within 1 scan cycle should use fallback with parallelism."""
        _insert_job(db_session, 1, status="DISPATCHED", cpu="2", memory="4Gi",
                     gpu=0, time_limit_seconds=3600,
                     completions=100, parallelism=10,
                     succeeded_count=0, failed_count=0,
                     cpu_millicores=2000, memory_mib=4096)
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1",
                                   conditions=[V1JobCondition(type="Failed", status="True")])]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.started_at is None

        usage = self._get_usage(db_session)
        assert usage is not None
        assert usage.cpu_millicores_seconds == 3600 * 2000 * 10
        assert usage.memory_mib_seconds == 3600 * 4096 * 10


# ── Sweep status tracking ──


def _make_sweep_k8s_job(namespace, job_id, name, conditions=None, active=None,
                         succeeded=None, failed=None,
                         completed_indexes=None, failed_indexes=None):
    """Build a LightK8sJob with sweep-specific status fields."""
    return LightK8sJob(
        namespace=namespace,
        job_id=job_id,
        name=name,
        conditions=tuple(_light_condition(c) for c in (conditions or [])),
        active=active,
        succeeded=succeeded,
        failed=failed,
        completed_indexes=completed_indexes,
        failed_indexes=failed_indexes,
    )


@patch.object(NamespacePodNodeResolver, "resolve", return_value=[])
@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestReconcileSweep:
    """Test sweep-specific reconciliation behavior."""

    def test_sweep_complete_all_succeeded(self, mock_delete, mock_fetch_nodes, db_session):
        """Sweep with all tasks succeeded → SUCCEEDED."""
        _insert_job(db_session, 1, status="RUNNING",
                     completions=10, parallelism=5,
                     succeeded_count=0, failed_count=0)
        k8s_jobs = [_make_sweep_k8s_job(
            NS, 1, "cjob-alice-1",
            conditions=[V1JobCondition(type="Complete", status="True")],
            succeeded=10, failed=0,
            completed_indexes="0-9",
        )]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "SUCCEEDED"
        assert job.succeeded_count == 10
        assert job.failed_count == 0
        assert job.completed_indexes == "0-9"

    def test_sweep_complete_with_failures(self, mock_delete, mock_fetch_nodes, db_session):
        """Sweep with K8s Complete but failed_count > 0 → FAILED."""
        _insert_job(db_session, 1, status="RUNNING",
                     completions=10, parallelism=5,
                     succeeded_count=0, failed_count=0)
        k8s_jobs = [_make_sweep_k8s_job(
            NS, 1, "cjob-alice-1",
            conditions=[V1JobCondition(type="Complete", status="True")],
            succeeded=8, failed=2,
            completed_indexes="0-7",
            failed_indexes="8,9",
        )]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.succeeded_count == 8
        assert job.failed_count == 2
        assert job.completed_indexes == "0-7"
        assert job.failed_indexes == "8,9"

    def test_sweep_failed_condition(self, mock_delete, mock_fetch_nodes, db_session):
        """Sweep with K8s Failed condition (e.g. DeadlineExceeded) → FAILED."""
        _insert_job(db_session, 1, status="RUNNING",
                     completions=100, parallelism=10,
                     succeeded_count=0, failed_count=0)
        k8s_jobs = [_make_sweep_k8s_job(
            NS, 1, "cjob-alice-1",
            conditions=[V1JobCondition(
                type="Failed", status="True", reason="DeadlineExceeded"
            )],
            succeeded=50, failed=5,
            completed_indexes="0-49",
            failed_indexes="50-54",
        )]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.last_error == "time limit exceeded"

    def test_sweep_index_tracking_updates(self, mock_delete, mock_fetch_nodes, db_session):
        """Index tracking should update on each reconcile cycle."""
        _insert_job(db_session, 1, status="RUNNING",
                     completions=100, parallelism=10,
                     succeeded_count=0, failed_count=0)
        k8s_jobs = [_make_sweep_k8s_job(
            NS, 1, "cjob-alice-1",
            active=10, succeeded=40, failed=1,
            completed_indexes="0-39",
            failed_indexes="40",
        )]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "RUNNING"
        assert job.succeeded_count == 40
        assert job.failed_count == 1
        assert job.completed_indexes == "0-39"
        assert job.failed_indexes == "40"


# ── _merge_node_names ──


class TestMergeNodeNames:
    def test_none_with_empty_list(self):
        assert _merge_node_names(None, []) is None

    def test_none_with_single_name(self):
        assert _merge_node_names(None, ["node-1"]) == "node-1"

    def test_none_with_multiple_names(self):
        assert _merge_node_names(None, ["node-2", "node-1"]) == "node-1,node-2"

    def test_existing_with_new_name(self):
        assert _merge_node_names("node-1", ["node-2"]) == "node-1,node-2"

    def test_existing_with_duplicate(self):
        assert _merge_node_names("node-1", ["node-1"]) == "node-1"

    def test_existing_list_with_partial_overlap(self):
        assert _merge_node_names("node-1,node-2", ["node-2", "node-3"]) == "node-1,node-2,node-3"

    def test_existing_with_empty_list(self):
        assert _merge_node_names("node-1", []) == "node-1"


# ── Sweep node_name accumulation ──


@patch.object(NamespacePodNodeResolver, "resolve")
@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestSweepNodeNameAccumulation:
    """Test node_name accumulation for sweep jobs."""

    def test_running_transition_records_multiple_nodes(self, mock_delete, mock_fetch_nodes, db_session):
        """RUNNING transition should record all Pod node names."""
        mock_fetch_nodes.return_value = ["node-2", "node-1"]
        _insert_job(db_session, 1, status="DISPATCHED",
                     completions=10, parallelism=5,
                     succeeded_count=0, failed_count=0)
        k8s_jobs = [_make_sweep_k8s_job(NS, 1, "cjob-alice-1", active=5)]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.node_name == "node-1,node-2"

    def test_count_change_adds_new_nodes(self, mock_delete, mock_fetch_nodes, db_session):
        """When succeeded_count changes, new node names should be merged."""
        mock_fetch_nodes.return_value = ["node-3"]
        _insert_job(db_session, 1, status="RUNNING", node_name="node-1,node-2",
                     completions=100, parallelism=10,
                     succeeded_count=40, failed_count=0)
        k8s_jobs = [_make_sweep_k8s_job(
            NS, 1, "cjob-alice-1",
            active=10, succeeded=50, failed=0,
            completed_indexes="0-49",
        )]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.node_name == "node-1,node-2,node-3"

    def test_count_change_deduplicates_nodes(self, mock_delete, mock_fetch_nodes, db_session):
        """Duplicate node names should not be added."""
        mock_fetch_nodes.return_value = ["node-1", "node-2"]
        _insert_job(db_session, 1, status="RUNNING", node_name="node-1,node-2",
                     completions=100, parallelism=10,
                     succeeded_count=40, failed_count=0)
        k8s_jobs = [_make_sweep_k8s_job(
            NS, 1, "cjob-alice-1",
            active=10, succeeded=50, failed=0,
            completed_indexes="0-49",
        )]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.node_name == "node-1,node-2"

    def test_no_fetch_when_counts_unchanged(self, mock_delete, mock_fetch_nodes, db_session):
        """No Pod fetch when succeeded/failed counts are unchanged."""
        _insert_job(db_session, 1, status="RUNNING", node_name="node-1",
                     completions=100, parallelism=10,
                     succeeded_count=40, failed_count=0,
                     completed_indexes="0-39")
        k8s_jobs = [_make_sweep_k8s_job(
            NS, 1, "cjob-alice-1",
            active=10, succeeded=40, failed=0,
            completed_indexes="0-39",
        )]

        reconcile_cycle(db_session, k8s_jobs)

        mock_fetch_nodes.assert_not_called()

    def test_failed_count_change_triggers_node_fetch(self, mock_delete, mock_fetch_nodes, db_session):
        """When failed_count changes, node names should be updated."""
        mock_fetch_nodes.return_value = ["node-1", "node-3"]
        _insert_job(db_session, 1, status="RUNNING", node_name="node-1,node-2",
                     completions=100, parallelism=10,
                     succeeded_count=40, failed_count=0)
        k8s_jobs = [_make_sweep_k8s_job(
            NS, 1, "cjob-alice-1",
            active=9, succeeded=40, failed=1,
            completed_indexes="0-39",
            failed_indexes="40",
        )]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.node_name == "node-1,node-2,node-3"


# ── Disappeared K8s Jobs (Step 8) ──


@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestReconcileDisappearedJobs:
    """Test Step 8: DISPATCHED/RUNNING jobs with no K8s Job are marked FAILED."""

    def test_dispatched_job_without_k8s_job_marked_failed(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="DISPATCHED")

        reconcile_cycle(db_session, [])

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"

    def test_running_job_without_k8s_job_marked_failed(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="RUNNING")

        reconcile_cycle(db_session, [])

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"

    def test_disappeared_job_sets_last_error(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="DISPATCHED")

        reconcile_cycle(db_session, [])

        job = db_session.get(Job, (NS, 1))
        assert job.last_error == "K8s Job not found (TTL expired or manually deleted)"

    def test_disappeared_job_sets_finished_at(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="RUNNING")

        reconcile_cycle(db_session, [])

        job = db_session.get(Job, (NS, 1))
        assert job.finished_at is not None

    def test_succeeded_job_not_affected(self, mock_delete, db_session):
        _insert_job(db_session, 1, status="SUCCEEDED")

        reconcile_cycle(db_session, [])

        job = db_session.get(Job, (NS, 1))
        assert job.status == "SUCCEEDED"
        assert job.last_error is None

    def test_job_with_k8s_job_not_affected(self, mock_delete, db_session):
        """DISPATCHED job with a corresponding K8s Job should not be marked FAILED."""
        _insert_job(db_session, 1, status="DISPATCHED")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1")]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "DISPATCHED"

    def test_dispatched_within_grace_period_spared(self, mock_delete, db_session):
        """A DISPATCHED job whose dispatched_at is still within the grace
        period must NOT be marked FAILED even when its K8s Job is absent.
        This is the race-window protection against Dispatcher/Watcher
        ordering (watcher.md §3 Step 8 dispatcher grace period)."""
        recent = datetime.now(timezone.utc) - timedelta(seconds=5)
        _insert_job(db_session, 1, status="DISPATCHED", dispatched_at=recent)

        reconcile_cycle(db_session, [], dispatch_grace_sec=30)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "DISPATCHED"
        assert job.finished_at is None
        assert job.last_error is None

    def test_dispatched_past_grace_period_marked_failed(self, mock_delete, db_session):
        """A DISPATCHED job that has been dispatched longer ago than the
        grace period is still subject to the disappearance check."""
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        _insert_job(db_session, 1, status="DISPATCHED", dispatched_at=old)

        reconcile_cycle(db_session, [], dispatch_grace_sec=30)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.finished_at is not None

    def test_dispatched_null_dispatched_at_still_marked_failed(
        self, mock_delete, db_session
    ):
        """Legacy/fixture rows with NULL dispatched_at retain the original
        Step 8 behaviour (no grace period protection)."""
        _insert_job(db_session, 1, status="DISPATCHED", dispatched_at=None)

        reconcile_cycle(db_session, [], dispatch_grace_sec=30)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"

    def test_running_job_not_subject_to_grace_period(self, mock_delete, db_session):
        """Grace period applies only to DISPATCHED. A RUNNING job whose
        K8s Job is missing must be marked FAILED regardless of timestamps."""
        recent = datetime.now(timezone.utc) - timedelta(seconds=5)
        _insert_job(
            db_session,
            1,
            status="RUNNING",
            dispatched_at=recent,
            started_at=recent,
        )

        reconcile_cycle(db_session, [], dispatch_grace_sec=30)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"


# ── Terminal state regression guard ──


@patch.object(NamespacePodNodeResolver, "resolve", return_value=[])
@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestTerminalStateRegressionGuard:
    """Defense-in-depth: SUCCEEDED/FAILED must never roll back to RUNNING
    via the normal status sync, even if the K8s Job temporarily looks
    active again (watcher.md §3 Terminal 状態復帰ガード)."""

    def test_failed_job_does_not_regress_to_running(
        self, mock_delete, mock_fetch_nodes, db_session
    ):
        finished = datetime.now(timezone.utc) - timedelta(seconds=60)
        _insert_job(
            db_session,
            1,
            status="FAILED",
            finished_at=finished,
            last_error="K8s Job not found (TTL expired or manually deleted)",
        )
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "FAILED"
        assert job.finished_at is not None
        # last_error preserved
        assert job.last_error is not None

    def test_succeeded_job_does_not_regress_to_running(
        self, mock_delete, mock_fetch_nodes, db_session
    ):
        finished = datetime.now(timezone.utc) - timedelta(seconds=60)
        _insert_job(
            db_session,
            1,
            status="SUCCEEDED",
            finished_at=finished,
        )
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "SUCCEEDED"

    def test_dispatched_to_running_still_allowed(
        self, mock_delete, mock_fetch_nodes, db_session
    ):
        """The regression guard must not block the normal forward path."""
        _insert_job(db_session, 1, status="DISPATCHED")
        k8s_jobs = [_make_k8s_job(NS, 1, "cjob-alice-1", active=1)]

        reconcile_cycle(db_session, k8s_jobs)

        job = db_session.get(Job, (NS, 1))
        assert job.status == "RUNNING"


# ── K8s API failure propagation ──


def _raw_v1job(namespace, job_id, name, conditions=None, active=None,
               succeeded=None, failed=None,
               completed_indexes=None, failed_indexes=None):
    """Build a raw V1Job (used to drive list_cjob_k8s_jobs via mocks)."""
    return V1Job(
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={
                "cjob.io/namespace": namespace,
                "cjob.io/job-id": str(job_id),
            },
        ),
        status=V1JobStatus(
            conditions=conditions,
            active=active,
            succeeded=succeeded,
            failed=failed,
            completed_indexes=completed_indexes,
            failed_indexes=failed_indexes,
        ),
    )


class _FakeJobList:
    def __init__(self, items, continue_token=None):
        self.items = items
        self.metadata = V1ListMeta(_continue=continue_token)


class TestListCjobK8sJobs:
    """Test that list_cjob_k8s_jobs pages results and propagates API errors."""

    @patch("cjob.watcher.reconciler.k8s_client.BatchV1Api")
    def test_api_failure_propagates(self, mock_batch_cls):
        mock_api = MagicMock()
        mock_api.list_job_for_all_namespaces.side_effect = ApiException(
            status=503, reason="Service Unavailable"
        )
        mock_batch_cls.return_value = mock_api

        with pytest.raises(ApiException):
            list_cjob_k8s_jobs()

    @patch("cjob.watcher.reconciler.k8s_client.BatchV1Api")
    def test_success_returns_light_k8s_jobs(self, mock_batch_cls):
        mock_api = MagicMock()
        mock_api.list_job_for_all_namespaces.return_value = _FakeJobList(
            items=[_raw_v1job("alice", 7, "cjob-alice-7", active=1)],
        )
        mock_batch_cls.return_value = mock_api

        result = list_cjob_k8s_jobs()

        assert len(result) == 1
        light = result[0]
        assert isinstance(light, LightK8sJob)
        assert light.namespace == "alice"
        assert light.job_id == 7
        assert light.name == "cjob-alice-7"
        assert light.active == 1

    @patch("cjob.watcher.reconciler.k8s_client.BatchV1Api")
    def test_paginates_until_continue_is_empty(self, mock_batch_cls):
        mock_api = MagicMock()
        mock_api.list_job_for_all_namespaces.side_effect = [
            _FakeJobList(
                items=[_raw_v1job("alice", 1, "cjob-alice-1", active=1)],
                continue_token="token-1",
            ),
            _FakeJobList(
                items=[_raw_v1job("bob", 1, "cjob-bob-1", active=1)],
                continue_token="token-2",
            ),
            _FakeJobList(
                items=[_raw_v1job("carol", 1, "cjob-carol-1", active=1)],
                continue_token=None,
            ),
        ]
        mock_batch_cls.return_value = mock_api

        result = list_cjob_k8s_jobs(page_size=1)

        assert [(j.namespace, j.job_id) for j in result] == [
            ("alice", 1),
            ("bob", 1),
            ("carol", 1),
        ]
        # Page 1: no continue token; pages 2 and 3: continue token passed through
        calls = mock_api.list_job_for_all_namespaces.call_args_list
        assert len(calls) == 3
        assert "_continue" not in calls[0].kwargs
        assert calls[1].kwargs["_continue"] == "token-1"
        assert calls[2].kwargs["_continue"] == "token-2"
        for call in calls:
            assert call.kwargs["limit"] == 1
            assert call.kwargs["label_selector"] == "cjob.io/job-id"

    @patch("cjob.watcher.reconciler.k8s_client.BatchV1Api")
    def test_skips_jobs_with_invalid_labels(self, mock_batch_cls):
        no_labels = V1Job(metadata=V1ObjectMeta(name="cjob-unknown"), status=V1JobStatus())
        bad_job_id = V1Job(
            metadata=V1ObjectMeta(
                name="cjob-alice-x",
                labels={
                    "cjob.io/namespace": "alice",
                    "cjob.io/job-id": "not-a-number",
                },
            ),
            status=V1JobStatus(),
        )
        ok = _raw_v1job("alice", 1, "cjob-alice-1", active=1)

        mock_api = MagicMock()
        mock_api.list_job_for_all_namespaces.return_value = _FakeJobList(
            items=[no_labels, bad_job_id, ok],
        )
        mock_batch_cls.return_value = mock_api

        result = list_cjob_k8s_jobs()

        assert len(result) == 1
        assert result[0].namespace == "alice"
        assert result[0].job_id == 1


class TestLightK8sJobFromV1Job:
    """Test the V1Job → LightK8sJob extraction."""

    def test_extracts_basic_fields(self):
        v1 = _raw_v1job(
            "alice", 5, "cjob-alice-5",
            conditions=[V1JobCondition(type="Complete", status="True")],
            active=0,
            succeeded=1,
            failed=0,
            completed_indexes="0",
            failed_indexes="",
        )

        light = LightK8sJob.from_v1job(v1)

        assert light is not None
        assert light.namespace == "alice"
        assert light.job_id == 5
        assert light.name == "cjob-alice-5"
        assert light.conditions == (
            LightJobCondition(type="Complete", status="True", reason=None),
        )
        assert light.active == 0
        assert light.succeeded == 1
        assert light.completed_indexes == "0"

    def test_returns_none_on_missing_labels(self):
        v1 = V1Job(metadata=V1ObjectMeta(name="x"), status=V1JobStatus())
        assert LightK8sJob.from_v1job(v1) is None

    def test_returns_none_on_invalid_job_id(self):
        v1 = V1Job(
            metadata=V1ObjectMeta(
                name="x",
                labels={
                    "cjob.io/namespace": "alice",
                    "cjob.io/job-id": "abc",
                },
            ),
            status=V1JobStatus(),
        )
        assert LightK8sJob.from_v1job(v1) is None


class TestNamespacePodNodeResolver:
    """Test that the resolver caches Pods per namespace."""

    @patch("cjob.watcher.reconciler.k8s_client.CoreV1Api")
    def test_single_fetch_per_namespace(self, mock_core_cls):
        pod_a = MagicMock()
        pod_a.metadata = V1ObjectMeta(labels={"job-name": "cjob-alice-1"})
        pod_a.spec = MagicMock(node_name="node-1")
        pod_b = MagicMock()
        pod_b.metadata = V1ObjectMeta(labels={"job-name": "cjob-alice-2"})
        pod_b.spec = MagicMock(node_name="node-2")

        mock_api = MagicMock()
        mock_api.list_namespaced_pod.return_value = MagicMock(items=[pod_a, pod_b])
        mock_core_cls.return_value = mock_api

        resolver = NamespacePodNodeResolver()

        assert resolver.resolve("alice", "cjob-alice-1") == ["node-1"]
        assert resolver.resolve("alice", "cjob-alice-2") == ["node-2"]
        # A second resolve for an already-cached namespace should not re-fetch
        assert resolver.resolve("alice", "cjob-alice-1") == ["node-1"]

        assert mock_api.list_namespaced_pod.call_count == 1
        call = mock_api.list_namespaced_pod.call_args
        assert call.kwargs == {
            "namespace": "alice",
            "label_selector": "job-name",
        }

    @patch("cjob.watcher.reconciler.k8s_client.CoreV1Api")
    def test_api_failure_returns_empty(self, mock_core_cls):
        mock_api = MagicMock()
        mock_api.list_namespaced_pod.side_effect = ApiException(
            status=500, reason="boom"
        )
        mock_core_cls.return_value = mock_api

        resolver = NamespacePodNodeResolver()
        assert resolver.resolve("alice", "cjob-alice-1") == []
        # Cached negative result: a second lookup should not retry
        assert resolver.resolve("alice", "cjob-alice-2") == []
        assert mock_api.list_namespaced_pod.call_count == 1

    @patch("cjob.watcher.reconciler.k8s_client.CoreV1Api")
    def test_unknown_job_name_returns_empty(self, mock_core_cls):
        pod = MagicMock()
        pod.metadata = V1ObjectMeta(labels={"job-name": "cjob-alice-1"})
        pod.spec = MagicMock(node_name="node-1")

        mock_api = MagicMock()
        mock_api.list_namespaced_pod.return_value = MagicMock(items=[pod])
        mock_core_cls.return_value = mock_api

        resolver = NamespacePodNodeResolver()
        assert resolver.resolve("alice", "cjob-alice-999") == []


# ── Prometheus metrics ──


@patch.object(NamespacePodNodeResolver, "resolve", return_value=[])
@patch("cjob.watcher.reconciler._delete_k8s_job")
class TestMetrics:
    def test_succeeded_increments_completed_counter(self, mock_delete, mock_fetch_nodes, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        k8s_jobs = [
            _make_k8s_job(NS, 1, "cjob-alice-1",
                          conditions=[V1JobCondition(type="Complete", status="True")])
        ]
        before = JOBS_COMPLETED_TOTAL.labels(status="succeeded")._value.get()
        reconcile_cycle(db_session, k8s_jobs)
        assert JOBS_COMPLETED_TOTAL.labels(status="succeeded")._value.get() - before == 1

    def test_failed_increments_completed_counter(self, mock_delete, mock_fetch_nodes, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        k8s_jobs = [
            _make_k8s_job(NS, 1, "cjob-alice-1",
                          conditions=[V1JobCondition(type="Failed", status="True")])
        ]
        before = JOBS_COMPLETED_TOTAL.labels(status="failed")._value.get()
        reconcile_cycle(db_session, k8s_jobs)
        assert JOBS_COMPLETED_TOTAL.labels(status="failed")._value.get() - before == 1

    def test_disappeared_job_increments_failed_counter(self, mock_delete, mock_fetch_nodes, db_session):
        _insert_job(db_session, 1, status="RUNNING")
        before = JOBS_COMPLETED_TOTAL.labels(status="failed")._value.get()
        reconcile_cycle(db_session, [])
        assert JOBS_COMPLETED_TOTAL.labels(status="failed")._value.get() - before == 1
