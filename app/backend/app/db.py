"""SQLAlchemy engine, session factory, and declarative base."""

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


def _make_engine():
    settings = get_settings()
    url = settings.database_url

    # Prefer Postgres (JSONB-compatible JSON columns). SQLite is supported for
    # local smoke tests without Docker — models use portable sqlalchemy.JSON.
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if url in {"sqlite://", "sqlite:///:memory:"} or ":memory:" in url:
            engine = create_engine(
                "sqlite://",
                connect_args=connect_args,
                poolclass=StaticPool,
                future=True,
            )
        else:
            engine = create_engine(
                url,
                connect_args=connect_args,
                future=True,
            )

        @event.listens_for(engine, "connect")
        def _sqlite_fk(dbapi_conn, _connection_record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


engine = _make_engine()
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables registered on Base.metadata."""
    # Import models so metadata is populated before create_all.
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
