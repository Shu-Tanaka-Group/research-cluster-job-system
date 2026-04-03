from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker

from cjob.config import Settings
from cjob.models import Base, FlavorQuota, Job, JobEvent, NamespaceDailyUsage, NamespaceWeight


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
