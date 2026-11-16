from collections.abc import Mapping
from typing import override

import feedparser

from app import logging
from app.context import AbstractContext
from app.models import ItemKind
from app.models import Model
from app.models import RssConfig
from app.models import Source
from app.models import SourceCategory
from app.models import SourceItem
from app.models import SourceKind
from app.result import OnSuccess
from app.result import is_error
from app.sources import normalise
from app.sources._common import FetchError
from app.sources._common import ImplementsSourceAdapter

logger = logging.get_logger(__name__)

_ITEM_KINDS: dict[SourceCategory, ItemKind] = {
    SourceCategory.VENDOR: ItemKind.PRIMARY,
    SourceCategory.PAPER: ItemKind.PRIMARY,
    SourceCategory.MEDIA: ItemKind.MEDIA,
    SourceCategory.AUTHOR: ItemKind.COMMENTARY,
    SourceCategory.COMMUNITY: ItemKind.DISCUSSION,
}


class FeedEntry(Model):
    """One RSS `<item>` or Atom `<entry>` with the fields both formats share; `raw`
    keeps feedparser's full mapping for extensions such as `yt:videoId`."""

    title: str
    link: str | None
    author: str | None
    published: str | None
    content: str
    categories: tuple[str, ...]
    raw: dict[str, object]


def text_field(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)

    if not isinstance(value, str) or value.strip() == "":
        return None

    return value.strip()


def _first_content(raw: Mapping[str, object]) -> str:
    content = raw.get("content")

    if isinstance(content, list) and content:
        first = content[0]

        if isinstance(first, Mapping):
            value = first.get("value")

            if isinstance(value, str) and value.strip() != "":
                return value

    return text_field(raw, "summary") or ""


def _categories(raw: Mapping[str, object]) -> tuple[str, ...]:
    tags = raw.get("tags")

    if not isinstance(tags, list):
        return ()

    terms: list[str] = []

    for tag in tags:
        if isinstance(tag, Mapping):
            term = tag.get("term")

            if isinstance(term, str) and term.strip() != "":
                terms.append(term.strip())

    return tuple(terms)


def _to_entry(raw: Mapping[str, object]) -> FeedEntry:
    return FeedEntry(
        title=text_field(raw, "title") or "",
        link=text_field(raw, "link"),
        author=text_field(raw, "author"),
        published=text_field(raw, "published") or text_field(raw, "updated"),
        content=_first_content(raw),
        categories=_categories(raw),
        raw=dict(raw),
    )


def parse_feed(body: bytes) -> OnSuccess[list[FeedEntry]]:
    """Empty feeds parse to an empty list; a document that is not a feed at all is a
    parse failure."""
    parsed = feedparser.parse(body)
    entries = [_to_entry(entry) for entry in parsed.entries]

    if not entries and not parsed.version:
        return FetchError.PARSE_FAILED

    return entries


def item_kind_for(category: SourceCategory) -> ItemKind:
    return _ITEM_KINDS[category]


async def fetch_feed(ctx: AbstractContext, url: str) -> OnSuccess[list[FeedEntry]]:
    response = await ctx.http.get(url)

    if is_error(response) or not response.ok:
        return FetchError.FETCH_FAILED

    return parse_feed(response.body)


class RssAdapter(ImplementsSourceAdapter):
    @property
    @override
    def kind(self) -> SourceKind:
        return "rss"

    @override
    async def fetch(
        self, ctx: AbstractContext, source: Source
    ) -> OnSuccess[list[SourceItem]]:
        if not isinstance(source.config, RssConfig):
            return FetchError.UNSUPPORTED

        entries = await fetch_feed(ctx, source.config.url)

        if is_error(entries):
            return entries

        kind = item_kind_for(source.category)
        now = ctx.clock.now()
        items: list[SourceItem] = []

        for entry in entries:
            if entry.link is None:
                logger.debug(
                    "Skipped feed entry without link.",
                    extra={"source": source.id, "title": entry.title},
                )

                continue

            item = normalise.build_item(
                source_id=source.id,
                kind=kind,
                url=entry.link,
                title=normalise.strip_html(entry.title),
                author=entry.author,
                text=normalise.strip_html(entry.content),
                published_at=normalise.parse_datetime(entry.published, fallback=now),
                language_hint=source.language,
                tags=entry.categories,
            )

            if is_error(item):
                logger.debug(
                    "Skipped invalid feed entry.",
                    extra={"source": source.id, "error": str(item)},
                )

                continue

            items.append(item)

        return items
