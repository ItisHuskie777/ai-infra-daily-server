import argparse
import asyncio
import sys
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from app import context
from app import logging
from app import settings
from app.api import app as api_app
from app.context import AbstractContext
from app.models import SOURCE_WEIGHT_MAX
from app.models import SOURCE_WEIGHT_MIN
from app.models import DigestStyle
from app.models import Interests
from app.models import TechnicalDepth
from app.pipeline import cluster
from app.pipeline import embed
from app.pipeline import feedback
from app.pipeline import ingest
from app.pipeline import run
from app.pipeline import threads
from app.result import is_error
from app.sources import fetch_all
from app.sources import registry
from app.sources import seeds

type Command = Callable[[AbstractContext, argparse.Namespace], Awaitable[int]]

logger = logging.get_logger(__name__)

_DIGEST_LIST_LIMIT = 20
_PREVIEW_DIRECTORY = Path("data")


async def _migrate(ctx: AbstractContext, args: argparse.Namespace) -> int:
    applied = await ctx.database.migrate(settings.MIGRATIONS_DIRECTORY)

    if is_error(applied):
        print(f"migration failed: {applied}")

        return 1

    print(f"applied {len(applied)} migration(s)")

    for name in applied:
        print(f"  {name}")

    return 0


async def _seed(ctx: AbstractContext, args: argparse.Namespace) -> int:
    inserted = await seeds.seed_default_sources(ctx)
    print(f"seeded {inserted} new source(s)")

    return 0


async def _write_preview(html: str, date: str) -> Path:
    path = _PREVIEW_DIRECTORY / f"digest-{date}.html"

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    await asyncio.to_thread(write)

    return path


async def _run(ctx: AbstractContext, args: argparse.Namespace) -> int:
    outcome = await run.run_pipeline(
        ctx, adapters=registry.ADAPTERS, force=args.force, dry_run=args.dry_run
    )

    if outcome.preview_html is not None:
        path = await _write_preview(outcome.preview_html, f"{ctx.clock.now():%Y-%m-%d}")
        print(f"dry run: preview written to {path}")

        return 0

    print(f"{outcome.reason} (digest {outcome.digest_id or '-'})")

    return 1 if outcome.failed else 0


async def _stage(ctx: AbstractContext, args: argparse.Namespace) -> int:
    match args.name:
        case "fetch":
            sources = await ctx.sources.list_enabled()
            fetched = await fetch_all.fetch_all(
                ctx, sources, adapters=registry.ADAPTERS
            )
            new_ids = await ingest.store_new_items(ctx, fetched.items)
            print(f"fetched {len(fetched.items)} item(s), {len(new_ids)} new")

            for health in fetched.health:
                status = "ok" if health.ok else f"error: {health.error}"
                print(f"  {health.source_id}: {health.item_count} {status}")
        case "embed":
            print(f"embedded {await embed.embed_pending_items(ctx)} item(s)")
        case "cluster":
            outcome = await cluster.cluster_recent_items(ctx)
            print(
                f"created {len(outcome.created)} event(s), "
                f"updated {len(outcome.updated)}"
            )
        case "threads":
            # NOTE: The same events a run links: every event updated in the window.
            since = ctx.clock.now() - timedelta(hours=settings.EVENT_WINDOW_HOURS)
            events = await ctx.events.list_updated_since(since)
            ids = [event.id for event in events]
            print(f"linked {await threads.link_events_to_threads(ctx, ids)} event(s)")
        case _:
            print(f"unknown stage: {args.name}")

            return 1

    return 0


async def _sources(ctx: AbstractContext, args: argparse.Namespace) -> int:
    now = ctx.clock.now()

    match args.action:
        case "list":
            for source in await ctx.sources.list_all():
                flag = "on " if source.enabled else "off"
                print(
                    f"{flag} {source.weight:.2f} {source.id:<28} "
                    f"{source.kind:<12} {source.name}"
                )

            return 0
        case "enable" | "disable":
            result = await ctx.sources.set_enabled(
                args.id, enabled=args.action == "enable", now=now
            )
        case "weight" if not SOURCE_WEIGHT_MIN <= args.value <= SOURCE_WEIGHT_MAX:
            print(f"weight must be between {SOURCE_WEIGHT_MIN} and {SOURCE_WEIGHT_MAX}")

            return 1
        case "weight":
            result = await ctx.sources.set_weight(args.id, weight=args.value, now=now)
        case _:
            print(f"unknown action: {args.action}")

            return 1

    if is_error(result):
        print(f"source {args.id}: {result}")

        return 1

    print(f"source {args.id} updated")

    return 0


