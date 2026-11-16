import asyncio
from typing import override
from urllib.parse import quote

import pydantic

from app import logging
from app.context import AbstractContext
from app.models import Comment
from app.models import Engagement
from app.models import HackerNewsConfig
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

_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
_COMMENT_FETCH_CONCURRENCY = 4


class _Hit(pydantic.BaseModel):
    objectID: str  # noqa: N815
    title: str | None = None
    url: str | None = None
    author: str | None = None
    points: int | None = None
    num_comments: int | None = None
    created_at: str | None = None
    story_text: str | None = None


class _SearchResponse(pydantic.BaseModel):
    hits: list[_Hit]


class _AlgoliaComment(pydantic.BaseModel):
    id: int
    author: str | None = None
    text: str | None = None
    points: int | None = None
    # NOTE: Only the reply count matters for ranking; nested replies never reach the
    # digest.
    children: list[object] = []


class _ItemResponse(pydantic.BaseModel):
    children: list[_AlgoliaComment] = []


def _item_url(item_id: str | int) -> str:
    return f"https://news.ycombinator.com/item?id={item_id}"


def _search_url(config: HackerNewsConfig) -> str | FetchError:
    if config.mode == "front":
        return f"{_ALGOLIA_BASE}/search?tags=front_page"

    if config.query is None or config.query.strip() == "":
        return FetchError.NOT_CONFIGURED

    query = quote(config.query.strip(), safe="")
    numeric_filter = quote(f"points>{config.min_points}", safe="")

    return (
        f"{_ALGOLIA_BASE}/search_by_date?query={query}&tags=story"
        f"&numericFilters={numeric_filter}"
    )


def _comment_weight(comment: _AlgoliaComment) -> int:
    return len(comment.children) if comment.points is None else comment.points


def _to_comment(comment: _AlgoliaComment) -> Comment | None:
    text = normalise.strip_html(comment.text or "")

    if text == "":
        return None

    return Comment(
        id=str(comment.id),
        author=comment.author,
        text=text,
        score=comment.points,
        url=_item_url(comment.id),
    )


async def _fetch_top_comments(
    ctx: AbstractContext, story_id: str, limit: int
) -> list[Comment]:
    if limit == 0:
        return []

    response = await ctx.http.get(f"{_ALGOLIA_BASE}/items/{story_id}")

    if is_error(response) or not response.ok:
        logger.warning("Failed to fetch HN comments.", extra={"story": story_id})

        return []

    try:
        parsed = _ItemResponse.model_validate_json(response.body)
    except pydantic.ValidationError:
        logger.warning("Unexpected HN item payload.", extra={"story": story_id})

        return []

    ranked = sorted(parsed.children, key=_comment_weight, reverse=True)
    comments = [
        comment
        for comment in (_to_comment(child) for child in ranked)
        if comment is not None
    ]

    return comments[:limit]


async def _build_story(
    ctx: AbstractContext,
    source: Source,
    config: HackerNewsConfig,
    hit: _Hit,
    semaphore: asyncio.Semaphore,
) -> OnSuccess[SourceItem]:
    async with semaphore:
        comments = await _fetch_top_comments(ctx, hit.objectID, config.comment_limit)

    story_url = hit.url if hit.url is not None and hit.url != "" else None

    return normalise.build_item(
        source_id=source.id,
        kind=ItemKind.DISCUSSION,
        url=_item_url(hit.objectID),
        canonical_source=story_url,
        title=hit.title or "",
        author=hit.author,
        text=normalise.strip_html(hit.story_text or ""),
        published_at=normalise.parse_datetime(hit.created_at, fallback=ctx.clock.now()),
        language_hint=source.language,
        engagement=Engagement(score=hit.points or 0, comments=hit.num_comments or 0),
        comments=comments,
    )


class HackerNewsAdapter(ImplementsSourceAdapter):
    @property
    @override
    def kind(self) -> SourceKind:
        return "hn"

    @override
    async def fetch(
        self, ctx: AbstractContext, source: Source
    ) -> OnSuccess[list[SourceItem]]:
        if not isinstance(source.config, HackerNewsConfig):
            return FetchError.UNSUPPORTED

        config = source.config
        url = _search_url(config)

        if isinstance(url, FetchError):
            return url

        response = await ctx.http.get(url)

        if is_error(response) or not response.ok:
            return FetchError.FETCH_FAILED

        try:
            parsed = _SearchResponse.model_validate_json(response.body)
        except pydantic.ValidationError:
            return FetchError.PARSE_FAILED

        hits = [
            hit
            for hit in parsed.hits
            if (hit.points or 0) >= config.min_points
            and (hit.title or "").strip() != ""
        ]
        semaphore = asyncio.Semaphore(_COMMENT_FETCH_CONCURRENCY)
        built = await asyncio.gather(
            *(_build_story(ctx, source, config, hit, semaphore) for hit in hits)
        )
        items: list[SourceItem] = []

        for item in built:
            if is_error(item):
                logger.debug(
                    "Skipped invalid HN story.",
                    extra={"source": source.id, "error": str(item)},
                )

                continue

            items.append(item)

        return items
