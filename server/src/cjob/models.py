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

    __table_args__ = (
        Index("idx_jobs_k8s_job_name", "k8s_job_name"),
        Index("idx_jobs_namespace_status", "namespace", "status"),
    )


class UserJobCounter(Base):
    __tablename__ = "user_job_counters"

    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    next_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class NamespaceDailyUsage(Base):
    __tablename__ = "namespace_daily_usage"

    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    cpu_millicores_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    memory_mib_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    gpu_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


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
