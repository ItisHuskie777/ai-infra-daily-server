import asyncio
from datetime import timedelta
from html import escape
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from pydantic import Field

from app import logging
from app import settings
from app.api import auth
from app.context import AbstractContext
from app.models import DeliveryStatus
from app.models import DigestStyle
from app.models import Feedback
from app.models import FeedbackSignal
from app.models import Interests
from app.models import Model
from app.models import Profile
from app.models import Source
from app.models import Thread
from app.pipeline import feedback
from app.pipeline import feedback_token
from app.pipeline import run
from app.repositories import StoreError
from app.result import ServiceError
from app.result import is_error

logger = logging.get_logger(__name__)

_DEFAULT_DIGEST_LIMIT = 10
_MAX_DIGEST_LIMIT = 50
_SIGNALS = {signal.value for signal in FeedbackSignal}

router = APIRouter()
owner = APIRouter(prefix="/api", dependencies=[Depends(auth.require_owner)])


class SourcePatch(Model):
    enabled: bool | None = None
    weight: float | None = Field(default=None, gt=0, le=2)


class RunRequest(Model):
    force: bool = False
    dry_run: bool = False


class PreferencesPayload(Model):
    profile: Profile | None = None
    interests: Interests | None = None


class DigestSummary(Model):
    id: str
    date: str
    style: DigestStyle
    subject: str
    delivery_status: DeliveryStatus
    delivery_error: str | None
    created_at: str


class HealthResponse(Model):
    ok: bool
    service: str


class SourcesResponse(Model):
    sources: tuple[Source, ...]


class SourceResponse(Model):
    source: Source


class DigestsResponse(Model):
    digests: tuple[DigestSummary, ...]


class ThreadsResponse(Model):
    threads: tuple[Thread, ...]


class PreferencesResponse(Model):
    profile: Profile
    interests: Interests


class RunResponse(Model):
    run_id: str


def _context(request: Request) -> AbstractContext:
    ctx: AbstractContext = request.app.state.context

    return ctx


type Ctx = Annotated[AbstractContext, Depends(_context)]


def _error(error: ServiceError | str, status: int) -> JSONResponse:
    return JSONResponse({"error": str(error)}, status_code=status)


