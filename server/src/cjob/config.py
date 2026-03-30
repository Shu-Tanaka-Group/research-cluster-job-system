import json
from functools import lru_cache

from pydantic import BaseModel
from pydantic_settings import BaseSettings


class FlavorDefinition(BaseModel):
    name: str
    label_selector: str
    gpu_resource_name: str | None = None


class Settings(BaseSettings):
    # PostgreSQL
    POSTGRES_HOST: str = "postgres.cjob-system.svc.cluster.local"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "cjob"
    POSTGRES_USER: str = "cjob"
    POSTGRES_PASSWORD: str = ""

    # Dispatcher
    DISPATCH_BUDGET_PER_NAMESPACE: int = 32
    DISPATCH_BATCH_SIZE: int = 50
    DISPATCH_BUDGET_CHECK_INTERVAL_SEC: int = 10
    DISPATCH_ROUND_SIZE: int = 1                   # jobs per namespace per round-robin round
    DISPATCH_RETRY_INTERVAL_SEC: int = 30
    DISPATCH_MAX_RETRIES: int = 5

    # Dispatcher - Gap Filling
    GAP_FILLING_ENABLED: bool = True
    GAP_FILLING_STALL_THRESHOLD_SEC: int = 300  # 5 minutes

    # Dispatcher - Fair Sharing
    FAIR_SHARE_WINDOW_DAYS: int = 7

    # ResourceFlavor
    RESOURCE_FLAVORS: str = '[{"name": "cpu", "label_selector": "cluster-job=true"}]'
    DEFAULT_FLAVOR: str = "cpu"
    NODE_RESOURCE_SYNC_INTERVAL_SEC: int = 300     # 5 minutes

    # Submit API
    MAX_QUEUED_JOBS_PER_NAMESPACE: int = 500
    MAX_SWEEP_COMPLETIONS: int = 1000
    DEFAULT_TIME_LIMIT_SECONDS: int = 86400   # 24 hours
    MAX_TIME_LIMIT_SECONDS: int = 604800      # 7 days

    # CLI binary distribution
    CLI_BINARY_DIR: str = "/cli-binary"

    # K8s Job
    TTL_SECONDS_AFTER_FINISHED: int = 300

    # Kueue
    KUEUE_LOCAL_QUEUE_NAME: str = "default"

    # Node Taint
    JOB_NODE_TAINT: str = "role=computing:NoSchedule"

    # Namespace
    USER_NAMESPACE_LABEL: str = "cjob.io/user-namespace=true"

    # Paths
    WORKSPACE_MOUNT_PATH: str = "/home/jovyan"
    LOG_BASE_DIR: str = "/home/jovyan/.cjob/logs"

    # Logging
    LOG_LEVEL: str = "INFO"

    @property
    def flavors(self) -> list[FlavorDefinition]:
        return [FlavorDefinition(**item) for item in json.loads(self.RESOURCE_FLAVORS)]

    def get_flavor_definition(self, name: str) -> FlavorDefinition | None:
        for f in self.flavors:
            if f.name == name:
                return f
        return None

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
