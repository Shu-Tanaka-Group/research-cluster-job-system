from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user: Mapped[str] = mapped_column("user", String, nullable=False)
    image: Mapped[str] = mapped_column(String, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    env_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    cpu: Mapped[str] = mapped_column(String, nullable=False)
    memory: Mapped[str] = mapped_column(String, nullable=False)
    gpu: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    flavor: Mapped[str] = mapped_column(String, nullable=False, server_default="cpu")
    time_limit_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    k8s_job_name: Mapped[str | None] = mapped_column(String)
    log_dir: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    completions: Mapped[int | None] = mapped_column(Integer)
    parallelism: Mapped[int | None] = mapped_column(Integer)
    completed_indexes: Mapped[str | None] = mapped_column(Text)
    failed_indexes: Mapped[str | None] = mapped_column(Text)
    succeeded_count: Mapped[int | None] = mapped_column(Integer)
    failed_count: Mapped[int | None] = mapped_column(Integer)
    node_name: Mapped[str | None] = mapped_column(String)
    cpu_millicores: Mapped[int | None] = mapped_column(Integer)
    memory_mib: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("idx_jobs_k8s_job_name", "k8s_job_name"),
        Index("idx_jobs_namespace_status", "namespace", "status"),
    )


class UserJobCounter(Base):
    __tablename__ = "user_job_counters"

    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    next_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class NamespaceWeight(Base):
    __tablename__ = "namespace_weights"

    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class NamespaceDailyUsage(Base):
    __tablename__ = "namespace_daily_usage"

    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    cpu_millicores_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    memory_mib_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    gpu_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class NodeResource(Base):
    __tablename__ = "node_resources"

    node_name: Mapped[str] = mapped_column(String, primary_key=True)
    cpu_millicores: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    flavor: Mapped[str] = mapped_column(String, nullable=False, server_default="cpu")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FlavorQuota(Base):
    __tablename__ = "flavor_quotas"

    flavor: Mapped[str] = mapped_column(String, primary_key=True)
    cpu: Mapped[str] = mapped_column(String, nullable=False)
    memory: Mapped[str] = mapped_column(String, nullable=False)
    gpu: Mapped[str] = mapped_column(String, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NamespaceResourceQuota(Base):
    __tablename__ = "namespace_resource_quotas"

    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    hard_cpu_millicores: Mapped[int] = mapped_column(Integer, nullable=False)
    hard_memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    hard_gpu: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    hard_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_cpu_millicores: Mapped[int] = mapped_column(Integer, nullable=False)
    used_memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    used_gpu: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    used_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace: Mapped[str] = mapped_column(String, nullable=False)
    job_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["namespace", "job_id"],
            ["jobs.namespace", "jobs.job_id"],
            ondelete="CASCADE",
        ),
    )
