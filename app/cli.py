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
from app.context import Context
from app.models import DigestStyle
from app.models import Interests
from app.models import TechnicalDepth
from app.pipeline import cluster
from app.pipeline import embed
from app.pipeline import ingest
from app.pipeline import run
from app.pipeline import threads
from app.result import is_error
from app.sources import fetch_all
from app.sources import registry
from app.sources import seeds

type Command = Callable[[Context, argparse.Namespace], Awaitable[int]]

logger = logging.get_logger(__name__)

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_DIGEST_LIST_LIMIT = 20


async def _migrate(ctx: Context, args: argparse.Namespace) -> int:
    applied = await ctx.database.migrate(_MIGRATIONS)
    print(f"applied {len(applied)} migration(s)")

    for name in applied:
        print(f"  {name}")

    return 0


async def _seed(ctx: Context, args: argparse.Namespace) -> int:
    inserted = await seeds.seed_default_sources(ctx)
    print(f"seeded {inserted} new source(s)")

    return 0


async def _run(ctx: Context, args: argparse.Namespace) -> int:
    outcome = await run.run_pipeline(ctx, force=args.force, dry_run=args.dry_run)
    print(f"{outcome.reason} (digest {outcome.digest_id or '-'})")

    return 1 if outcome.failed else 0


async def _stage(ctx: Context, args: argparse.Namespace) -> int:
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
            since = ctx.clock.now() - timedelta(hours=settings.EVENT_WINDOW_HOURS)
            events = await ctx.events.list_unthreaded_since(since)
            ids = [event.id for event in events]
            print(f"linked {await threads.link_events_to_threads(ctx, ids)} event(s)")
        case _:
            print(f"unknown stage: {args.name}")

            return 1

    return 0


async def _sources(ctx: Context, args: argparse.Namespace) -> int:
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
        case _:
            update = {}

    return interests.model_copy(update=update)


async def _prefs(ctx: Context, args: argparse.Namespace) -> int:
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

    await ctx.preferences.save_profile(profile, now=now)
    await ctx.preferences.save_interests(interests, now=now)

    if args.action != "show":
        print(f"preferences updated ({args.action})")

    return 0


async def _digests(ctx: Context, args: argparse.Namespace) -> int:
    for digest in await ctx.digests.list_recent(limit=_DIGEST_LIST_LIMIT):
        print(f"{digest.date} {digest.delivery_status:<8} {digest.id} {digest.subject}")

    return 0


async def _threads(ctx: Context, args: argparse.Namespace) -> int:
    since = ctx.clock.now() - timedelta(days=settings.THREAD_INACTIVE_DAYS)

    for thread in await ctx.threads.list_active(since=since):
        pin = "pinned" if thread.pinned else "      "
        print(
            f"{pin} {thread.day_count:>2}d {thread.last_seen_at:%Y-%m-%d} "
            f"{thread.id} {thread.title}"
        )

    return 0


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

    for action in ("add-topic", "remove-topic", "remove-keyword", "follow", "block"):
        pref_actions.add_parser(action).add_argument("value")

    keyword = pref_actions.add_parser("add-keyword")
    keyword.add_argument("value")
    keyword.add_argument("--exclude", action="store_true")
    digests = commands.add_parser("digests", help="list recent digests")
    digests.add_subparsers(dest="action", required=True).add_parser("list")
    threads_parser = commands.add_parser("threads", help="list active threads")
    threads_parser.add_subparsers(dest="action", required=True).add_parser("list")
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
