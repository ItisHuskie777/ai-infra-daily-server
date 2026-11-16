import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import override
from urllib.parse import quote

import pydantic

from app import logging
from app import settings
from app.adapters.browser import ImplementsBrowser
from app.context import AbstractContext
from app.models import Engagement
from app.models import ItemKind
from app.models import Language
from app.models import Model
from app.models import Source
from app.models import SourceItem
from app.models import SourceKind
from app.models import XConfig
from app.result import OnSuccess
from app.result import is_error
from app.sources import _social
from app.sources import normalise
from app.sources._common import FetchError
from app.sources._common import ImplementsSourceAdapter

logger = logging.get_logger(__name__)

# Every DOM selector and URL the adapter relies on; X renames `data-testid`s often.
SELECTORS: Mapping[str, str] = {
    "origin": "https://x.com",
    "homeUrl": "https://x.com/home",
    "tweetCard": 'article[data-testid="tweet"]',
    "tweetText": '[data-testid="tweetText"]',
    "userName": '[data-testid="User-Name"]',
    "statusLink": 'a[href*="/status/"]',
    "time": "time",
    "reply": '[data-testid="reply"]',
    "repost": '[data-testid="retweet"], [data-testid="unretweet"]',
    "like": '[data-testid="like"], [data-testid="unlike"]',
    "engagementGroup": 'div[role="group"][aria-label]',
    "socialContext": '[data-testid="socialContext"]',
    "statusPattern": r"^/([A-Za-z0-9_]{1,15})/status/(\d+)",
}

_FEED_SCROLL_ROUNDS = 3
_TITLE_LENGTH = 80
_STATUS = re.compile(SELECTORS["statusPattern"])
# Evaluated in the page; one record per rendered tweet card with the raw aria labels.
_TIMELINE_EXTRACT_SCRIPT = """(() => {
  const S = __SELECTORS__;
  const statusPattern = new RegExp(S.statusPattern);
  const text = (node, selector) => {
    const found = node.querySelector(selector);
    return found ? (found.textContent || "").trim() : undefined;
  };
  const label = (node, selector) => {
    const found = node.querySelector(selector);
    return found ? found.getAttribute("aria-label") || undefined : undefined;
  };
  return Array.from(document.querySelectorAll(S.tweetCard))
    .map((card) => {
      const links = Array.from(card.querySelectorAll(S.statusLink));
      const pathOf = (link) =>
        new URL(link.getAttribute("href"), location.origin).pathname;
      const permalink =
        links.find((link) => link.querySelector(S.time)) ||
        links.find((link) => statusPattern.test(pathOf(link)));
      if (!permalink) return null;
      const match = statusPattern.exec(pathOf(permalink));
      if (!match) return null;
      const timeNode = permalink.querySelector(S.time) || card.querySelector(S.time);
      const userName = card.querySelector(S.userName);
      const nameSpan = userName ? userName.querySelector("span") : null;
      return {
        id: match[2],
        handle: match[1],
        author: nameSpan ? (nameSpan.textContent || "").trim() : undefined,
        text: text(card, S.tweetText),
        time: timeNode ? timeNode.getAttribute("datetime") || undefined : undefined,
        reply_label: label(card, S.reply),
        repost_label: label(card, S.repost),
        like_label: label(card, S.like),
        group_label: label(card, S.engagementGroup),
        social_context: text(card, S.socialContext),
      };
    })
    .filter((card) => card !== null);
})()""".replace("__SELECTORS__", json.dumps(SELECTORS))


class _TweetCard(Model):
    id: str = pydantic.Field(min_length=1)
    handle: str = pydantic.Field(min_length=1)
    author: str | None = None
    text: str | None = None
    time: str | None = None
    reply_label: str | None = None
    repost_label: str | None = None
    like_label: str | None = None
    group_label: str | None = None
    social_context: str | None = None


_TIMELINE = pydantic.TypeAdapter(list[_TweetCard])


def _timeline_url_for(config: XConfig) -> OnSuccess[str]:
    if config.mode == "home":
        return SELECTORS["homeUrl"]

    handle = _social.optional_string(config.handle)
    cleaned = None if handle is None else handle.removeprefix("@")

    if cleaned is None or cleaned == "":
        return FetchError.NOT_CONFIGURED

    return f"{SELECTORS['origin']}/{quote(cleaned, safe='')}"


