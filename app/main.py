from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import rest, web
from app.core.config import settings
from app.core.database import dispose_db, init_db, session_scope
from app.core.logging import get_logger, setup_logging
from app.services.pipeline import BondPipeline

logger = get_logger(__name__)


async def _scheduled_collect(pipeline: BondPipeline) -> None:
    logger.info("Scheduler: starting collection cycle")
    try:
        async with session_scope() as session:
            result = await pipeline.run_collection_cycle(session)
        logger.info("Scheduler: cycle done %s", result)
    except Exception:  # noqa: BLE001
        logger.exception("Scheduler: collection cycle failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    await init_db()

    pipeline = BondPipeline()
    app.state.pipeline = pipeline

    scheduler: AsyncIOScheduler | None = None
    if settings.scheduler_enabled:
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            _scheduled_collect,
            trigger="interval",
            minutes=max(1, settings.collector_interval_minutes),
            args=[pipeline],
            id="bond_collector",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info(
            "APScheduler started: every %s min", settings.collector_interval_minutes
        )
    app.state.scheduler = scheduler

    logger.info(
        "Started %s env=%s db=%s",
        settings.app_name,
        settings.environment,
        "sqlite" if settings.is_sqlite else "postgres",
    )
    yield

    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
    await dispose_db()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        description=(
            "Сервис сбора и проверки эмиссионной документации "
            "по российским облигациям (гибридный конвейер + ручная проверка)."
        ),
    )
    app.include_router(web.router)
    app.include_router(rest.router)

    # Раздача storage на случай прямых ссылок (основной путь — /files/{id})
    storage = Path(settings.storage_dir)
    storage.mkdir(parents=True, exist_ok=True)
    app.mount("/storage", StaticFiles(directory=str(storage)), name="storage")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.environment == "dev",
    )
