from kubernetes.client import V1Job, V1JobCondition, V1JobStatus, V1ObjectMeta

from cjob.watcher.reconciler import determine_status


def _make_k8s_job(conditions=None, active=None):
    """Helper to build a minimal V1Job for testing."""
    status = V1JobStatus(conditions=conditions, active=active)
    return V1Job(
        metadata=V1ObjectMeta(name="test-job", namespace="user-test"),
        status=status,
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
        job = _make_k8s_job(active=1)
        assert determine_status(job) == ("RUNNING", None)

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
        job = V1Job(
            metadata=V1ObjectMeta(name="test-job"),
            status=None,
        )
        assert determine_status(job) == (None, None)
