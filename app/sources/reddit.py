import asyncio
import html
from datetime import UTC
from datetime import datetime
from typing import override

import pydantic

from app import logging
from app.context import AbstractContext
from app.models import Comment
from app.models import Engagement
from app.models import ItemKind
from app.models import RedditConfig
from app.models import Source
from app.models import SourceItem
from app.models import SourceKind
from app.result import OnSuccess
from app.result import is_error
from app.sources import normalise
from app.sources._common import FetchError
from app.sources._common import ImplementsSourceAdapter

logger = logging.get_logger(__name__)

_REDDIT_BASE = "https://www.reddit.com"
_LISTING_LIMIT = 40
_COMMENT_FETCH_CONCURRENCY = 4
_PLACEHOLDER_BODIES = frozenset({"[deleted]", "[removed]"})


class _Post(pydantic.BaseModel):
    id: str
    title: str
    url: str | None = None
    permalink: str
    author: str | None = None
    score: int | None = None
    num_comments: int | None = None
    created_utc: float | None = None
    selftext: str | None = None
    is_self: bool | None = None
    link_flair_text: str | None = None


class _Child(pydantic.BaseModel):
    kind: str
    data: dict[str, object]


class _ListingData(pydantic.BaseModel):
    children: list[_Child]


class _Listing(pydantic.BaseModel):
    data: _ListingData


class _CommentData(pydantic.BaseModel):
    id: str
    author: str | None = None
    body: str | None = None
    score: int | None = None
    permalink: str | None = None


_COMMENT_THREAD = pydantic.TypeAdapter(list[_Listing])


def _permalink_url(permalink: str) -> str:
    return f"{_REDDIT_BASE}{permalink}"


def _epoch_to_datetime(seconds: float | None, fallback: datetime) -> datetime:
    if seconds is None:
        return fallback

    return datetime.fromtimestamp(seconds, tz=UTC)


def _to_comment(data: dict[str, object]) -> Comment | None:
    try:
        comment = _CommentData.model_validate(data)
    except pydantic.ValidationError:
        return None

    text = html.unescape(comment.body or "").strip()

    if text == "" or text in _PLACEHOLDER_BODIES:
        return None

    return Comment(
        id=comment.id,
        author=comment.author,
        text=text,
        score=comment.score,
        url=None if comment.permalink is None else _permalink_url(comment.permalink),
    )


def _to_post(child: _Child) -> _Post | None:
    if child.kind != "t3":
        return None

    try:
        return _Post.model_validate(child.data)
    except pydantic.ValidationError:
        return None


async def _fetch_top_comments(
    ctx: AbstractContext, post_id: str, limit: int
) -> list[Comment]:
    if limit == 0:
        return []

    response = await ctx.http.get(
        f"{_REDDIT_BASE}/comments/{post_id}.json?limit={limit}&sort=top"
    )

    if is_error(response) or not response.ok:
        logger.warning("Failed to fetch Reddit comments.", extra={"post": post_id})

        return []

    try:
        listings = _COMMENT_THREAD.validate_json(response.body)
    except pydantic.ValidationError:
        listings = []

    if len(listings) < 2:
        logger.warning("Unexpected Reddit comment payload.", extra={"post": post_id})

        return []

    comments = [
        comment
        for comment in (
            _to_comment(child.data)
            for child in listings[1].data.children
            if child.kind == "t1"
        )
        if comment is not None
    ]
    comments.sort(key=lambda comment: comment.score or 0, reverse=True)

    return comments[:limit]


async def _build_post(
    ctx: AbstractContext,
    source: Source,
    config: RedditConfig,
    post: _Post,
    semaphore: asyncio.Semaphore,
) -> OnSuccess[SourceItem]:
    async with semaphore:
        comments = await _fetch_top_comments(ctx, post.id, config.comment_limit)

    discussion_url = _permalink_url(post.permalink)
    is_link_post = post.is_self is not True and post.url is not None
    flair = post.link_flair_text

    return normalise.build_item(
        source_id=source.id,
        kind=ItemKind.DISCUSSION,
        url=discussion_url,
        canonical_source=(post.url or discussion_url) if is_link_post else None,
        title=html.unescape(post.title),
        author=post.author,
        text=html.unescape(post.selftext or "").strip(),
        published_at=_epoch_to_datetime(post.created_utc, ctx.clock.now()),
        language_hint=source.language,
        engagement=Engagement(score=post.score or 0, comments=post.num_comments or 0),
        comments=comments,
        tags=() if flair is None else (flair,),
    )


class RedditAdapter(ImplementsSourceAdapter):
    @property
    @override
    def kind(self) -> SourceKind:
        return "reddit"

    @override
    def precheck(self, ctx: AbstractContext, source: Source) -> FetchError | None:
        if not isinstance(source.config, RedditConfig):
            return FetchError.UNSUPPORTED

        return None

    @override
    async def fetch(
        self, ctx: AbstractContext, source: Source
    ) -> OnSuccess[list[SourceItem]]:
        if not isinstance(source.config, RedditConfig):
            return FetchError.UNSUPPORTED

        config = source.config
        listing_url = (
            f"{_REDDIT_BASE}/r/{config.subreddit}/{config.sort}.json"
            f"?limit={_LISTING_LIMIT}&t=day"
        )
        response = await ctx.http.get(listing_url)

        if is_error(response) or not response.ok:
            return FetchError.FETCH_FAILED

        try:
            listing = _Listing.model_validate_json(response.body)
        except pydantic.ValidationError:
            return FetchError.PARSE_FAILED

        posts = [
            post
            for post in (_to_post(child) for child in listing.data.children)
            if post is not None and (post.score or 0) >= config.min_score
        ]
        semaphore = asyncio.Semaphore(_COMMENT_FETCH_CONCURRENCY)
        built = await asyncio.gather(
            *(_build_post(ctx, source, config, post, semaphore) for post in posts)
        )
        items: list[SourceItem] = []

        for item in built:
            if is_error(item):
                logger.debug(
                    "Skipped invalid Reddit post.",
                    extra={"source": source.id, "error": str(item)},
                )

                continue

            items.append(item)

        return items
