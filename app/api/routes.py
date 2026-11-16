import asyncio
from html import escape
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from pydantic import Field

from app import logging
from app import settings
from app.api import auth
from app.context import AbstractContext
from app.models import SOURCE_WEIGHT_MAX
from app.models import SOURCE_WEIGHT_MIN
from app.models import DeliveryStatus
from app.models import DigestStyle
from app.models import FeedbackSignal
from app.models import Interests
from app.models import Model
from app.models import Profile
from app.models import RunStatus
from app.models import Source
from app.models import Thread
from app.pipeline import links
from app.pipeline import preview
from app.pipeline import run
from app.pipeline import schedule
from app.pipeline import threads
from app.pipeline.links import LinkError
from app.repositories import RunRecord
from app.repositories import StoreError
from app.result import OnSuccess
from app.result import ServiceError
from app.result import is_error
from app.sources import registry

logger = logging.get_logger(__name__)

_DEFAULT_DIGEST_LIMIT = 10
_MAX_DIGEST_LIMIT = 50
_DEFAULT_RUN_LIMIT = 10
_MAX_RUN_LIMIT = 100

router = APIRouter()
owner = APIRouter(prefix="/api", dependencies=[Depends(auth.require_owner)])


class SourcePatch(Model):
    enabled: bool | None = None
    weight: float | None = Field(
        default=None, ge=SOURCE_WEIGHT_MIN, le=SOURCE_WEIGHT_MAX
    )


class ThreadPatch(Model):
    pinned: bool


class ThreadResponse(Model):
    thread: Thread


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


class RunSummary(Model):
    """The public part of a run record; the detail may quote an error and stays behind
    the owner token."""

    started_at: str
    finished_at: str | None
    stage: str
    outcome: RunStatus | None


class HealthResponse(Model):
    ok: bool
    service: str
    last_sent_date: str | None
    last_run: RunSummary | None


class RunsResponse(Model):
    runs: tuple[RunRecord, ...]


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


async def _manual_run(
    ctx: AbstractContext, run_id: str, *, force: bool, dry_run: bool
) -> run.RunOutcome:
    try:
        outcome = await run.run_pipeline(
            ctx,
            adapters=registry.ADAPTERS,
            force=force,
            dry_run=dry_run,
            run_id=run_id,
        )

        if outcome.preview_html is not None:
            path = await preview.write(
                outcome.preview_html, f"{ctx.clock.now():%Y-%m-%d}"
            )
            logger.info("Dry run preview written.", extra={"path": str(path)})

        return outcome
    finally:
        # NOTE: The API process lives for weeks; without this one manual run would
        # keep the embedding model and the browser in memory until the next restart.
        await ctx.embeddings.close()

        if ctx.browser is not None:
            await ctx.browser.close()


@router.get("/api/health")
async def health(ctx: Ctx) -> HealthResponse:
    """Answers from the database, so it also says whether the pipeline is alive: the
    last run attempted and the last day a digest went out."""
    runs = await ctx.runs.list_recent(limit=1)
    last = runs[0] if runs else None

    return HealthResponse(
        ok=True,
        service="ai-infra-daily",
        last_sent_date=await ctx.digests.latest_sent_date(),
        last_run=None
        if last is None
        else RunSummary(
            started_at=last.started_at.isoformat(),
            finished_at=None
            if last.finished_at is None
            else last.finished_at.isoformat(),
            stage=last.stage,
            outcome=last.outcome,
        ),
    )


_STYLE = (
    "body{font-family:Georgia,serif;max-width:32rem;margin:4rem auto;padding:0 1rem;"
    "color:#1f2933}button{font:inherit;padding:.4rem 1.2rem;cursor:pointer}"
)
_SIGNAL_LABELS = {
    FeedbackSignal.MORE_LIKE_THIS.value: "Show more stories like this one.",
    FeedbackSignal.LESS_LIKE_THIS.value: "Show fewer stories like this one.",
    FeedbackSignal.MUTE_SOURCE.value: "Stop using the source of this story.",
    FeedbackSignal.PIN_THREAD.value: "Keep following this story every day.",
}
_LINK_PAGES: dict[LinkError, tuple[str, str, int]] = {
    LinkError.INVALID: ("Invalid link", "This link is not valid.", 400),
    LinkError.EXPIRED: ("Link expired", "This link has expired.", 400),
    LinkError.UNKNOWN_TARGET: (
        "Invalid link",
        "This link points at content that no longer exists.",
        400,
    ),
    LinkError.ALREADY_APPLIED: (
        "Already done",
        "This link was used before; nothing changed.",
        200,
    ),
    LinkError.FAILED: ("Something went wrong", "Nothing was changed.", 500),
}


