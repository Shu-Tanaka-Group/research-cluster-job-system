import json
import logging

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from cjob.config import Settings
from cjob.models import Job

logger = logging.getLogger(__name__)

TEMPORARY_STATUS_CODES = {429, 500, 503}


class TemporaryK8sError(Exception):
    pass


class PermanentK8sError(Exception):
    pass


def build_k8s_job(job: Job, settings: Settings) -> k8s_client.V1Job:
    """Build a K8s Job manifest from a DB job row."""
    username = job.user
    k8s_job_name = f"cjob-{username}-{job.job_id}"
    log_dir = job.log_dir

    # Build tee-wrapped command
    user_command = job.command
    wrapped_command = (
        f'LOG_DIR={log_dir}\n'
        f'mkdir -p "${{LOG_DIR}}"\n'
        f'exec > >(tee "${{LOG_DIR}}/stdout.log") '
        f'2> >(tee "${{LOG_DIR}}/stderr.log" >&2)\n'
        f'{user_command}'
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

    pod_spec = k8s_client.V1PodSpec(
        restart_policy="Never",
        tolerations=[
            k8s_client.V1Toleration(
                key="role",
                operator="Equal",
                value="computing",
                effect="NoSchedule",
            )
        ],
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
        spec=k8s_client.V1JobSpec(
            ttl_seconds_after_finished=10800,
            template=k8s_client.V1PodTemplateSpec(spec=pod_spec),
        ),
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
