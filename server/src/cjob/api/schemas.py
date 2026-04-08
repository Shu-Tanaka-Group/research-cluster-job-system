from datetime import datetime

from pydantic import BaseModel, Field


class ResourceSpec(BaseModel):
    cpu: str = "1"
    memory: str = "1Gi"
    gpu: int = 0
    flavor: str | None = None


class JobSubmitRequest(BaseModel):
    command: str
    image: str
    cwd: str
    env: dict[str, str] = Field(default_factory=dict)
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    time_limit_seconds: int | None = None


class SweepSubmitRequest(BaseModel):
    command: str
    image: str
    cwd: str
    env: dict[str, str] = Field(default_factory=dict)
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    completions: int
    parallelism: int = 1
    time_limit_seconds: int | None = None


class JobSubmitResponse(BaseModel):
    job_id: int
    status: str


class JobSummary(BaseModel):
    job_id: int
    status: str
    flavor: str
    command: str
    created_at: datetime
    finished_at: datetime | None = None
    time_limit_seconds: int
    completions: int | None = None
    parallelism: int | None = None
    succeeded_count: int | None = None
    failed_count: int | None = None


class JobListResponse(BaseModel):
    jobs: list[JobSummary]
    total_count: int
    log_base_dir: str


class JobDetailResponse(BaseModel):
    job_id: int
    status: str
    namespace: str
    command: str
    cwd: str
    cpu: str
    memory: str
    gpu: int
    flavor: str
    time_limit_seconds: int
    k8s_job_name: str | None
    log_dir: str | None
    created_at: datetime
    dispatched_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None = None
    completions: int | None = None
    parallelism: int | None = None
    succeeded_count: int | None = None
    failed_count: int | None = None
    completed_indexes: str | None = None
    failed_indexes: str | None = None
    node_name: list[str] | None = None


class SingleCancelResponse(BaseModel):
    job_id: int
    status: str


class CancelRequest(BaseModel):
    job_ids: list[int]


class CancelResponse(BaseModel):
    cancelled: list[int]
    skipped: list[int]
    not_found: list[int]


class SkippedItem(BaseModel):
    job_id: int
    reason: str


class HoldRequest(BaseModel):
    job_ids: list[int] | None = None


class HoldResponse(BaseModel):
    held: list[int]
    skipped: list[int]
    not_found: list[int]


class SingleHoldResponse(BaseModel):
    job_id: int
    status: str


class ReleaseRequest(BaseModel):
    job_ids: list[int] | None = None


class ReleaseResponse(BaseModel):
    released: list[int]
    skipped: list[int]
    not_found: list[int]


class SingleReleaseResponse(BaseModel):
    job_id: int
    status: str


class SetParams(BaseModel):
    cpu: str | None = None
    memory: str | None = None
    gpu: int | None = None
    flavor: str | None = None
    time_limit_seconds: int | None = None


class SetRequest(SetParams):
    job_ids: list[int]


class SingleSetResponse(BaseModel):
    job_id: int
    status: str


class SetResponse(BaseModel):
    modified: list[int]
    skipped: list[int]
    not_found: list[int]


class DeleteRequest(BaseModel):
    job_ids: list[int] | None = None


class DeleteResponse(BaseModel):
    deleted: list[int]
    skipped: list[SkippedItem]
    not_found: list[int]
    log_dirs: list[str]


class DailyUsage(BaseModel):
    date: str
    cpu_millicores_seconds: int
    memory_mib_seconds: int
    gpu_seconds: int


class ResourceQuota(BaseModel):
    hard_cpu_millicores: int
    hard_memory_mib: int
    hard_gpu: int
    hard_count: int | None = None
    used_cpu_millicores: int
    used_memory_mib: int
    used_gpu: int
    used_count: int | None = None


class UsageResponse(BaseModel):
    window_days: int
    daily: list[DailyUsage]
    total_cpu_millicores_seconds: int
    total_memory_mib_seconds: int
    total_gpu_seconds: int
    resource_quota: ResourceQuota | None = None


class ResetResponse(BaseModel):
    status: str


class ResetErrorResponse(BaseModel):
    message: str
    blocking_job_ids: list[int] | None = None


class FlavorNodeInfo(BaseModel):
    node_name: str
    cpu_millicores: int
    memory_mib: int
    gpu: int


class FlavorQuotaInfo(BaseModel):
    cpu: str
    memory: str
    gpu: str


class FlavorInfo(BaseModel):
    name: str
    has_gpu: bool
    nodes: list[FlavorNodeInfo]
    quota: FlavorQuotaInfo | None = None


class FlavorListResponse(BaseModel):
    flavors: list[FlavorInfo]
    default_flavor: str


class CliVersionResponse(BaseModel):
    version: str


class CliVersionsResponse(BaseModel):
    versions: list[str]
    latest: str
