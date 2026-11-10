import asyncio
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from typing import override

from app import logging
from app import settings
from app.context import AbstractContext
from app.models import DeliveryStatus
from app.models import Digest
from app.models import DigestEventPlacement
from app.models import DigestSection
from app.models import Interests
from app.models import Model
from app.models import Profile
from app.models import RunStatus
from app.models import SourceHealth
from app.pipeline import cluster
from app.pipeline import compose
from app.pipeline import embed
from app.pipeline import feedback_token
from app.pipeline import ingest
from app.pipeline import quiz
from app.pipeline import rank
from app.pipeline import schedule
from app.pipeline import select
from app.pipeline import summarise
from app.pipeline import threads
from app.pipeline.compose import ComposedEvent
from app.pipeline.compose import ComposedSection
from app.pipeline.summarise import EventSummary
from app.result import OnSuccess
from app.result import ServiceError
from app.result import is_error
from app.sources import fetch_all
from app.sources import seeds
from app.sources._common import ImplementsSourceAdapter

logger = logging.get_logger(__name__)


type Adapters = Mapping[str, ImplementsSourceAdapter]


class RunError(ServiceError):
    RANKING_FAILED = "ranking_failed"
    ALL_SUMMARIES_FAILED = "all_summaries_failed"

    @override
    def service(self) -> str:
        return "run"


class RunOutcome(Model):
    status: RunStatus
    digest_id: str | None
    # Operator-facing detail: the skip reason or the error behind a failure.
    reason: str
    # The rendered digest of a dry run; the caller decides where it goes.
    preview_html: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in {RunStatus.FAILED, RunStatus.CRASHED}


def _skipped(reason: str) -> RunOutcome:
    return RunOutcome(status=RunStatus.SKIPPED, digest_id=None, reason=reason)


def _failed(error: ServiceError, digest_id: str | None = None) -> RunOutcome:
    return RunOutcome(status=RunStatus.FAILED, digest_id=digest_id, reason=str(error))


async def _ingest(
    ctx: AbstractContext, run_id: str, adapters: Adapters
) -> tuple[SourceHealth, ...]:
    await ctx.runs.set_stage(run_id, "seed")
    seeded = await seeds.seed_default_sources(ctx)
    await ctx.runs.set_stage(run_id, "fetch")
    sources = await ctx.sources.list_enabled()
    fetched = await fetch_all.fetch_all(ctx, sources, adapters=adapters)
    new_ids = await ingest.store_new_items(ctx, fetched.items)
    logger.info(
        "Sources fetched.",
        extra={
            "seeded": seeded,
            "sources": len(sources),
            "items": len(fetched.items),
            "new_items": len(new_ids),
        },
    )

    return fetched.health


async def _group_events(ctx: AbstractContext, run_id: str) -> None:
    await ctx.runs.set_stage(run_id, "embed")
    embedded = await embed.embed_pending_items(ctx)
    await ctx.runs.set_stage(run_id, "cluster")
    clustered = await cluster.cluster_recent_items(ctx)
    await ctx.runs.set_stage(run_id, "threads")
    linked = await threads.link_events_to_threads(
        ctx, [*clustered.created, *clustered.updated]
    )
    logger.info(
        "Events grouped.",
        extra={
            "embedded": embedded,
            "events_created": len(clustered.created),
            "events_updated": len(clustered.updated),
            "events_linked": linked,
        },
    )


async def _rank_and_select(
    ctx: AbstractContext,
    run_id: str,
    *,
    now: datetime,
    profile: Profile,
    interests: Interests,
) -> OnSuccess[list[DigestEventPlacement]]:
    await ctx.runs.set_stage(run_id, "rank")
    since = now - timedelta(hours=settings.EVENT_WINDOW_HOURS)
    events = await ctx.events.list_updated_since(since)
    item_ids = [item.id for event in events for item in event.items]
    already_sent = await ctx.items.filter_already_sent(item_ids)
    sources = await ctx.sources.list_all()
    ranked = await rank.rank_events(
        ctx, interests, sources, select.fresh_events(events, already_sent)
    )

    if is_error(ranked):
        logger.error("Ranking failed.", extra={"error": str(ranked)})

        return RunError.RANKING_FAILED

    await ctx.runs.set_stage(run_id, "select")
    pinned = await ctx.threads.list_pinned(since=threads.inactive_since(now))

    return select.select_for_digest(
        ranked,
        style=profile.digest_style,
        already_sent_item_ids=already_sent,
        pinned_thread_ids={thread.id for thread in pinned},
    )


