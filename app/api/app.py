import asyncio
import sys
from collections.abc import AsyncGenerator
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from contextlib import asynccontextmanager
from contextlib import suppress

import uvicorn
from fastapi import FastAPI

from app import context
from app import logging
from app import settings
from app.api import routes
from app.context import Context
from app.pipeline.run import RunOutcome
from app.result import is_error

logger = logging.get_logger(__name__)


def _lifespan(
    context_factory: Callable[[], Context],
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        ctx = context_factory()
        # NOTE: Migrations are safe to run from several processes; the API applies them
        # too, so it never serves a schema older than the code.
        migrated = await ctx.database.migrate(settings.MIGRATIONS_DIRECTORY)

        if is_error(migrated):
            logger.error("API stopped: database migration failed.")
            await ctx.close()
            sys.exit(1)

        app.state.context = ctx
        app.state.run_task = None
        logger.info("API started.")

        try:
            yield
        finally:
            await _stop_run(app.state.run_task)
            await ctx.close()

    return lifespan


async def _stop_run(task: asyncio.Task[RunOutcome] | None) -> None:
    if task is None or task.done():
        return

    logger.warning("Cancelling the in-flight run for shutdown.")
    task.cancel()

    with suppress(asyncio.CancelledError):
        await task


def create_app(context_factory: Callable[[], Context] = context.default) -> FastAPI:
    """`context_factory` lets a test serve the API over a context of fakes."""
    app = FastAPI(title="AI Infra Daily", lifespan=_lifespan(context_factory))
    app.include_router(routes.router)

    return app


def serve(*, host: str, port: int) -> None:
    """Blocks; uvicorn owns the event loop, so this is called from the CLI only."""
    uvicorn.run(
        "app.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        log_config=None,
        # NOTE: Access lines carry the query string, and email links carry tokens.
        access_log=False,
    )
