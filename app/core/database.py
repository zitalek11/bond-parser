from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _build_engine(database_url: str, echo: bool = False) -> AsyncEngine:
    is_sqlite = database_url.startswith("sqlite")
    kwargs: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}

    if is_sqlite:
        kwargs["connect_args"] = {"timeout": 30}
    else:
        kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)

    engine = create_async_engine(database_url, **kwargs)

    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - driver hook
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


engine: AsyncEngine = _build_engine(settings.database_url, settings.db_echo)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, autoflush=False
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for background jobs (scheduler, pipeline)."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _ensure_column(sync_conn, table: str, column: str, ddl_type: str) -> None:
    """Idempotent ADD COLUMN for SQLite/Postgres MVP (until Alembic is wired)."""
    dialect = sync_conn.dialect.name
    if dialect == "sqlite":
        rows = sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        existing = {r[1] for r in rows}
        if column not in existing:
            sync_conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"
            )
            logger.info("Added column %s.%s", table, column)
    else:
        sync_conn.exec_driver_sql(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
        )


async def init_db() -> None:
    """Create tables. For production schema evolution use Alembic migrations."""
    from app.models import Base  # noqa: WPS433 - registers all models in metadata

    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(
            lambda c: _ensure_column(c, "bond_issues", "exported_at", "TIMESTAMP")
        )
        await conn.run_sync(
            lambda c: _ensure_column(c, "amortization_schedules", "coupon_number", "INTEGER")
        )
        await conn.run_sync(
            lambda c: _ensure_column(
                c, "amortization_schedules", "residual_nominal_percent", "NUMERIC(9,4)"
            )
        )
    logger.info("Database initialised: %s", engine.url.render_as_string(hide_password=True))


async def dispose_db() -> None:
    await engine.dispose()
