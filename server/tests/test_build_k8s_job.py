import json
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from cjob.config import Settings
from cjob.dispatcher.k8s_job import (
    PermanentK8sError,
    QuotaExceededError,
    TemporaryK8sError,
    _extract_k8s_error_message,
    _parse_taint,
    build_k8s_job,
    create_k8s_job,
)
from cjob.models import Job


def _make_job(**overrides):
    """Helper to build a minimal Job model for testing."""
    defaults = dict(
        namespace="user-alice",
        job_id=1,
        user="alice",
        image="your-registry/cjob-jupyter:2.1.0",
        command="python main.py",
        cwd="/home/jovyan/project-a",
        env_json={"PYTHONPATH": "/home/jovyan/project-a"},
        cpu="2",
        memory="4Gi",
        gpu=0,
        time_limit_seconds=86400,
        status="DISPATCHING",
        log_dir="/home/jovyan/.cjob/logs/1",
    )
    defaults.update(overrides)
    return Job(**defaults)


def _make_settings(**overrides):
    """Helper to build Settings with test defaults."""
    env = {
        "WORKSPACE_MOUNT_PATH": "/home/jovyan",
        "KUEUE_LOCAL_QUEUE_NAME": "default",
        "POSTGRES_PASSWORD": "test",
    }
    env.update(overrides)
    return Settings(**env)


