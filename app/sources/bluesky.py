from typing import override
from urllib.parse import quote

import pydantic

from app import logging
from app.context import AbstractContext
from app.models import BlueskyConfig
from app.models import Engagement
from app.models import ItemKind
from app.models import Source
from app.models import SourceItem
from app.models import SourceKind
from app.result import OnSuccess
from app.result import is_error
from app.sources import normalise
from app.sources._common import FetchError
from app.sources._common import ImplementsSourceAdapter

logger = logging.get_logger(__name__)

_BSKY_API = "https://public.api.bsky.app/xrpc"
_FEED_LIMIT = 30


class _Author(pydantic.BaseModel):
    handle: str
    displayName: str | None = None  # noqa: N815


class _Record(pydantic.BaseModel):
    text: str = ""
    createdAt: str | None = None  # noqa: N815
    reply: object | None = None


class _External(pydantic.BaseModel):
    uri: str
    title: str | None = None


class _Embed(pydantic.BaseModel):
    external: _External | None = None


class _Post(pydantic.BaseModel):
    uri: str
    author: _Author
    record: _Record
    embed: _Embed | None = None
    likeCount: int | None = None  # noqa: N815
    replyCount: int | None = None  # noqa: N815
    repostCount: int | None = None  # noqa: N815


class _FeedEntry(pydantic.BaseModel):
    post: _Post
    reason: object | None = None


class _FeedResponse(pydantic.BaseModel):
    feed: list[_FeedEntry]


def _feed_url(config: BlueskyConfig) -> str | FetchError:
    if config.mode == "author":
        if config.handle is None or config.handle.strip() == "":
            return FetchError.NOT_CONFIGURED

        actor = quote(config.handle.strip(), safe="")

        return (
            f"{_BSKY_API}/app.bsky.feed.getAuthorFeed?actor={actor}&limit={_FEED_LIMIT}"
        )

    if config.feed_uri is None or config.feed_uri.strip() == "":
        return FetchError.NOT_CONFIGURED

    feed = quote(config.feed_uri.strip(), safe="")

    return f"{_BSKY_API}/app.bsky.feed.getFeed?feed={feed}&limit={_FEED_LIMIT}"


def post_url(handle: str, at_uri: str) -> str:
    record_key = at_uri.rsplit("/", 1)[-1]

    return f"https://bsky.app/profile/{handle}/post/{record_key}"


def _post_text(post: _Post) -> str:
    parts = [post.record.text.strip()]

    if post.embed is not None and post.embed.external is not None:
        parts.append(post.embed.external.uri)

    return "\n".join(part for part in parts if part != "")


class BlueskyAdapter(ImplementsSourceAdapter):
    @property
    @override
    def kind(self) -> SourceKind:
        return "bluesky"

    @override
    def precheck(self, ctx: AbstractContext, source: Source) -> FetchError | None:
        if not isinstance(source.config, BlueskyConfig):
            return FetchError.UNSUPPORTED

        url = _feed_url(source.config)

        return url if isinstance(url, FetchError) else None

    @override
    async def fetch(
        self, ctx: AbstractContext, source: Source
    ) -> OnSuccess[list[SourceItem]]:
        if not isinstance(source.config, BlueskyConfig):
            return FetchError.UNSUPPORTED

        url = _feed_url(source.config)

        if isinstance(url, FetchError):
            return url

        response = await ctx.http.get(url)

        if is_error(response) or not response.ok:
            return FetchError.FETCH_FAILED

        try:
            parsed = _FeedResponse.model_validate_json(response.body)
        except pydantic.ValidationError:
            return FetchError.PARSE_FAILED

        now = ctx.clock.now()
        items: list[SourceItem] = []

        for entry in parsed.feed:
            # NOTE: Reposts and replies lack the author's own context, so only original
            # posts are kept.
            if entry.reason is not None or entry.post.record.reply is not None:
                continue

            post = entry.post
            text = _post_text(post)

            if text == "":
                continue

            display_name = (post.author.displayName or "").strip()
            item = normalise.build_item(
                source_id=source.id,
                kind=ItemKind.COMMENTARY,
                url=post_url(post.author.handle, post.uri),
                title=normalise.make_title(text),
                author=post.author.handle if display_name == "" else display_name,
                text=text,
                published_at=normalise.parse_datetime(
                    post.record.createdAt, fallback=now
                ),
                language_hint=source.language,
                engagement=Engagement(
                    likes=post.likeCount or 0,
                    comments=post.replyCount or 0,
                    shares=post.repostCount or 0,
                ),
            )

            if is_error(item):
                logger.debug(
                    "Skipped invalid Bluesky post.",
                    extra={"source": source.id, "error": str(item)},
                )

                continue

            items.append(item)

        return items
