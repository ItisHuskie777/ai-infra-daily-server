import asyncio
import uuid
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from pathlib import Path

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
from app.result import is_error
from app.sources import fetch_all
from app.sources import registry
from app.sources import seeds

logger = logging.get_logger(__name__)

_DRY_RUN_DIRECTORY = Path("data")


class RunOutcome(Model):
    digest_id: str | None
    reason: str
    failed: bool = False


class _Finished(Model):
    """What the run row records; `status` is the operator-facing outcome word."""

    status: str
    outcome: RunOutcome


def _skipped(reason: str) -> _Finished:
    return _Finished(
        status="skipped", outcome=RunOutcome(digest_id=None, reason=reason)
    )


async def _ingest(ctx: AbstractContext, run_id: str) -> tuple[SourceHealth, ...]:
    await ctx.runs.set_stage(run_id, "seed")
    seeded = await seeds.seed_default_sources(ctx)
    await ctx.runs.set_stage(run_id, "fetch")
    sources = await ctx.sources.list_enabled()
    fetched = await fetch_all.fetch_all(ctx, sources, adapters=registry.ADAPTERS)
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
) -> list[DigestEventPlacement] | str:
    await ctx.runs.set_stage(run_id, "rank")
    since = now - timedelta(hours=settings.EVENT_WINDOW_HOURS)
    events = await ctx.events.list_updated_since(since)
    sources = await ctx.sources.list_all()
    ranked = await rank.rank_events(ctx, interests, sources, events)

    if is_error(ranked):
        logger.error("Ranking failed.", extra={"error": str(ranked)})

        return f"ranking failed: {ranked}"

    await ctx.runs.set_stage(run_id, "select")
    item_ids = [item.id for entry in ranked for item in entry.items]
    already_sent = await ctx.items.filter_already_sent(item_ids)
    pinned = await ctx.threads.list_pinned()

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

    for placement in placements:
        summary = await summarise.summarise_event(
            ctx,
            placement,
            style=profile.digest_style,
            depth=interests.technical_depth,
        )

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
    token = feedback_token.sign(
        settings.FEEDBACK_SECRET,
        digest_id=digest_id,
        expires_at=now + timedelta(days=settings.FEEDBACK_TOKEN_DAYS),
    )
    composed = compose.compose_digest_html(
        date=date,
        style=profile.digest_style,
        sections=sections,
        health=health,
        feedback_base_url=settings.PUBLIC_BASE_URL,
        feedback_token=token,
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


async def _write_preview(digest: Digest) -> Path:
    path = _DRY_RUN_DIRECTORY / f"digest-{digest.date}.html"

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(digest.html, encoding="utf-8")

    await asyncio.to_thread(write)

    return path


async def _deliver(
    ctx: AbstractContext,
    digest: Digest,
    *,
    now: datetime,
    profile: Profile,
    dry_run: bool,
) -> _Finished:
    if dry_run:
        path = await _write_preview(digest)
        logger.info("Dry run: digest written to disk.", extra={"path": str(path)})

        return _Finished(
            status="dry_run",
            outcome=RunOutcome(digest_id=digest.id, reason=f"dry run: {path}"),
        )

    sent = await ctx.email.send(
        to=profile.email, subject=digest.subject, html=digest.html
    )

    if is_error(sent):
        logger.error("Digest delivery failed.", extra={"error": str(sent)})
        await ctx.digests.set_delivery_status(
            digest.id, DeliveryStatus.FAILED, str(sent)
        )

        return _Finished(
            status="failed",
            outcome=RunOutcome(
                digest_id=digest.id, reason=f"delivery failed: {sent}", failed=True
            ),
        )

    await ctx.digests.set_delivery_status(digest.id, DeliveryStatus.SENT, None)
    await ctx.preferences.save_profile(
        profile.model_copy(update={"last_digest_date": digest.date}), now=now
    )
    logger.info("Digest sent.", extra={"digest_id": digest.id, "message_id": sent})

    return _Finished(
        status="sent", outcome=RunOutcome(digest_id=digest.id, reason="sent")
    )


async def _stages(
    ctx: AbstractContext,
    run_id: str,
    *,
    now: datetime,
    date: str,
    profile: Profile,
    interests: Interests,
    dry_run: bool,
) -> _Finished:
    health = await _ingest(ctx, run_id)
    await _group_events(ctx, run_id)
    placements = await _rank_and_select(
        ctx, run_id, now=now, profile=profile, interests=interests
    )

    if isinstance(placements, str):
        return _Finished(
            status="failed",
            outcome=RunOutcome(digest_id=None, reason=placements, failed=True),
        )

    if len(placements) == 0:
        return _skipped("nothing new to send")

    await ctx.runs.set_stage(run_id, "summarise")
    sections = await _summarise_all(
        ctx, placements, profile=profile, interests=interests
    )

    if len(sections) == 0:
        return _Finished(
            status="failed",
            outcome=RunOutcome(
                digest_id=None, reason="every event summary failed", failed=True
            ),
        )

    await ctx.runs.set_stage(run_id, "compose")
    digest = _build_digest(
        digest_id=str(uuid.uuid4()),
        date=date,
        now=now,
        profile=profile,
        placements=placements,
        sections=sections,
        health=health,
    )
    await ctx.runs.set_stage(run_id, "persist")
    await ctx.digests.save(digest)
    await ctx.runs.set_stage(run_id, "deliver")

    return await _deliver(ctx, digest, now=now, profile=profile, dry_run=dry_run)


async def _run(
    ctx: AbstractContext, run_id: str, *, force: bool, dry_run: bool
) -> RunOutcome:
    now = ctx.clock.now()
    profile = await ctx.preferences.profile()
    interests = await ctx.preferences.interests()
    decision = schedule.should_send_now(profile, now)

    if not force and not decision.send:
        logger.info("Run skipped by schedule.", extra={"reason": decision.reason})

        return RunOutcome(digest_id=None, reason=decision.reason)

    if profile.email == "":
        logger.warning("Run refused: no delivery email configured.")

        return RunOutcome(digest_id=None, reason="no delivery email configured")

    await ctx.runs.start(run_id, now=now)

    try:
        finished = await _stages(
            ctx,
            run_id,
            now=now,
            date=decision.local_date,
            profile=profile,
            interests=interests,
            dry_run=dry_run,
        )
    except asyncio.CancelledError:
        logger.warning("Pipeline run cancelled.")
        await ctx.runs.finish(
            run_id,
            now=ctx.clock.now(),
            outcome="cancelled",
            detail=None,
            digest_id=None,
        )

        raise
    except Exception as error:
        logger.exception("Pipeline run crashed.")
        await ctx.runs.finish(
            run_id,
            now=ctx.clock.now(),
            outcome="crashed",
            detail=repr(error),
            digest_id=None,
        )

        raise

    await ctx.runs.finish(
        run_id,
        now=ctx.clock.now(),
        outcome=finished.status,
        detail=finished.outcome.reason,
        digest_id=finished.outcome.digest_id,
    )
    logger.info(
        "Pipeline run finished.",
        extra={"outcome": finished.status, "reason": finished.outcome.reason},
    )

    return finished.outcome


def new_run_id() -> str:
    return str(uuid.uuid4())


async def run_pipeline(
    ctx: AbstractContext,
    *,
    force: bool,
    dry_run: bool,
    run_id: str | None = None,
) -> RunOutcome:
    """Fetches, groups, ranks and sends one digest; `force` ignores the delivery
    hour and `dry_run` writes the HTML to `data/` instead of emailing it."""
    if run_id is None:
        run_id = new_run_id()

    with logging.scoped(run_id=run_id):
        return await _run(ctx, run_id, force=force, dry_run=dry_run)
