from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from cjob.config import get_settings

_engine = None
_SessionLocal = None


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
    return _engine


def _get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine())
    return _SessionLocal


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a session with auto-commit/rollback."""
    session = _get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_session() -> Session:
    """Create a standalone session for daemon loops (Dispatcher/Watcher).

    Caller is responsible for commit/rollback/close.
    """
    return _get_session_factory()()