def _with(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    return values if value in values else (*values, value)


def _without(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    return tuple(entry for entry in values if entry != value)


def _edit_interests(interests: Interests, args: argparse.Namespace) -> Interests:
    value: str = args.value
    update: dict[str, object]

    match args.action:
        case "set-depth":
            update = {"technical_depth": TechnicalDepth(value)}
        case "add-topic":
            update = {"topics": _with(interests.topics, value)}
        case "remove-topic":
            update = {"topics": _without(interests.topics, value)}
        case "add-keyword" if args.exclude:
            update = {"keywords_exclude": _with(interests.keywords_exclude, value)}
        case "add-keyword":
            update = {"keywords_include": _with(interests.keywords_include, value)}
        case "remove-keyword":
            update = {
                "keywords_include": _without(interests.keywords_include, value),
                "keywords_exclude": _without(interests.keywords_exclude, value),
            }
        case "follow":
            update = {"followed_authors": _with(interests.followed_authors, value)}
        case "block":
            update = {"blocked_sources": _with(interests.blocked_sources, value)}
        case "unblock":
            update = {"blocked_sources": _without(interests.blocked_sources, value)}
        case _:
            update = {}

    return interests.model_copy(update=update)


async def _prefs(ctx: AbstractContext, args: argparse.Namespace) -> int:
    now = ctx.clock.now()
    profile = await ctx.preferences.profile()
    interests = await ctx.preferences.interests()

    match args.action:
        case "show":
            print(profile.model_dump_json(indent=2))
            print(interests.model_dump_json(indent=2))
        case "set-email":
            profile = profile.model_copy(update={"email": args.value})
        case "set-hour":
            profile = profile.model_copy(update={"delivery_hour": args.value})
        case "set-style":
            profile = profile.model_copy(
                update={"digest_style": DigestStyle(args.value)}
            )
        case _:
            interests = _edit_interests(interests, args)

    # NOTE: Only what changed is written, so a feedback click at the same moment
    # survives.
    if profile != await ctx.preferences.profile():
        await ctx.preferences.save_profile(profile, now=now)

    if interests != await ctx.preferences.interests():
        await ctx.preferences.save_interests(interests, now=now)

    if args.action != "show":
        print(f"preferences updated ({args.action})")

    return 0


async def _digests(ctx: AbstractContext, args: argparse.Namespace) -> int:
    for digest in await ctx.digests.list_recent(limit=_DIGEST_LIST_LIMIT):
        print(f"{digest.date} {digest.delivery_status:<8} {digest.id} {digest.subject}")

    return 0


async def _set_pinned(ctx: AbstractContext, thread_id: str, *, pinned: bool) -> int:
    found = await ctx.threads.find_by_id(thread_id)

    if is_error(found):
        print(f"no thread {thread_id}")

        return 1

    query = feedback.pinned_query_for(found.title) if pinned else None
    await ctx.threads.set_pinned(thread_id, pinned=pinned, query=query)
    print(f"thread {thread_id} {'pinned' if pinned else 'unpinned'}")

    return 0


async def _threads(ctx: AbstractContext, args: argparse.Namespace) -> int:
    if args.action in {"pin", "unpin"}:
        return await _set_pinned(ctx, args.id, pinned=args.action == "pin")

    since = threads.inactive_since(ctx.clock.now())

    for thread in await ctx.threads.list_active(since=since):
        pin = "pinned" if thread.pinned else "      "
        print(
            f"{pin} {thread.day_count:>2}d {thread.last_seen_at:%Y-%m-%d} "
            f"{thread.id} {thread.title}"
        )

    return 0


async def _maintain(ctx: AbstractContext, args: argparse.Namespace) -> int:
    """Daily housekeeping, safe to run every hour: prunes, then backs up once a day."""
    now = ctx.clock.now()
    retention = timedelta(days=settings.RETENTION_DAYS)
    pruned_items = await ctx.items.prune_before(now - retention)
    pruned_runs = await ctx.runs.prune_before(now - 2 * retention)
    backup = settings.BACKUP_DIRECTORY / f"aid-{now:%Y-%m-%d}.sqlite3"

    if not await asyncio.to_thread(backup.exists):
        await ctx.database.backup(backup)
        removed = await asyncio.to_thread(_remove_old_backups)
        print(f"backed up to {backup}; removed {removed} old backup(s)")

    print(f"pruned {pruned_items} item(s) and {pruned_runs} run record(s)")

    return 0


def _remove_old_backups() -> int:
    backups = sorted(settings.BACKUP_DIRECTORY.glob("aid-*.sqlite3"))
    old = backups[: max(0, len(backups) - settings.BACKUPS_KEPT)]

    for path in old:
        path.unlink()

    return len(old)


async def _execute(command: Command, args: argparse.Namespace) -> int:
    ctx = context.default()

    try:
        return await command(ctx, args)
    finally:
        await ctx.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aid", description="AI Infra Daily")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply pending database migrations")
    commands.add_parser("seed", help="insert the default sources that are missing")
    run_parser = commands.add_parser("run", help="run the whole digest pipeline")
    run_parser.add_argument("--force", action="store_true", help="ignore the hour")
    run_parser.add_argument(
        "--dry-run", action="store_true", help="write the HTML to data/ instead"
    )
    stage = commands.add_parser("stage", help="run one pipeline stage")
    stage.add_argument("name", choices=["fetch", "embed", "cluster", "threads"])
    sources = commands.add_parser("sources", help="list or edit sources")
    source_actions = sources.add_subparsers(dest="action", required=True)
    source_actions.add_parser("list")

    for action in ("enable", "disable"):
        source_actions.add_parser(action).add_argument("id")

    weight = source_actions.add_parser("weight")
    weight.add_argument("id")
    weight.add_argument("value", type=float)
    prefs = commands.add_parser("prefs", help="show or edit owner preferences")
    pref_actions = prefs.add_subparsers(dest="action", required=True)
    pref_actions.add_parser("show")
    pref_actions.add_parser("set-email").add_argument("value")
    pref_actions.add_parser("set-hour").add_argument("value", type=int)
    pref_actions.add_parser("set-style").add_argument(
        "value", choices=[style.value for style in DigestStyle]
    )
    pref_actions.add_parser("set-depth").add_argument(
        "value", choices=[depth.value for depth in TechnicalDepth]
    )

    for action in (
        "add-topic",
        "remove-topic",
        "remove-keyword",
        "follow",
        "block",
        "unblock",
    ):
        pref_actions.add_parser(action).add_argument("value")

    keyword = pref_actions.add_parser("add-keyword")
    keyword.add_argument("value")
    keyword.add_argument("--exclude", action="store_true")
    digests = commands.add_parser("digests", help="list recent digests")
    digests.add_subparsers(dest="action", required=True).add_parser("list")
    threads_parser = commands.add_parser("threads", help="list or pin threads")
    thread_actions = threads_parser.add_subparsers(dest="action", required=True)
    thread_actions.add_parser("list")

    for action in ("pin", "unpin"):
        thread_actions.add_parser(action).add_argument("id")

    commands.add_parser("maintain", help="prune old data and back up the database")
    commands.add_parser("serve", help="serve the HTTP API")

    return parser


_COMMANDS: dict[str, Command] = {
    "migrate": _migrate,
    "seed": _seed,
    "run": _run,
    "stage": _stage,
    "sources": _sources,
    "prefs": _prefs,
    "digests": _digests,
    "threads": _threads,
    "maintain": _maintain,
}


def main() -> None:
    args = _build_parser().parse_args()
    logging.configure(level=settings.LOG_LEVEL)

    if args.command == "serve":
        api_app.serve(host=settings.API_HOST, port=settings.API_PORT)

        return

    command = _COMMANDS.get(args.command)

    if command is None:
        print(f"unknown command: {args.command}")
        sys.exit(1)

    code = asyncio.run(_execute(command, args))

    if code != 0:
        sys.exit(code)