def _confirm_page(
    action: str, description: str, fields: dict[str, str]
) -> HTMLResponse:
    """Email links only show this page; the change happens when the owner presses the
    button, so mail scanners that open every link change nothing."""
    hidden = "".join(
        f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'
        for name, value in fields.items()
    )
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex">'
        f"<title>Confirm</title><style>{_STYLE}</style></head><body>"
        f"<h1>Confirm</h1><p>{escape(description)}</p>"
        f'<form method="post" action="{escape(action)}">{hidden}'
        '<button type="submit">Confirm</button></form></body></html>'
    )

    return HTMLResponse(html)


def _link_result(result: OnSuccess[str]) -> HTMLResponse:
    if isinstance(result, LinkError):
        title, body, status = _LINK_PAGES[result]

        return _page(title, body, status=status)

    if is_error(result):
        title, body, status = _LINK_PAGES[LinkError.FAILED]

        return _page(title, body, status=status)

    return _page("Thanks, noted", result)


@router.get("/feedback", response_class=HTMLResponse)
async def confirm_feedback(
    *,
    token: str = "",
    signal: str = "",
    event: str = "",
    source: str = "",
    item: str = "",
) -> HTMLResponse:
    return _confirm_page(
        "/feedback",
        _SIGNAL_LABELS.get(signal, "Apply this feedback."),
        {
            "token": token,
            "signal": signal,
            "event": event,
            "source": source,
            "item": item,
        },
    )


@router.post("/feedback", response_class=HTMLResponse)
async def apply_feedback(
    *,
    ctx: Ctx,
    token: Annotated[str, Form()] = "",
    signal: Annotated[str, Form()] = "",
    event: Annotated[str, Form()] = "",
    source: Annotated[str, Form()] = "",
    item: Annotated[str, Form()] = "",
) -> HTMLResponse:
    return _link_result(
        await links.apply_feedback_link(
            ctx,
            secret=settings.FEEDBACK_SECRET,
            token=token,
            signal=signal,
            event_id=event or None,
            source_id=source or None,
            item_id=item or None,
        )
    )


@router.get("/quiz", response_class=HTMLResponse)
async def confirm_quiz(
    *, token: str = "", question: str = "", answer: str = ""
) -> HTMLResponse:
    return _confirm_page(
        "/quiz",
        "Save this answer for tomorrow's digest.",
        {"token": token, "question": question, "answer": answer},
    )


@router.post("/quiz", response_class=HTMLResponse)
async def apply_quiz(
    *,
    ctx: Ctx,
    token: Annotated[str, Form()] = "",
    question: Annotated[str, Form()] = "",
    answer: Annotated[str, Form()] = "",
) -> HTMLResponse:
    return _link_result(
        await links.apply_quiz_link(
            ctx,
            secret=settings.FEEDBACK_SECRET,
            token=token,
            question=question,
            answer=answer,
        )
    )


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
    since = threads.inactive_since(ctx.clock.now())

    if pinned:
        return ThreadsResponse(
            threads=tuple(await ctx.threads.list_pinned(since=since))
        )

    return ThreadsResponse(threads=tuple(await ctx.threads.list_active(since=since)))


@owner.patch("/threads/{thread_id}", response_model=ThreadResponse)
async def patch_thread(
    thread_id: str, patch: ThreadPatch, ctx: Ctx
) -> ThreadResponse | JSONResponse:
    updated = await threads.set_pinned(ctx, thread_id, pinned=patch.pinned)

    if is_error(updated):
        return _error(updated, _store_status(updated))

    return ThreadResponse(thread=updated)


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

    if payload.profile is not None and not schedule.is_known_timezone(
        payload.profile.timezone
    ):
        return _error("unknown timezone", 400)

    now = ctx.clock.now()

    if payload.profile is not None:
        await ctx.preferences.save_profile(payload.profile, now=now)

    if payload.interests is not None:
        await ctx.preferences.save_interests(payload.interests, now=now)

    logger.info("Preferences updated.")

    return await get_preferences(ctx)


@owner.get("/runs")
async def list_runs(ctx: Ctx, limit: int = _DEFAULT_RUN_LIMIT) -> RunsResponse:
    runs = await ctx.runs.list_recent(limit=min(max(limit, 1), _MAX_RUN_LIMIT))

    return RunsResponse(runs=tuple(runs))


@owner.post("/runs", status_code=202, response_model=RunResponse)
async def start_run(
    body: RunRequest, request: Request, ctx: Ctx
) -> RunResponse | JSONResponse:
    state = request.app.state
    running: asyncio.Task[run.RunOutcome] | None = state.run_task

    if running is not None and not running.done():
        return _error("a run is already in progress", 409)

    run_id = ctx.ids.new()
    task = asyncio.create_task(
        _manual_run(ctx, run_id, force=body.force, dry_run=body.dry_run),
        name=f"run-{run_id}",
    )
    task.add_done_callback(_log_run_result)
    state.run_task = task
    logger.info("Manual run started.", extra={"run_id": run_id})

    return RunResponse(run_id=run_id)


router.include_router(owner)