async def _summarise_all(
    ctx: AbstractContext,
    placements: Sequence[DigestEventPlacement],
    *,
    profile: Profile,
    interests: Interests,
) -> list[ComposedSection]:
    buckets: dict[DigestSection, list[ComposedEvent]] = {}
    semaphore = asyncio.Semaphore(settings.SUMMARY_CONCURRENCY)

    async def summarised(placement: DigestEventPlacement) -> OnSuccess[EventSummary]:
        async with semaphore:
            return await summarise.summarise_event(
                ctx,
                placement,
                style=profile.digest_style,
                depth=interests.technical_depth,
            )

    summaries = await asyncio.gather(*(summarised(entry) for entry in placements))

    for placement, summary in zip(placements, summaries, strict=True):
        if is_error(summary):
            logger.warning(
                "Event summary failed; event skipped.",
                extra={"event_id": placement.event.id, "error": str(summary)},
            )

            continue

        buckets.setdefault(placement.section, []).append(
            ComposedEvent(
                placement=placement, summary=summary, thread=placement.event.thread
            )
        )

    return [
        ComposedSection(section=section, events=tuple(buckets[section]))
        for section in DigestSection
        if section in buckets
    ]


def _build_digest(
    *,
    digest_id: str,
    date: str,
    now: datetime,
    profile: Profile,
    placements: Sequence[DigestEventPlacement],
    sections: Sequence[ComposedSection],
    health: Sequence[SourceHealth],
) -> Digest:
    expires_at = now + timedelta(days=settings.FEEDBACK_TOKEN_DAYS)

    def sign_link(scope: str) -> str:
        return feedback_token.sign(
            settings.FEEDBACK_SECRET,
            digest_id=digest_id,
            expires_at=expires_at,
            scope=scope,
        )

    composed = compose.compose_digest_html(
        date=date,
        style=profile.digest_style,
        sections=sections,
        health=health,
        feedback_base_url=settings.PUBLIC_BASE_URL,
        sign_link=sign_link,
        quizzes=quiz.build_quizzes(placements, date=date),
    )

    return Digest(
        id=digest_id,
        date=date,
        style=profile.digest_style,
        subject=composed.subject,
        html=composed.html,
        placements=tuple(placements),
        health=tuple(health),
        delivery_status=DeliveryStatus.PENDING,
        delivery_error=None,
        created_at=now,
    )


async def _deliver(
    ctx: AbstractContext,
    digest: Digest,
    *,
    now: datetime,
    profile: Profile,
    dry_run: bool,
) -> RunOutcome:
    if dry_run:
        logger.info("Dry run: digest built, not sent.", extra={"digest_id": digest.id})

        return RunOutcome(
            status=RunStatus.DRY_RUN,
            digest_id=None,
            reason="dry run",
            preview_html=digest.html,
        )

    sent = await ctx.email.send(
        to=profile.email,
        subject=digest.subject,
        html=digest.html,
        idempotency_key=digest.id,
    )

    if is_error(sent):
        logger.error("Digest delivery failed.", extra={"error": str(sent)})
        await ctx.digests.set_delivery_status(
            digest.id, DeliveryStatus.FAILED, str(sent)
        )

        return _failed(sent, digest.id)

    await ctx.digests.set_delivery_status(digest.id, DeliveryStatus.SENT, None)
    await ctx.preferences.set_last_digest_date(digest.date, now=now)
    logger.info("Digest sent.", extra={"digest_id": digest.id, "message_id": sent})

    return RunOutcome(status=RunStatus.SENT, digest_id=digest.id, reason="sent")


async def _stages(
    ctx: AbstractContext,
    run_id: str,
    *,
    now: datetime,
    date: str,
    profile: Profile,
    interests: Interests,
    dry_run: bool,
    adapters: Adapters,
) -> RunOutcome:
    health = await _ingest(ctx, run_id, adapters)
    await _group_events(ctx, run_id)
    placements = await _rank_and_select(
        ctx, run_id, now=now, profile=profile, interests=interests
    )

    if is_error(placements):
        return _failed(placements)

    if len(placements) == 0:
        return _skipped("nothing new to send")

    await ctx.runs.set_stage(run_id, "summarise")
    sections = await _summarise_all(
        ctx, placements, profile=profile, interests=interests
    )

    if len(sections) == 0:
        return _failed(RunError.ALL_SUMMARIES_FAILED)

    await ctx.runs.set_stage(run_id, "compose")
    # NOTE: Only summarised events are recorded, so an event whose summary failed is
    # not counted as sent and can appear tomorrow.
    digest = _build_digest(
        digest_id=ctx.ids.new(),
        date=date,
        now=now,
        profile=profile,
        placements=[
            event.placement for section in sections for event in section.events
        ],
        sections=sections,
        health=health,
    )
    # NOTE: A dry run leaves no digest behind, so previews never mix with real mail.
    if not dry_run:
        await ctx.runs.set_stage(run_id, "persist")
        await ctx.digests.save(digest)
    await ctx.runs.set_stage(run_id, "deliver")

    return await _deliver(ctx, digest, now=now, profile=profile, dry_run=dry_run)


