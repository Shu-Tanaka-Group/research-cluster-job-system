from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL
    POSTGRES_HOST: str = "postgres.cjob-system.svc.cluster.local"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "cjob"
    POSTGRES_USER: str = "cjob"
    POSTGRES_PASSWORD: str = ""

    # Dispatcher
    DISPATCH_BUDGET_PER_NAMESPACE: int = 256
    DISPATCH_BUDGET_CHECK_INTERVAL_SEC: int = 10
    DISPATCH_RETRY_INTERVAL_SEC: int = 30
    DISPATCH_MAX_RETRIES: int = 5

    # Submit API
    MAX_QUEUED_JOBS_PER_NAMESPACE: int = 2000

    # Kueue
    KUEUE_LOCAL_QUEUE_NAME: str = "default"

    # Namespace
    JOB_NAMESPACE_PREFIX: str = "user-"

    # Paths
    WORKSPACE_MOUNT_PATH: str = "/home/jovyan"
    LOG_BASE_DIR: str = "/home/jovyan/.cjob/logs"

    # Logging
    LOG_LEVEL: str = "INFO"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
