import asyncio
import re
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from typing import override

from app import logging
from app import settings
from app.adapters.http import HttpResponse
from app.context import AbstractContext
from app.models import ItemKind
from app.models import RedditConfig
from app.models import Source
from app.models import SourceItem
from app.models import SourceKind
from app.result import OnSuccess
from app.result import is_error
from app.sources import normalise
from app.sources import rss
from app.sources._common import FetchError
from app.sources._common import ImplementsSourceAdapter

type Sleep = Callable[[float], Awaitable[None]]

logger = logging.get_logger(__name__)

_REDDIT_BASE = "https://www.reddit.com"
_TOO_MANY_REQUESTS = 429
_MAX_WAIT_SECONDS = 120.0
# A request that cannot start this long after its fetch began would be cut off by the
# source deadline, so it gives up at once instead of sleeping under the lock.
_START_BUDGET_SECONDS = (
    settings.SOURCE_DEADLINE_SECONDS - settings.SOURCE_FETCH_TIMEOUT_SECONDS
)
_AUTHOR_PREFIX = "/u/"
_LINK_ANCHOR = re.compile(r'<a href="([^"]+)">\[link\]</a>')
_FOOTER = re.compile(r"\s*submitted by\s+/u/\S+\s*\[link\]\s*\[comments\]\s*$")


def _header_number(response: HttpResponse, name: str) -> float | None:
    try:
        return float(response.headers[name])
    except KeyError, ValueError:
        return None


def _linked_url(content: str, discussion_url: str) -> str | None:
    """The page a link post points at; self posts link back to their own thread."""
    match = _LINK_ANCHOR.search(content)

    if match is None:
        return None

    linked = normalise.strip_html(match.group(1))

    return None if linked.rstrip("/") == discussion_url.rstrip("/") else linked


def _author(raw: str | None) -> str | None:
    return None if raw is None else raw.removeprefix(_AUTHOR_PREFIX)


class RedditAdapter(ImplementsSourceAdapter):
    """Reads a subreddit's public Atom feed. Reddit refuses anonymous JSON requests and
    allows roughly one feed request a minute, so requests are serialised and each waits
    out the window the previous response announced. The feed carries neither scores
    nor comments; the listing order (`top` of the day) stands in for the score."""

    __slots__ = ("_lock", "_ready_at", "_sleep")

    def __init__(self, *, sleep: Sleep = asyncio.sleep) -> None:
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._ready_at: datetime | None = None

    @property
    @override
    def kind(self) -> SourceKind:
        return "reddit"

    @override
    async def fetch(
        self, ctx: AbstractContext, source: Source
    ) -> OnSuccess[list[SourceItem]]:
        if not isinstance(source.config, RedditConfig):
            return FetchError.UNSUPPORTED

        config = source.config
        response = await self._get(
            ctx,
            f"{_REDDIT_BASE}/r/{config.subreddit}/{config.sort}/.rss"
            f"?t=day&limit={config.limit}",
            give_up_at=ctx.clock.now() + timedelta(seconds=_START_BUDGET_SECONDS),
        )

        if response is FetchError.RATE_LIMITED:
            return response

        if is_error(response) or not response.ok:
            return FetchError.FETCH_FAILED

        entries = rss.parse_feed(response.body)

        if is_error(entries):
            return entries

        now = ctx.clock.now()
        items: list[SourceItem] = []

        for entry in entries:
            if entry.link is None:
                continue

            item = normalise.build_item(
                source_id=source.id,
                kind=ItemKind.DISCUSSION,
                url=entry.link,
                canonical_source=_linked_url(entry.content, entry.link),
                title=normalise.strip_html(entry.title),
                author=_author(entry.author),
                text=_FOOTER.sub("", normalise.strip_html(entry.content)),
                published_at=normalise.parse_datetime(entry.published, fallback=now),
                language_hint=source.language,
            )

            if is_error(item):
                logger.debug(
                    "Skipped invalid Reddit post.",
                    extra={"source": source.id, "error": str(item)},
                )

                continue

            items.append(item)

        return items

    async def _get(
        self, ctx: AbstractContext, url: str, *, give_up_at: datetime
    ) -> OnSuccess[HttpResponse]:
        async with self._lock:
            if not await self._wait_for_window(ctx, give_up_at):
                return FetchError.RATE_LIMITED

            response = await ctx.http.get(url)

            if is_error(response):
                return response

            self._note_window(ctx, response)

            if response.status != _TOO_MANY_REQUESTS:
                return response

            logger.info("Reddit rate limit reached; retrying once.", extra={"url": url})

            if not await self._wait_for_window(ctx, give_up_at):
                return FetchError.RATE_LIMITED

            retried = await ctx.http.get(url)

            if not is_error(retried):
                self._note_window(ctx, retried)

            return retried

    async def _wait_for_window(
        self, ctx: AbstractContext, give_up_at: datetime
    ) -> bool:
        """False when Reddit's window opens too late for this fetch to use it."""
        if self._ready_at is None:
            return True

        if self._ready_at > give_up_at:
            return False

        delay = (self._ready_at - ctx.clock.now()).total_seconds()

        if delay > 0:
            await self._sleep(delay)

        return True

    def _note_window(self, ctx: AbstractContext, response: HttpResponse) -> None:
        remaining = _header_number(response, "x-ratelimit-remaining")
        reset = _header_number(response, "x-ratelimit-reset")

        if remaining is None or reset is None or remaining >= 1:
            self._ready_at = None

            return

        wait = min(max(reset, 0.0), _MAX_WAIT_SECONDS) + 1.0
        self._ready_at = ctx.clock.now() + timedelta(seconds=wait)