async def _resend_failed(
    ctx: AbstractContext, *, date: str, now: datetime, profile: Profile
) -> RunOutcome | None:
    """A digest built today whose delivery failed is sent again as it is, without
    spending another run of the model on it."""
    existing = await ctx.digests.find_by_date(date)

    if is_error(existing) or existing.delivery_status != DeliveryStatus.FAILED:
        return None

    logger.info("Resending today's undelivered digest.", extra={"digest": existing.id})

    return await _deliver(ctx, existing, now=now, profile=profile, dry_run=False)


async def _decide(
    ctx: AbstractContext, *, now: datetime, profile: Profile, force: bool
) -> schedule.ScheduleDecision | RunOutcome:
    decision = schedule.should_send_now(
        profile, now, last_sent_date=await ctx.digests.latest_sent_date()
    )

    if not force and not decision.send:
        logger.info("Run skipped by schedule.", extra={"reason": decision.reason})

        return _skipped(decision.reason)

    if profile.email == "":
        logger.warning("Run refused: no delivery email configured.")

        return _skipped("no delivery email configured")

    day_start = schedule.start_of_local_day(profile.timezone, now)
    failures = await ctx.runs.count_failed_since(day_start)

    if not force and failures >= settings.MAX_DAILY_DELIVERY_ATTEMPTS:
        logger.warning(
            "Run skipped: today's delivery attempts are used up.",
            extra={"failures": failures},
        )

        return _skipped("delivery attempts used up today")

    return decision


async def _attempt(
    ctx: AbstractContext,
    run_id: str,
    *,
    now: datetime,
    decision: schedule.ScheduleDecision,
    profile: Profile,
    dry_run: bool,
    adapters: Adapters,
) -> RunOutcome:
    if not dry_run:
        resent = await _resend_failed(
            ctx, date=decision.local_date, now=now, profile=profile
        )

        if resent is not None:
            return resent

    return await _stages(
        ctx,
        run_id,
        now=now,
        date=decision.local_date,
        profile=profile,
        interests=await ctx.preferences.interests(),
        dry_run=dry_run,
        adapters=adapters,
    )


async def _run_holding_lease(
    ctx: AbstractContext,
    run_id: str,
    *,
    force: bool,
    dry_run: bool,
    adapters: Adapters,
) -> RunOutcome:
    now = ctx.clock.now()
    await ctx.runs.abandon_unfinished(except_id=run_id, now=now)
    profile = await ctx.preferences.profile()
    decision = await _decide(ctx, now=now, profile=profile, force=force)

    if isinstance(decision, RunOutcome):
        return decision

    await ctx.runs.start(run_id, now=now)

    try:
        finished = await _attempt(
            ctx,
            run_id,
            now=now,
            decision=decision,
            profile=profile,
            dry_run=dry_run,
            adapters=adapters,
        )
    except asyncio.CancelledError:
        logger.warning("Pipeline run cancelled.")
        await ctx.runs.finish(
            run_id,
            now=ctx.clock.now(),
            outcome=RunStatus.CANCELLED,
            detail=None,
            digest_id=None,
        )

        raise
    except Exception as error:
        logger.exception("Pipeline run crashed.")
        await ctx.runs.finish(
            run_id,
            now=ctx.clock.now(),
            outcome=RunStatus.CRASHED,
            detail=repr(error),
            digest_id=None,
        )

        raise

    await ctx.runs.finish(
        run_id,
        now=ctx.clock.now(),
        outcome=finished.status,
        detail=finished.reason,
        digest_id=finished.digest_id,
    )
    logger.info(
        "Pipeline run finished.",
        extra={"outcome": finished.status, "reason": finished.reason},
    )

    return finished


async def _run(
    ctx: AbstractContext,
    run_id: str,
    *,
    force: bool,
    dry_run: bool,
    adapters: Adapters,
) -> RunOutcome:
    """Every run holds the pipeline lease, so the scheduler, the API and a one-off
    command never build or send the same day's digest twice."""
    acquired = await ctx.runs.acquire_lease(
        run_id,
        now=ctx.clock.now(),
        duration=timedelta(minutes=settings.RUN_LEASE_MINUTES),
    )

    if not acquired:
        logger.warning("Run skipped: another run holds the lease.")

        return _skipped("another run is in progress")

    try:
        return await _run_holding_lease(
            ctx, run_id, force=force, dry_run=dry_run, adapters=adapters
        )
    finally:
        await ctx.runs.release_lease(run_id)


async def run_pipeline(
    ctx: AbstractContext,
    *,
    adapters: Adapters,
    force: bool,
    dry_run: bool,
    run_id: str | None = None,
) -> RunOutcome:
    """Fetches, groups, ranks and sends one digest; `force` ignores the schedule and
    `dry_run` returns the HTML instead of storing and emailing it. `adapters` maps each
    source kind to the adapter that fetches it."""
    run_id = run_id or ctx.ids.new()

    with logging.scoped(run_id=run_id):
        return await _run(ctx, run_id, force=force, dry_run=dry_run, adapters=adapters)
