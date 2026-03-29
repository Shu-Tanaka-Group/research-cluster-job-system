import json
import logging

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from cjob.config import Settings
from cjob.models import Job

logger = logging.getLogger(__name__)

TEMPORARY_STATUS_CODES = {429, 500, 503}

VALID_TAINT_EFFECTS = {"NoSchedule", "PreferNoSchedule", "NoExecute"}


def _parse_taint(taint_str: str) -> k8s_client.V1Toleration | None:
    """Parse a taint string (key=value:effect) into a V1Toleration.

    Returns None if taint_str is empty.
    Raises ValueError if the format is invalid.
    """
    if not taint_str:
        return None

    if ":" not in taint_str:
        raise ValueError(
            f"Invalid taint format '{taint_str}': expected 'key=value:effect'"
        )

    kv_part, effect = taint_str.rsplit(":", 1)
    if effect not in VALID_TAINT_EFFECTS:
        raise ValueError(
            f"Invalid taint effect '{effect}': must be one of {VALID_TAINT_EFFECTS}"
        )

    if "=" not in kv_part:
        raise ValueError(
            f"Invalid taint format '{taint_str}': expected 'key=value:effect'"
        )

    key, value = kv_part.split("=", 1)
    if not key:
        raise ValueError(f"Invalid taint format '{taint_str}': key must not be empty")

    return k8s_client.V1Toleration(
        key=key,
        operator="Equal",
        value=value,
        effect=effect,
    )


class TemporaryK8sError(Exception):
    pass


class PermanentK8sError(Exception):
    pass


def build_k8s_job(job: Job, settings: Settings) -> k8s_client.V1Job:
    """Build a K8s Job manifest from a DB job row."""
    username = job.user
    k8s_job_name = f"cjob-{username}-{job.job_id}"
    log_dir = job.log_dir
    is_sweep = job.completions is not None

    # Build tee-wrapped command
    user_command = job.command
    if is_sweep:
        # Replace _INDEX_ placeholder with $CJOB_INDEX shell variable
        user_command = user_command.replace("_INDEX_", "$CJOB_INDEX")
        wrapped_command = (
            f'export CJOB_INDEX=$JOB_COMPLETION_INDEX\n'
            f'LOG_DIR={log_dir}/$CJOB_INDEX\n'
            f'mkdir -p "$LOG_DIR"\n'
            f'exec > >(tee "$LOG_DIR/stdout.log") '
            f'2> >(tee "$LOG_DIR/stderr.log" >&2)\n'
            f'{user_command}\n'
            f'EXIT_CODE=$?\n'
            f'exec >&- 2>&-\n'
            f'wait\n'
            f'exit $EXIT_CODE'
        )
    else:
        wrapped_command = (
            f'LOG_DIR={log_dir}\n'
            f'mkdir -p "$LOG_DIR"\n'
            f'exec > >(tee "$LOG_DIR/stdout.log") '
            f'2> >(tee "$LOG_DIR/stderr.log" >&2)\n'
            f'{user_command}\n'
            f'EXIT_CODE=$?\n'
            f'exec >&- 2>&-\n'
            f'wait\n'
            f'exit $EXIT_CODE'
        )

    # Build env vars from job.env_json
    env_vars = [
        k8s_client.V1EnvVar(name="PYTHONUNBUFFERED", value="1"),
    ]
    env_data = job.env_json if isinstance(job.env_json, dict) else {}
    for key, value in env_data.items():
        env_vars.append(k8s_client.V1EnvVar(name=key, value=str(value)))

    container = k8s_client.V1Container(
        name="worker",
        image=job.image,
        working_dir=job.cwd,
        command=["/bin/bash", "-lc"],
        args=[wrapped_command],
        env=env_vars,
        volume_mounts=[
            k8s_client.V1VolumeMount(
                name="workspace",
                mount_path=settings.WORKSPACE_MOUNT_PATH,
            )
        ],
        resources=k8s_client.V1ResourceRequirements(
            requests={"cpu": job.cpu, "memory": job.memory},
            limits={"cpu": job.cpu, "memory": job.memory},
        ),
    )

    toleration = _parse_taint(settings.JOB_NODE_TAINT)
    tolerations = [toleration] if toleration else None

    pod_spec = k8s_client.V1PodSpec(
        restart_policy="Never",
        tolerations=tolerations,
        containers=[container],
        volumes=[
            k8s_client.V1Volume(
                name="workspace",
                persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=username,
                ),
            )
        ],
    )

    job_spec_kwargs = {
        "active_deadline_seconds": job.time_limit_seconds,
        "ttl_seconds_after_finished": 10800,
        "template": k8s_client.V1PodTemplateSpec(spec=pod_spec),
    }
    if is_sweep:
        job_spec_kwargs["completion_mode"] = "Indexed"
        job_spec_kwargs["completions"] = job.completions
        job_spec_kwargs["parallelism"] = job.parallelism
        job_spec_kwargs["backoff_limit_per_index"] = 0

    return k8s_client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=k8s_client.V1ObjectMeta(
            name=k8s_job_name,
            namespace=job.namespace,
            labels={
                "kueue.x-k8s.io/queue-name": settings.KUEUE_LOCAL_QUEUE_NAME,
                "cjob.io/job-id": str(job.job_id),
                "cjob.io/namespace": job.namespace,
            },
        ),
        spec=k8s_client.V1JobSpec(**job_spec_kwargs),
    )


def create_k8s_job(job_manifest: k8s_client.V1Job) -> str:
    """Create a K8s Job. Returns k8s_job_name.

    Raises TemporaryK8sError or PermanentK8sError.
    """
    batch_v1 = k8s_client.BatchV1Api()
    namespace = job_manifest.metadata.namespace
    name = job_manifest.metadata.name

    try:
        batch_v1.create_namespaced_job(namespace=namespace, body=job_manifest)
        logger.info("Created K8s Job %s in %s", name, namespace)
        return name
    except ApiException as e:
        if e.status in TEMPORARY_STATUS_CODES:
            raise TemporaryK8sError(f"K8s API temporary error {e.status}: {e.reason}")
        raise PermanentK8sError(f"K8s API permanent error {e.status}: {e.reason}")
