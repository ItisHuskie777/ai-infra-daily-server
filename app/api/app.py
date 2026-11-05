import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextlib import suppress

import uvicorn
from fastapi import FastAPI

from app import context
from app import logging
from app.api import routes
from app.pipeline.run import RunOutcome

logger = logging.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    ctx = context.default()
    app.state.context = ctx
    app.state.run_task = None
    logger.info("API started.")

    try:
        yield
    finally:
        await _stop_run(app.state.run_task)
        await ctx.close()


async def _stop_run(task: asyncio.Task[RunOutcome] | None) -> None:
    if task is None or task.done():
        return

    logger.warning("Cancelling the in-flight run for shutdown.")
    task.cancel()

    with suppress(asyncio.CancelledError):
        await task


def create_app() -> FastAPI:
    app = FastAPI(title="AI Infra Daily", lifespan=_lifespan)
    app.include_router(routes.router)

    return app


def serve(*, host: str, port: int) -> None:
    """Blocks; uvicorn owns the event loop, so this is called from the CLI only."""
    uvicorn.run(
        "app.api.app:create_app", factory=True, host=host, port=port, log_config=None
    )
