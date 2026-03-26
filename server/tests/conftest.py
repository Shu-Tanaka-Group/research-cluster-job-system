from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker

from cjob.config import Settings
from cjob.models import Base, Job, JobEvent


def _patch_jsonb_columns():
    """Replace JSONB columns with JSON for SQLite compatibility."""
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()


@pytest.fixture()
def db_session():
    """Create an in-memory SQLite database session for testing."""
    engine = create_engine("sqlite:///:memory:")

    # SQLite foreign key support
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    _patch_jsonb_columns()
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
    return Settings(
        POSTGRES_PASSWORD="test",
        MAX_QUEUED_JOBS_PER_NAMESPACE=10,
        DEFAULT_TIME_LIMIT_SECONDS=86400,
        MAX_TIME_LIMIT_SECONDS=604800,
    )


@pytest.fixture(autouse=True)
def mock_settings(settings):
    """Patch get_settings to return test settings."""
    with patch("cjob.api.services.get_settings", return_value=settings):
        yield settings