def _canonical_tweet_url(handle: str, tweet_id: str) -> str:
    return f"{SELECTORS['origin']}/{handle}/status/{tweet_id}"


def _engagement_of(card: _TweetCard) -> Engagement:
    group = _social.parse_aria_counts(card.group_label or "")
    likes = _social.parse_aria_counts(card.like_label or "").likes
    replies = _social.parse_aria_counts(card.reply_label or "").replies
    reposts = _social.parse_aria_counts(card.repost_label or "").reposts

    return Engagement(
        likes=group.likes if likes is None else likes,
        comments=group.replies if replies is None else replies,
        shares=group.reposts if reposts is None else reposts,
    )


def _build_item(
    source: Source, card: _TweetCard, *, now: datetime
) -> OnSuccess[SourceItem]:
    canonical_url = _canonical_tweet_url(card.handle, card.id)
    text = _social.optional_string(card.text) or ""
    first_line = next((line for line in text.split("\n") if line.strip() != ""), "")
    title = (
        first_line[:_TITLE_LENGTH] if first_line != "" else f"Post by @{card.handle}"
    )

    return normalise.build_item(
        source_id=source.id,
        kind=ItemKind.COMMENTARY,
        url=canonical_url,
        title=title,
        text=text,
        published_at=_social.to_datetime(card.time, fallback=now),
        author=_social.optional_string(card.author) or f"@{card.handle}",
        language_hint=Language.EN,
        engagement=_engagement_of(card),
    )


async def _render_timeline(
    browser: ImplementsBrowser, cookie: str, timeline_url: str
) -> OnSuccess[list[_TweetCard]]:
    rendered = await browser.render(
        timeline_url,
        cookie_header=cookie,
        script=_TIMELINE_EXTRACT_SCRIPT,
        scrolls=_FEED_SCROLL_ROUNDS,
    )

    if is_error(rendered):
        return FetchError.FETCH_FAILED

    try:
        cards = _TIMELINE.validate_python(rendered.extracted)
    except pydantic.ValidationError:
        cards = None

    if _social.is_blocked(rendered, has_content=bool(cards)):
        return FetchError.BLOCKED

    if cards is None:
        return FetchError.PARSE_FAILED

    return cards


def _select_cards(timeline: list[_TweetCard]) -> list[_TweetCard]:
    seen: set[str] = set()
    cards: list[_TweetCard] = []

    for card in timeline:
        if _STATUS.match(f"/{card.handle}/status/{card.id}") is None:
            continue

        if card.id in seen:
            continue

        seen.add(card.id)
        cards.append(card)

    return cards[: settings.MAX_ITEMS_PER_SOURCE]


class XAdapter(ImplementsSourceAdapter):
    __slots__ = ("_cookie",)

    def __init__(self, *, cookie: str) -> None:
        self._cookie = cookie

    @property
    @override
    def kind(self) -> SourceKind:
        return "x"

    @override
    async def fetch(
        self, ctx: AbstractContext, source: Source
    ) -> OnSuccess[list[SourceItem]]:
        config = source.config

        if not isinstance(config, XConfig):
            return FetchError.UNSUPPORTED

        browser = ctx.browser
        cookie = self._cookie

        if browser is None or cookie == "":
            return FetchError.NOT_CONFIGURED

        timeline_url = _timeline_url_for(config)

        if is_error(timeline_url):
            return timeline_url

        timeline = await _render_timeline(browser, cookie, timeline_url)

        if is_error(timeline):
            return timeline

        now = ctx.clock.now()
        items: list[SourceItem] = []

        for card in _select_cards(timeline):
            item = _build_item(source, card, now=now)

            if is_error(item):
                logger.warning(
                    "Skipped a tweet that did not normalise.",
                    extra={"tweet_id": card.id, "error": str(item)},
                )

                continue

            items.append(item)

        logger.info(
            "Fetched X timeline.",
            extra={
                "source": source.id,
                "mode": config.mode,
                "cards": len(timeline),
                "items": len(items),
            },
        )

        return items
