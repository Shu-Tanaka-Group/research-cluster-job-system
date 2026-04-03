import pytest

from cjob.config import Settings
from cjob.dispatcher.k8s_job import _parse_taint, build_k8s_job
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
