import json
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker

from cjob.config import Settings
from cjob.models import Base, FlavorQuota, Job, JobEvent, NamespaceDailyUsage, NamespaceWeight

try:
    from testcontainers.postgres import PostgresContainer

    HAS_TESTCONTAINERS = True
except ImportError:
    HAS_TESTCONTAINERS = False


def _patch_sqlite_incompatible_types():
    """Replace PostgreSQL-specific types for SQLite compatibility."""
    from sqlalchemy import BigInteger, Integer

    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
            # BigInteger AUTOINCREMENT doesn't work in SQLite;
            # replace with plain Integer for the job_events.id column
            if isinstance(column.type, BigInteger) and column.autoincrement:
                column.type = Integer()


@pytest.fixture()
def db_session():
    """Create an in-memory SQLite database session for testing."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        # Register NOW() function for PostgreSQL compatibility in raw SQL
        dbapi_conn.create_function("NOW", 0, lambda: "2026-01-01 00:00:00")

    _patch_sqlite_incompatible_types()
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def settings():
    """Create test Settings."""
    import json
    return Settings(
        POSTGRES_PASSWORD="test",
        MAX_QUEUED_JOBS_PER_NAMESPACE=10,
        DEFAULT_TIME_LIMIT_SECONDS=86400,
        MAX_TIME_LIMIT_SECONDS=604800,
        RESOURCE_FLAVORS=json.dumps([
            {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
            {"name": "gpu", "label_selector": "cjob.io/flavor=gpu", "gpu_resource_name": "nvidia.com/gpu"},
        ]),
        DEFAULT_FLAVOR="cpu",
    )


@pytest.fixture(autouse=True)
def mock_settings(settings):
    """Patch get_settings to return test settings."""
    with patch("cjob.api.services.get_settings", return_value=settings):
        yield settings


# ── PostgreSQL integration test fixtures ──


@pytest.fixture(scope="session")
def pg_engine():
    """Start a PostgreSQL container and create all tables (session-scoped)."""
    if not HAS_TESTCONTAINERS:
        pytest.skip("testcontainers not installed")
    try:
        with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
            engine = create_engine(pg.get_connection_url())
            Base.metadata.create_all(engine)
            yield engine
    except Exception as e:
        pytest.skip(f"Docker not available: {e}")


@pytest.fixture()
def pg_session(pg_engine):
    """Per-test PostgreSQL session with transaction rollback for isolation."""
    conn = pg_engine.connect()
    txn = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    txn.rollback()
    conn.close()


@pytest.fixture()
def pg_settings():
    """Settings for integration tests."""
    return Settings(
        POSTGRES_PASSWORD="test",
        DISPATCH_BUDGET_PER_NAMESPACE=32,
        DISPATCH_BATCH_SIZE=50,
        DISPATCH_FETCH_MULTIPLIER=10,
        DISPATCH_ROUND_SIZE=1,
        FAIR_SHARE_WINDOW_DAYS=7,
        USAGE_RETENTION_DAYS=7,
        DEFAULT_TIME_LIMIT_SECONDS=86400,
        MAX_TIME_LIMIT_SECONDS=604800,
        GAP_FILLING_ENABLED=False,
        RESOURCE_FLAVORS=json.dumps([
            {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
            {"name": "gpu", "label_selector": "cjob.io/flavor=gpu",
             "gpu_resource_name": "nvidia.com/gpu"},
        ]),
        DEFAULT_FLAVOR="cpu",
    )