class TestBuildK8sJob:
    def test_basic_structure(self):
        job = _make_job()
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        assert manifest.api_version == "batch/v1"
        assert manifest.kind == "Job"
        assert manifest.metadata.name == "cjob-alice-1"
        assert manifest.metadata.namespace == "user-alice"

    def test_labels(self):
        job = _make_job()
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        labels = manifest.metadata.labels
        assert labels["kueue.x-k8s.io/queue-name"] == "default"
        assert labels["cjob.io/job-id"] == "1"
        assert labels["cjob.io/namespace"] == "user-alice"

    def test_active_deadline_seconds(self):
        job = _make_job(time_limit_seconds=3600)
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        assert manifest.spec.active_deadline_seconds == 3600

    def test_active_deadline_seconds_default(self):
        job = _make_job(time_limit_seconds=86400)
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        assert manifest.spec.active_deadline_seconds == 86400

    def test_ttl_seconds(self):
        job = _make_job()
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        assert manifest.spec.ttl_seconds_after_finished == 300

    def test_backoff_limit(self):
        job = _make_job()
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        assert manifest.spec.backoff_limit == 0

    def test_container_resources(self):
        job = _make_job(cpu="4", memory="8Gi")
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert container.resources.requests == {"cpu": "4", "memory": "8Gi"}
        assert container.resources.limits == {"cpu": "4", "memory": "8Gi"}

    def test_gpu_resource_in_manifest(self):
        import json
        job = _make_job(gpu=2, flavor="gpu")
        settings = _make_settings(RESOURCE_FLAVORS=json.dumps([
            {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
            {"name": "gpu", "label_selector": "cjob.io/flavor=gpu", "gpu_resource_name": "nvidia.com/gpu"},
        ]))
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert container.resources.requests["nvidia.com/gpu"] == "2"
        assert container.resources.limits["nvidia.com/gpu"] == "2"
        # CPU and memory should still be present
        assert container.resources.requests["cpu"] == "2"
        assert container.resources.requests["memory"] == "4Gi"

    def test_amd_gpu_resource_name(self):
        """AMD GPU flavor should use amd.com/gpu resource name."""
        import json
        job = _make_job(gpu=1, flavor="gpu-amd")
        settings = _make_settings(RESOURCE_FLAVORS=json.dumps([
            {"name": "gpu-amd", "label_selector": "cjob.io/flavor=gpu-amd", "gpu_resource_name": "amd.com/gpu"},
        ]))
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert container.resources.requests["amd.com/gpu"] == "1"
        assert container.resources.limits["amd.com/gpu"] == "1"
        assert "nvidia.com/gpu" not in container.resources.requests

    def test_no_gpu_resource_when_zero(self):
        job = _make_job(gpu=0)
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert "nvidia.com/gpu" not in container.resources.requests
        assert "nvidia.com/gpu" not in container.resources.limits

    def test_container_image(self):
        job = _make_job(image="custom:1.0")
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert container.image == "custom:1.0"

    def test_working_dir(self):
        job = _make_job(cwd="/home/jovyan/exp")
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert container.working_dir == "/home/jovyan/exp"

    def test_env_vars(self):
        job = _make_job(env_json={"MY_VAR": "hello", "OTHER": "world"})
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        env_names = {e.name: e.value for e in container.env}
        assert env_names["PYTHONUNBUFFERED"] == "1"
        assert env_names["MY_VAR"] == "hello"
        assert env_names["OTHER"] == "world"

    def test_env_json_empty(self):
        job = _make_job(env_json={})
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        env_names = [e.name for e in container.env]
        assert "PYTHONUNBUFFERED" in env_names

    def test_volume_mount(self):
        job = _make_job()
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        pod_spec = manifest.spec.template.spec
        assert pod_spec.volumes[0].persistent_volume_claim.claim_name == "alice"
        assert pod_spec.containers[0].volume_mounts[0].mount_path == "/home/jovyan"

    def test_command_wrapping(self):
        job = _make_job(command="echo hello")
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert container.command == ["/bin/bash", "-lc"]
        assert "echo hello" in container.args[0]
        assert "LOG_DIR=" in container.args[0]
        assert "tee" in container.args[0]

    def test_tolerations_default(self):
        job = _make_job()
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        tolerations = manifest.spec.template.spec.tolerations
        assert len(tolerations) == 1
        assert tolerations[0].key == "role"
        assert tolerations[0].value == "computing"
        assert tolerations[0].effect == "NoSchedule"

    def test_tolerations_custom(self):
        job = _make_job()
        settings = _make_settings(JOB_NODE_TAINT="gpu=true:NoExecute")
        manifest = build_k8s_job(job, settings)

        tolerations = manifest.spec.template.spec.tolerations
        assert len(tolerations) == 1
        assert tolerations[0].key == "gpu"
        assert tolerations[0].value == "true"
        assert tolerations[0].effect == "NoExecute"

    def test_tolerations_empty(self):
        job = _make_job()
        settings = _make_settings(JOB_NODE_TAINT="")
        manifest = build_k8s_job(job, settings)

        assert manifest.spec.template.spec.tolerations is None

    def test_node_selector_cpu_flavor(self):
        job = _make_job(flavor="cpu")
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        assert manifest.spec.template.spec.node_selector == {"cjob.io/flavor": "cpu"}

    def test_node_selector_gpu_flavor_without_gpu(self):
        """GPU flavor with gpu=0 should still get GPU node selector."""
        import json
        job = _make_job(gpu=0, flavor="gpu")
        settings = _make_settings(RESOURCE_FLAVORS=json.dumps([
            {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
            {"name": "gpu", "label_selector": "cjob.io/flavor=gpu", "gpu_resource_name": "nvidia.com/gpu"},
        ]))
        manifest = build_k8s_job(job, settings)

        assert manifest.spec.template.spec.node_selector == {"cjob.io/flavor": "gpu"}

    def test_node_selector_unknown_flavor(self):
        """Unknown flavor should result in no node selector."""
        job = _make_job(flavor="nonexistent")
        settings = _make_settings()
        manifest = build_k8s_job(job, settings)

        assert manifest.spec.template.spec.node_selector is None

    def test_cpu_limit_buffer_multiplier(self):
        """CPU limit should be buffered when multiplier > 1.0."""
        job = _make_job(cpu="2", memory="4Gi")
        settings = _make_settings(CPU_LIMIT_BUFFER_MULTIPLIER=1.05)
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert container.resources.requests["cpu"] == "2"
        assert container.resources.limits["cpu"] == "2100m"

    def test_cpu_limit_buffer_does_not_affect_memory(self):
        """Memory should remain unchanged regardless of CPU buffer."""
        job = _make_job(cpu="2", memory="4Gi")
        settings = _make_settings(CPU_LIMIT_BUFFER_MULTIPLIER=1.05)
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert container.resources.requests["memory"] == "4Gi"
        assert container.resources.limits["memory"] == "4Gi"

    def test_cpu_limit_buffer_with_gpu(self):
        """GPU resources should not be affected by CPU buffer."""
        import json
        job = _make_job(gpu=2, flavor="gpu")
        settings = _make_settings(
            CPU_LIMIT_BUFFER_MULTIPLIER=1.05,
            RESOURCE_FLAVORS=json.dumps([
                {"name": "gpu", "label_selector": "cjob.io/flavor=gpu", "gpu_resource_name": "nvidia.com/gpu"},
            ]),
        )
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert container.resources.requests["nvidia.com/gpu"] == "2"
        assert container.resources.limits["nvidia.com/gpu"] == "2"
        assert container.resources.requests["cpu"] == "2"
        assert container.resources.limits["cpu"] == "2100m"

    def test_cpu_limit_buffer_millicores_input(self):
        """Buffer should work correctly with millicores input."""
        job = _make_job(cpu="500m", memory="1Gi")
        settings = _make_settings(CPU_LIMIT_BUFFER_MULTIPLIER=1.05)
        manifest = build_k8s_job(job, settings)

        container = manifest.spec.template.spec.containers[0]
        assert container.resources.requests["cpu"] == "500m"
        assert container.resources.limits["cpu"] == "525m"


class TestParseTaint:
    def test_valid_taint(self):
        result = _parse_taint("role=computing:NoSchedule")
        assert result.key == "role"
        assert result.value == "computing"
        assert result.operator == "Equal"
        assert result.effect == "NoSchedule"

    def test_empty_string(self):
        assert _parse_taint("") is None

    def test_no_execute_effect(self):
        result = _parse_taint("gpu=true:NoExecute")
        assert result.effect == "NoExecute"

    def test_prefer_no_schedule_effect(self):
        result = _parse_taint("key=val:PreferNoSchedule")
        assert result.effect == "PreferNoSchedule"

    def test_empty_value(self):
        result = _parse_taint("key=:NoSchedule")
        assert result.key == "key"
        assert result.value == ""

    def test_invalid_no_colon(self):
        with pytest.raises(ValueError, match="expected 'key=value:effect'"):
            _parse_taint("role=computing")

    def test_invalid_no_equals(self):
        with pytest.raises(ValueError, match="expected 'key=value:effect'"):
            _parse_taint("role:NoSchedule")

    def test_invalid_effect(self):
        with pytest.raises(ValueError, match="Invalid taint effect"):
            _parse_taint("role=computing:BadEffect")

    def test_invalid_empty_key(self):
        with pytest.raises(ValueError, match="key must not be empty"):
            _parse_taint("=computing:NoSchedule")


# ── create_k8s_job error classification ──


def _make_api_exception(status, message=None, reason="Forbidden"):
    """Build an ApiException with a realistic K8s Status body."""
    exc = ApiException(status=status, reason=reason)
    if message is not None:
        exc.body = json.dumps({
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "message": message,
            "reason": reason,
            "code": status,
        })
    else:
        exc.body = None
    return exc


def _make_manifest():
    """Build a minimal V1Job manifest that create_k8s_job can pass to the API."""
    job = _make_job()
    settings = _make_settings()
    return build_k8s_job(job, settings)


class TestExtractK8sErrorMessage:
    def test_body_with_message(self):
        exc = _make_api_exception(403, message="boom")
        assert _extract_k8s_error_message(exc) == "boom"

    def test_body_missing_falls_back_to_reason(self):
        exc = _make_api_exception(403, message=None, reason="Forbidden")
        assert _extract_k8s_error_message(exc) == "Forbidden"

    def test_invalid_json_falls_back_to_reason(self):
        exc = ApiException(status=403, reason="Forbidden")
        exc.body = "<html>not json</html>"
        assert _extract_k8s_error_message(exc) == "Forbidden"

    def test_body_dict_without_message(self):
        exc = ApiException(status=403, reason="Forbidden")
        exc.body = json.dumps({"code": 403})
        assert _extract_k8s_error_message(exc) == "Forbidden"


class TestCreateK8sJobErrorClassification:
    """create_k8s_job must distinguish ResourceQuota 403 (recoverable)
    from other 403s (permanent) and from transient 429/500/503."""

    @patch("cjob.dispatcher.k8s_job.k8s_client.BatchV1Api")
    def test_403_exceeded_quota_raises_quota_exceeded_error(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api.create_namespaced_job.side_effect = _make_api_exception(
            403,
            message=(
                'jobs.batch "cjob-alice-1" is forbidden: '
                "exceeded quota: computing-quota, "
                "requested: count/jobs.batch=1, used: count/jobs.batch=50, "
                "limited: count/jobs.batch=50"
            ),
        )
        mock_api_cls.return_value = mock_api

        with pytest.raises(QuotaExceededError) as exc_info:
            create_k8s_job(_make_manifest())
        assert "exceeded quota" in str(exc_info.value)

    @patch("cjob.dispatcher.k8s_job.k8s_client.BatchV1Api")
    def test_403_rbac_raises_permanent_error(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api.create_namespaced_job.side_effect = _make_api_exception(
            403,
            message=(
                "jobs.batch is forbidden: User "
                '"system:serviceaccount:cjob-system:dispatcher-sa" '
                'cannot create resource "jobs" in API group "batch"'
            ),
        )
        mock_api_cls.return_value = mock_api

        with pytest.raises(PermanentK8sError) as exc_info:
            create_k8s_job(_make_manifest())
        # detailed message should be included for debugging
        assert "cannot create resource" in str(exc_info.value)

    @patch("cjob.dispatcher.k8s_job.k8s_client.BatchV1Api")
    def test_429_raises_temporary_error(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api.create_namespaced_job.side_effect = _make_api_exception(
            429, message="Too Many Requests", reason="Too Many Requests",
        )
        mock_api_cls.return_value = mock_api

        with pytest.raises(TemporaryK8sError):
            create_k8s_job(_make_manifest())

    @patch("cjob.dispatcher.k8s_job.k8s_client.BatchV1Api")
    def test_503_raises_temporary_error(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api.create_namespaced_job.side_effect = _make_api_exception(
            503, message="Service Unavailable", reason="Service Unavailable",
        )
        mock_api_cls.return_value = mock_api

        with pytest.raises(TemporaryK8sError):
            create_k8s_job(_make_manifest())

    @patch("cjob.dispatcher.k8s_job.k8s_client.BatchV1Api")
    def test_403_without_body_falls_back_to_permanent(self, mock_api_cls):
        mock_api = MagicMock()
        exc = ApiException(status=403, reason="Forbidden")
        exc.body = None
        mock_api.create_namespaced_job.side_effect = exc
        mock_api_cls.return_value = mock_api

        # No "exceeded quota" string available -> cannot be classified as
        # quota race, so it falls back to permanent.
        with pytest.raises(PermanentK8sError) as exc_info:
            create_k8s_job(_make_manifest())
        assert "403" in str(exc_info.value)

    @patch("cjob.dispatcher.k8s_job.k8s_client.BatchV1Api")
    def test_422_raises_permanent_error(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api.create_namespaced_job.side_effect = _make_api_exception(
            422, message="invalid manifest", reason="Unprocessable Entity",
        )
        mock_api_cls.return_value = mock_api

        with pytest.raises(PermanentK8sError) as exc_info:
            create_k8s_job(_make_manifest())
        assert "invalid manifest" in str(exc_info.value)
