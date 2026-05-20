from kubernetes.client import V1JobCondition

from cjob.watcher.reconciler import (
    LightJobCondition,
    LightK8sJob,
    determine_status,
)


def _light_condition(cond: V1JobCondition) -> LightJobCondition:
    return LightJobCondition(
        type=cond.type or "",
        status=cond.status or "",
        reason=cond.reason,
    )


_UNSET = object()


def _make_k8s_job(conditions=None, active=None, ready=_UNSET):
    """Helper to build a minimal LightK8sJob for testing determine_status.

    When ``ready`` is not specified, it defaults to ``active`` so existing
    call sites that mean "Pod is up and Running" with ``active=1`` continue
    to work without rewriting every test (watcher.md §3 requires
    active>0 AND ready>0 for the RUNNING transition). Pass ``ready=None``
    explicitly to simulate a Job whose ready field is absent.
    """
    if ready is _UNSET:
        ready = active
    return LightK8sJob(
        namespace="user-test",
        job_id=1,
        name="test-job",
        conditions=tuple(_light_condition(c) for c in (conditions or [])),
        active=active,
        ready=ready,
        succeeded=None,
        failed=None,
        completed_indexes=None,
        failed_indexes=None,
    )


class TestDetermineStatus:
    def test_succeeded(self):
        job = _make_k8s_job(
            conditions=[V1JobCondition(type="Complete", status="True")]
        )
        assert determine_status(job) == ("SUCCEEDED", None)

    def test_failed(self):
        job = _make_k8s_job(
            conditions=[V1JobCondition(type="Failed", status="True")]
        )
        assert determine_status(job) == ("FAILED", None)

    def test_failed_with_reason(self):
        job = _make_k8s_job(
            conditions=[
                V1JobCondition(type="Failed", status="True", reason="BackoffLimitExceeded")
            ]
        )
        assert determine_status(job) == ("FAILED", "BackoffLimitExceeded")

    def test_failed_deadline_exceeded(self):
        job = _make_k8s_job(
            conditions=[
                V1JobCondition(type="Failed", status="True", reason="DeadlineExceeded")
            ]
        )
        status, reason = determine_status(job)
        assert status == "FAILED"
        assert reason == "DeadlineExceeded"

    def test_running(self):
        job = _make_k8s_job(active=1, ready=1)
        assert determine_status(job) == ("RUNNING", None)

    def test_pending_pod_not_running(self):
        """active=1, ready=0 (Pod Pending, e.g. node resource shortage) → no transition."""
        job = _make_k8s_job(active=1, ready=0)
        assert determine_status(job) == (None, None)

    def test_running_requires_ready_present(self):
        """active=1 but ready field missing (older K8s) → no transition."""
        job = _make_k8s_job(active=1, ready=None)
        assert determine_status(job) == (None, None)

    def test_sweep_partial_running(self):
        """Sweep: some Pods Pending, some Running (active=5, ready=2) → RUNNING."""
        job = _make_k8s_job(active=5, ready=2)
        assert determine_status(job) == ("RUNNING", None)

    def test_sweep_all_pending(self):
        """Sweep: all Pods Pending (active=5, ready=0) → no transition."""
        job = _make_k8s_job(active=5, ready=0)
        assert determine_status(job) == (None, None)

    def test_no_status(self):
        job = _make_k8s_job()
        assert determine_status(job) == (None, None)

    def test_condition_not_true(self):
        job = _make_k8s_job(
            conditions=[V1JobCondition(type="Complete", status="False")]
        )
        assert determine_status(job) == (None, None)

    def test_complete_takes_precedence_over_active(self):
        job = _make_k8s_job(
            conditions=[V1JobCondition(type="Complete", status="True")],
            active=1,
        )
        assert determine_status(job) == ("SUCCEEDED", None)

    def test_empty_status(self):
        """LightK8sJob with empty conditions and no active Pods maps to (None, None)."""
        job = LightK8sJob(
            namespace="user-test",
            job_id=1,
            name="test-job",
            conditions=(),
            active=None,
            ready=None,
            succeeded=None,
            failed=None,
            completed_indexes=None,
            failed_indexes=None,
        )
        assert determine_status(job) == (None, None)