def _store_status(error: ServiceError) -> int:
    match error:
        case StoreError.NOT_FOUND:
            return 404
        case StoreError.CONFLICT:
            return 409
        case _:
            return 500


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title><style>body{{font-family:Georgia,serif;"
        "max-width:32rem;margin:4rem auto;padding:0 1rem;color:#1f2933}</style>"
        f"</head><body><h1>{escape(title)}</h1><p>{escape(body)}</p></body></html>"
    )

    return HTMLResponse(html, status_code=status)


def _log_run_result(task: asyncio.Task[run.RunOutcome]) -> None:
    if task.cancelled():
        logger.warning("Manual run cancelled.", extra={"task": task.get_name()})

        return

    error = task.exception()

    if error is not None:
        logger.error(
            "Manual run crashed.",
            extra={"task": task.get_name(), "error": repr(error)},
        )

        return

    outcome = task.result()
    logger.info(
        "Manual run finished.",
        extra={"digest_id": outcome.digest_id, "reason": outcome.reason},
    )


@router.get("/api/health")
async def health() -> HealthResponse:
    return HealthResponse(ok=True, service="ai-infra-daily")


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_link(
    ctx: Ctx,
    *,
    token: str = "",
    signal: str = "",
    event: str | None = None,
    source: str | None = None,
    item: str | None = None,
) -> HTMLResponse:
    if token == "" or signal not in _SIGNALS:
        return _page(
            "Invalid feedback link",
            "This link is missing required parameters.",
            status=400,
        )

    now = ctx.clock.now()
    claims = feedback_token.verify(settings.FEEDBACK_SECRET, token, now=now)

    if is_error(claims):
        return _page(
            "Invalid feedback link", "This link is invalid or has expired.", status=400
        )

    received = Feedback(
        created_at=now,
        digest_id=claims.digest_id,
        event_id=event,
        item_id=item,
        source_id=source,
        signal=FeedbackSignal(signal),
    )
    interests = await ctx.preferences.interests()
    outcome = await feedback.apply_feedback(ctx, received, interests)

    if is_error(outcome):
        logger.error(
            "Applying feedback failed.", extra={"error": str(outcome), "signal": signal}
        )

        return _page(
            "Something went wrong", "Your feedback could not be recorded.", status=500
        )

    await ctx.preferences.save_interests(outcome.interests, now=now)
    logger.info(
        "Feedback recorded.",
        extra={"signal": signal, "event_id": event, "source_id": source},
    )

    return _page("Thanks, noted", "Tomorrow's digest will take this into account.")


@owner.get("/sources")
async def list_sources(ctx: Ctx) -> SourcesResponse:
    return SourcesResponse(sources=tuple(await ctx.sources.list_all()))


@owner.patch("/sources/{source_id}", response_model=SourceResponse)
async def patch_source(
    source_id: str, patch: SourcePatch, ctx: Ctx
) -> SourceResponse | JSONResponse:
    if patch.enabled is None and patch.weight is None:
        return _error("enabled or weight is required", 400)

    now = ctx.clock.now()

    if patch.enabled is not None:
        result = await ctx.sources.set_enabled(
            source_id, enabled=patch.enabled, now=now
        )

        if is_error(result):
            return _error(result, _store_status(result))

    if patch.weight is not None:
        result = await ctx.sources.set_weight(source_id, weight=patch.weight, now=now)

        if is_error(result):
            return _error(result, _store_status(result))

    found = await ctx.sources.find_by_id(source_id)

    if is_error(found):
        return _error(found, _store_status(found))

    logger.info("Source patched.", extra={"source_id": source_id})

    return SourceResponse(source=found)


@owner.get("/digests")
async def list_digests(ctx: Ctx, limit: int = _DEFAULT_DIGEST_LIMIT) -> DigestsResponse:
    digests = await ctx.digests.list_recent(limit=min(max(limit, 1), _MAX_DIGEST_LIMIT))

    return DigestsResponse(
        digests=tuple(
            DigestSummary(
                id=digest.id,
                date=digest.date,
                style=digest.style,
                subject=digest.subject,
                delivery_status=digest.delivery_status,
                delivery_error=digest.delivery_error,
                created_at=digest.created_at.isoformat(),
            )
            for digest in digests
        )
    )


@owner.get("/digests/{digest_id}", response_class=HTMLResponse, response_model=None)
async def preview_digest(digest_id: str, ctx: Ctx) -> HTMLResponse | JSONResponse:
    digest = await ctx.digests.find_by_id(digest_id)

    if is_error(digest):
        return _error(digest, _store_status(digest))

    return HTMLResponse(digest.html)


@owner.get("/threads")
async def list_threads(ctx: Ctx, pinned: bool = False) -> ThreadsResponse:
    if pinned:
        return ThreadsResponse(threads=tuple(await ctx.threads.list_pinned()))

    since = ctx.clock.now() - timedelta(days=settings.THREAD_INACTIVE_DAYS)

    return ThreadsResponse(threads=tuple(await ctx.threads.list_active(since=since)))


@owner.get("/preferences")
async def get_preferences(ctx: Ctx) -> PreferencesResponse:
    return PreferencesResponse(
        profile=await ctx.preferences.profile(),
        interests=await ctx.preferences.interests(),
    )


@owner.put("/preferences", response_model=PreferencesResponse)
async def put_preferences(
    payload: PreferencesPayload, ctx: Ctx
) -> PreferencesResponse | JSONResponse:
    if payload.profile is None and payload.interests is None:
        return _error("profile or interests is required", 400)

    now = ctx.clock.now()

    if payload.profile is not None:
        await ctx.preferences.save_profile(payload.profile, now=now)

    if payload.interests is not None:
        await ctx.preferences.save_interests(payload.interests, now=now)

    logger.info("Preferences updated.")

    return await get_preferences(ctx)


@owner.post("/runs", status_code=202, response_model=RunResponse)
async def start_run(
    body: RunRequest, request: Request, ctx: Ctx
) -> RunResponse | JSONResponse:
    state = request.app.state
    running: asyncio.Task[run.RunOutcome] | None = state.run_task

    if running is not None and not running.done():
        return _error("a run is already in progress", 409)

    run_id = run.new_run_id()
    task = asyncio.create_task(
        run.run_pipeline(ctx, force=body.force, dry_run=body.dry_run, run_id=run_id),
        name=f"run-{run_id}",
    )
    task.add_done_callback(_log_run_result)
    state.run_task = task
    logger.info("Manual run started.", extra={"run_id": run_id})

    return RunResponse(run_id=run_id)


router.include_router(owner)
