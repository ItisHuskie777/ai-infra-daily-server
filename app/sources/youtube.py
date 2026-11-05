from typing import override
from urllib.parse import quote

from app import logging
from app.context import AbstractContext
from app.models import ItemKind
from app.models import Source
from app.models import SourceItem
from app.models import SourceKind
from app.models import YoutubeConfig
from app.result import OnSuccess
from app.result import is_error
from app.sources import normalise
from app.sources import rss
from app.sources._common import FetchError
from app.sources._common import ImplementsSourceAdapter

logger = logging.get_logger(__name__)

_FEED_BASE = "https://www.youtube.com/feeds/videos.xml"


def _video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


class YoutubeAdapter(ImplementsSourceAdapter):
    @property
    @override
    def kind(self) -> SourceKind:
        return "youtube"

    @override
    def precheck(self, ctx: AbstractContext, source: Source) -> FetchError | None:
        if not isinstance(source.config, YoutubeConfig):
            return FetchError.UNSUPPORTED

        return None

    @override
    async def fetch(
        self, ctx: AbstractContext, source: Source
    ) -> OnSuccess[list[SourceItem]]:
        if not isinstance(source.config, YoutubeConfig):
            return FetchError.UNSUPPORTED

        channel_id = quote(source.config.channel_id, safe="")
        entries = await rss.fetch_feed(ctx, f"{_FEED_BASE}?channel_id={channel_id}")

        if is_error(entries):
            return entries

        now = ctx.clock.now()
        items: list[SourceItem] = []

        for entry in entries:
            video_id = rss.text_field(entry.raw, "yt_videoid")
            url = entry.link if video_id is None else _video_url(video_id)

            if url is None:
                logger.debug(
                    "Skipped video entry without id.",
                    extra={"source": source.id, "title": entry.title},
                )

                continue

            # NOTE: feedparser folds `media:group/media:description` into the summary.
            item = normalise.build_item(
                source_id=source.id,
                kind=ItemKind.COMMENTARY,
                url=url,
                title=normalise.strip_html(entry.title),
                author=entry.author,
                text=normalise.strip_html(entry.content),
                published_at=normalise.parse_datetime(entry.published, fallback=now),
                language_hint=source.language,
                tags=("video",),
            )

            if is_error(item):
                logger.debug(
                    "Skipped invalid video entry.",
                    extra={"source": source.id, "error": str(item)},
                )

                continue

            items.append(item)

        return items
