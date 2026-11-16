"""FastAPI application entrypoint for the ModernEdge assessment backend."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import init_db

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


async def _maybe_run_pipeline() -> None:
    """
    Optionally kick off the ingest → resolve → payroll pipeline on startup.

    The pipeline module is loaded lazily so the API can boot before those
    packages are fully implemented. Set AUTO_RUN_PIPELINE=true to enable.
    """
    settings = get_settings()
    if not settings.auto_run_pipeline:
        logger.info("AUTO_RUN_PIPELINE disabled; skipping startup pipeline")
        return

    try:
        from app.ingest.pipeline import run_pipeline  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "AUTO_RUN_PIPELINE is set but app.ingest.pipeline.run_pipeline "
            "is not available yet; skipping"
        )
        return

    logger.info("Starting startup pipeline")
    try:
        result = run_pipeline()
        logger.info("Startup pipeline finished: %s", result)
    except Exception:
        logger.exception("Startup pipeline failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Initializing database schema")
    init_db()

    await _maybe_run_pipeline()
    yield
    logger.info("Shutting down ModernEdge backend")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="ModernEdge Assessment API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "payroll_dry_run": settings.payroll_dry_run,
            "data_dir": str(settings.data_dir),
            "output_dir": str(settings.output_dir),
        }

    # Routers are mounted here as modules land, e.g.:
    #   from app.api import buildings, kpis, chat, review
    #   app.include_router(buildings.router, prefix="/api")
    _mount_routers(app)

    return app


def _mount_routers(app: FastAPI) -> None:
    """Best-effort include of API routers when they exist."""
    router_modules = (
        "app.api.buildings",
        "app.api.kpis",
        "app.api.chat",
        "app.api.review",
        "app.api.pipeline",
    )
    for module_path in router_modules:
        try:
            module = __import__(module_path, fromlist=["router"])
        except ImportError:
            continue
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router, prefix="/api")
            logger.info("Mounted router from %s", module_path)


app = create_app()
