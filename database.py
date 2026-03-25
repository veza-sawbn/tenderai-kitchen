import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker


Base = declarative_base()

_engine = None
_SessionFactory = None


def normalize_database_url(url: str) -> str:
    if not url:
        return "sqlite:///tenderai_local.db"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def get_database_url() -> str:
    return normalize_database_url(os.getenv("DATABASE_URL", "").strip())


def get_engine():
    global _engine
    if _engine is None:
        database_url = get_database_url()
        connect_args = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        _engine = create_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=connect_args,
        )
    return _engine


def get_session_factory():
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = scoped_session(
            sessionmaker(
                bind=get_engine(),
                autoflush=False,
                autocommit=False,
                future=True,
            )
        )
    return _SessionFactory


def init_db():
    from models import AnalysisJob, IngestRun, Profile, ProfileIssue, TenderCache, TenderDocumentCache  # noqa: F401

    Base.metadata.create_all(bind=get_engine())


@contextmanager
def get_db_session():
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
