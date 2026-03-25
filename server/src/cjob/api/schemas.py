from datetime import datetime

from pydantic import BaseModel, Field


class ResourceSpec(BaseModel):
    cpu: str = "1"
    memory: str = "1Gi"
    gpu: int = 0


class JobSubmitRequest(BaseModel):
    command: str = Field(..., min_length=1)
    image: str
    cwd: str
    env: dict[str, str] = Field(default_factory=dict)
    resources: ResourceSpec = Field(default_factory=ResourceSpec)


class JobSubmitResponse(BaseModel):
    job_id: int
    status: str


class JobSummary(BaseModel):
    job_id: int
    status: str
    command: str
    created_at: datetime
    finished_at: datetime | None = None


class JobListResponse(BaseModel):
    jobs: list[JobSummary]
    total_count: int


class JobDetailResponse(BaseModel):
    job_id: int
    status: str
    namespace: str
    command: str
    cwd: str
    k8s_job_name: str | None
    log_dir: str | None
    created_at: datetime
    dispatched_at: datetime | None
    finished_at: datetime | None


class CancelRequest(BaseModel):
    job_ids: list[int]


class CancelResponse(BaseModel):
    cancelled: list[int]
    skipped: list[int]
    not_found: list[int]


class SkippedItem(BaseModel):
    job_id: int
    reason: str


class DeleteRequest(BaseModel):
    job_ids: list[int] | None = None


class DeleteResponse(BaseModel):
    deleted: list[int]
    skipped: list[SkippedItem]
    not_found: list[int]


class ResetResponse(BaseModel):
    status: str


class ResetErrorResponse(BaseModel):
    message: str
    blocking_job_ids: list[int] | None = None
