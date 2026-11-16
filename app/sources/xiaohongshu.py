import asyncio
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import override
from urllib.parse import quote

import pydantic

from app import logging
from app.adapters.browser import ImplementsBrowser
from app.context import AbstractContext
from app.models import Comment
from app.models import Engagement
from app.models import ItemKind
from app.models import Language
from app.models import Model
from app.models import Source
from app.models import SourceItem
from app.models import SourceKind
from app.models import XiaohongshuConfig
from app.result import OnSuccess
from app.result import is_error
from app.sources import _social
from app.sources import normalise
from app.sources._common import FetchError
from app.sources._common import ImplementsSourceAdapter
from app.sources._social import Count

logger = logging.get_logger(__name__)

# Every DOM selector, URL and `__INITIAL_STATE__` path the adapter relies on. The site
# changes often; fix things here first.
SELECTORS: Mapping[str, str | tuple[str, ...]] = {
    "origin": "https://www.xiaohongshu.com",
    "followingFeedUrl": "https://www.xiaohongshu.com/explore?channel_id=homefeed.follow",
    "userProfileUrl": "https://www.xiaohongshu.com/user/profile/",
    "notePath": "/explore/",
    "noteSource": "pc_feed",
    "stateGlobal": "__INITIAL_STATE__",
    "stateFeedList": ("feed", "feeds"),
    "stateUserNotes": ("user", "notes"),
    "stateNoteDetailMap": ("note", "noteDetailMap"),
    "feedCard": "section.note-item",
    "feedCardLink": 'a[href*="/explore/"], a.cover, a[href*="/discovery/item/"]',
    "feedTitle": ".footer .title, .title",
    "feedAuthor": ".author .name, .author-wrapper .name, .name",
    "feedLikes": ".like-wrapper .count, .count",
    "noteTitle": "#detail-title, .note-content .title",
    "noteDesc": "#detail-desc, .note-content .desc",
    "noteAuthor": ".author-wrapper .username, .author-container .username, .username",
    "noteDate": ".bottom-container .date, .date",
    "noteLikes": ".engage-bar .like-wrapper .count, .like-wrapper .count",
    "noteCommentCount": ".engage-bar .chat-wrapper .count, .chat-wrapper .count",
    "commentItem": ".comment-item, .parent-comment .comment-item",
    "commentAuthor": ".author .name, .name",
    "commentText": ".content .note-text, .content",
    "commentLikes": ".like-wrapper .count, .count",
    "noteIdPattern": "([0-9a-f]{24})",
}

_NOTE_LIMIT = 20
_FEED_SCROLL_ROUNDS = 3
_NOTE_RENDER_CONCURRENCY = 2
_FALLBACK_TITLE_LENGTH = 80
_NOTE_ID = re.compile(r"^[0-9a-f]{24}$")
_NOTE_URL = re.compile(f"{SELECTORS['notePath']}{SELECTORS['noteIdPattern']}")
_SELECTORS_JSON = json.dumps(SELECTORS)
_JS_HELPERS = """
  const unwrap = (value) =>
    value && typeof value === "object" && ("_rawValue" in value || "_value" in value)
      ? (value._rawValue ?? value._value)
      : value;
  const dig = (root, path) =>
    path.reduce((node, key) => (node == null ? undefined : unwrap(node[key])), root);
  const text = (node, selector) => {
    const found = node.querySelector(selector);
    return found ? (found.textContent || "").trim() : undefined;
  };
"""
# Evaluated in the page; prefers `__INITIAL_STATE__`, falls back to the rendered cards.
_FEED_EXTRACT_SCRIPT = (
    """(() => {
  const S = __SELECTORS__;"""
    + _JS_HELPERS
    + """
  const noteIdOf = (href) => {
    const match = new RegExp(S.notePath + S.noteIdPattern).exec(href || "");
    return match ? match[1] : undefined;
  };
  const fromState = () => {
    const state = window[S.stateGlobal];
    if (!state) return [];
    const feeds = dig(state, S.stateFeedList);
    const userNotes = dig(state, S.stateUserNotes);
    const list =
      Array.isArray(feeds) && feeds.length > 0
        ? feeds
        : Array.isArray(userNotes)
          ? userNotes.flat()
          : [];
    return list
      .map((entry) => {
        const card = entry && (entry.noteCard || entry.note_card || entry);
        const noteId = entry && (entry.id || entry.noteId || (card && card.noteId));
        if (!noteId || !card) return null;
        const token = entry.xsecToken || entry.xsec_token || card.xsecToken;
        const query = token
          ? "?xsec_token=" + encodeURIComponent(token) + "&xsec_source=" + S.noteSource
          : "";
        const interact = card.interactInfo || card.interact_info || {};
        return {
          note_id: String(noteId),
          url: S.origin + S.notePath + noteId + query,
          title: card.displayTitle || card.display_title || card.title,
          author: card.user && (card.user.nickname || card.user.nickName),
          likes: interact.likedCount ?? interact.liked_count,
          published_at: card.time,
        };
      })
      .filter((note) => note !== null);
  };
  const fromDom = () =>
    Array.from(document.querySelectorAll(S.feedCard))
      .map((card) => {
        const link = card.querySelector(S.feedCardLink);
        const href = link ? link.getAttribute("href") : null;
        const noteId = noteIdOf(href);
        if (!href || !noteId) return null;
        return {
          note_id: noteId,
          url: new URL(href, location.origin).toString(),
          title: text(card, S.feedTitle),
          author: text(card, S.feedAuthor),
          likes: text(card, S.feedLikes),
        };
      })
      .filter((note) => note !== null);
  const state = fromState();
  return state.length > 0 ? state : fromDom();
})()"""
).replace("__SELECTORS__", _SELECTORS_JSON)
_NOTE_EXTRACT_SCRIPT = (
    """(() => {
  const S = __SELECTORS__;
  const noteId = __NOTE_ID__;"""
    + _JS_HELPERS
    + """
  const fromState = () => {
    const state = window[S.stateGlobal];
    const detailMap = state ? dig(state, S.stateNoteDetailMap) : undefined;
    const detail = detailMap ? unwrap(detailMap[noteId]) : undefined;
    const note = detail && unwrap(detail.note);
    if (!note) return null;
    const interact = note.interactInfo || note.interact_info || {};
    const commentList = (unwrap(detail.comments) || {}).list || [];
    return {
      title: note.title,
      desc: note.desc,
      author: note.user && (note.user.nickname || note.user.nickName),
      time: note.time,
      likes: interact.likedCount ?? interact.liked_count,
      comment_count: interact.commentCount ?? interact.comment_count,
      comments: (Array.isArray(commentList) ? commentList : []).map((comment) => ({
        id: comment.id,
        author: comment.userInfo && comment.userInfo.nickname,
        text: comment.content,
        likes: comment.likeCount ?? comment.like_count,
      })),
    };
  };
  const fromDom = () => {
    const title = text(document, S.noteTitle);
    const desc = text(document, S.noteDesc);
    if (title === undefined && desc === undefined) return null;
    return {
      title,
      desc,
      author: text(document, S.noteAuthor),
      time: text(document, S.noteDate),
      likes: text(document, S.noteLikes),
      comment_count: text(document, S.noteCommentCount),
      comments: Array.from(document.querySelectorAll(S.commentItem)).map(
        (item, index) => ({
          id: item.getAttribute("data-id") || item.id || String(index),
          author: text(item, S.commentAuthor),
          text: text(item, S.commentText),
          likes: text(item, S.commentLikes),
        }),
      ),
    };
  };
  return fromState() ?? fromDom();
})()"""
).replace("__SELECTORS__", _SELECTORS_JSON)


class _FeedNote(Model):
    note_id: str = pydantic.Field(min_length=1)
    url: str = pydantic.Field(min_length=1)
    title: str | None = None
    author: str | None = None
    likes: Count = None
    published_at: Count = None


class _ExtractedComment(Model):
    id: Count = None
    author: str | None = None
    text: str | None = None
    likes: Count = None


class _NoteDetail(Model):
    title: str | None = None
    desc: str | None = None
    author: str | None = None
    time: Count = None
    likes: Count = None
    comment_count: Count = None
    comments: tuple[_ExtractedComment, ...] = ()


_FEED = pydantic.TypeAdapter(list[_FeedNote])
_NOTE_DETAIL = pydantic.TypeAdapter(_NoteDetail | None)


def _note_extract_script(note_id: str) -> str:
    return _NOTE_EXTRACT_SCRIPT.replace("__NOTE_ID__", json.dumps(note_id))


def _feed_url_for(config: XiaohongshuConfig) -> OnSuccess[str]:
    if config.mode == "following":
        return str(SELECTORS["followingFeedUrl"])

    user_id = _social.optional_string(config.user_id)

    if user_id is None:
        return FetchError.NOT_CONFIGURED

    return f"{SELECTORS['userProfileUrl']}{quote(user_id, safe='')}"


def _canonical_note_url(note_id: str) -> str:
    return f"{SELECTORS['origin']}{SELECTORS['notePath']}{note_id}"


def _select_comments(detail: _NoteDetail, limit: int) -> list[Comment]:
    ranked: list[tuple[int, Comment]] = []

    for index, raw in enumerate(detail.comments):
        text = _social.optional_string(raw.text)

        if text is None:
            continue

        likes = _social.parse_count(raw.likes)
        raw_id = None if raw.id is None else _social.optional_string(str(raw.id))

        comment = Comment(
            id=raw_id or f"comment-{index}",
            author=_social.optional_string(raw.author),
            text=text,
            score=likes,
        )

        ranked.append((likes or 0, comment))

    ranked.sort(key=lambda entry: entry[0], reverse=True)

    return [comment for _, comment in ranked[:limit]]


def _build_item(
    source: Source,
    feed_note: _FeedNote,
    detail: _NoteDetail | None,
    *,
    comment_limit: int,
    now: datetime,
) -> OnSuccess[SourceItem]:
    note = _NoteDetail() if detail is None else detail
    desc = _social.optional_string(note.desc)
    desc_title = None if desc is None else desc.split("\n")[0][:_FALLBACK_TITLE_LENGTH]
    title = (
        _social.optional_string(note.title)
        or _social.optional_string(feed_note.title)
        or desc_title
        or feed_note.note_id
    )
    likes = _social.parse_count(note.likes)

    if likes is None:
        likes = _social.parse_count(feed_note.likes)

    published = feed_note.published_at if note.time is None else note.time

    return normalise.build_item(
        source_id=source.id,
        kind=ItemKind.COMMENTARY,
        url=feed_note.url,
        canonical_source=_canonical_note_url(feed_note.note_id),
        title=title,
        text=desc or "",
        published_at=_social.to_datetime(published, fallback=now),
        author=_social.optional_string(note.author)
        or _social.optional_string(feed_note.author),
        language_hint=Language.ZH,
        engagement=Engagement(
            likes=likes, comments=_social.parse_count(note.comment_count)
        ),
        comments=_select_comments(note, comment_limit),
    )


async def _render_note(
    browser: ImplementsBrowser,
    cookie: str,
    feed_note: _FeedNote,
    *,
    semaphore: asyncio.Semaphore,
) -> _NoteDetail | None:
    async with semaphore:
        rendered = await browser.render(
            feed_note.url,
            cookie_header=cookie,
            script=_note_extract_script(feed_note.note_id),
        )

    if is_error(rendered):
        logger.warning(
            "Note page failed to render; using feed card only.",
            extra={"note_id": feed_note.note_id, "error": str(rendered)},
        )

        return None

    try:
        detail = _NOTE_DETAIL.validate_python(rendered.extracted)
    except pydantic.ValidationError as error:
        logger.warning(
            "Note page extraction did not match the expected shape.",
            extra={"note_id": feed_note.note_id, "issues": error.error_count()},
        )
        detail = None

    has_text = detail is not None and (detail.title or detail.desc) is not None

    if _social.is_blocked(rendered, has_content=has_text):
        logger.warning(
            "Note page looks like a login wall; using feed card only.",
            extra={"note_id": feed_note.note_id},
        )

        return None

    return detail


async def _render_feed(
    browser: ImplementsBrowser, cookie: str, feed_url: str
) -> OnSuccess[list[_FeedNote]]:
    rendered = await browser.render(
        feed_url,
        cookie_header=cookie,
        script=_FEED_EXTRACT_SCRIPT,
        scrolls=_FEED_SCROLL_ROUNDS,
    )

    if is_error(rendered):
        return FetchError.FETCH_FAILED

    try:
        notes = _FEED.validate_python(rendered.extracted)
    except pydantic.ValidationError:
        notes = None

    if _social.is_blocked(rendered, has_content=bool(notes)):
        return FetchError.BLOCKED

    if notes is None:
        return FetchError.PARSE_FAILED

    return notes


def _select_notes(feed: list[_FeedNote]) -> list[_FeedNote]:
    seen: set[str] = set()
    notes: list[_FeedNote] = []

    for note in feed:
        if _NOTE_URL.search(note.url) is None and _NOTE_ID.match(note.note_id) is None:
            continue

        if note.note_id in seen:
            continue

        seen.add(note.note_id)
        notes.append(note)

    return notes[:_NOTE_LIMIT]


class XiaohongshuAdapter(ImplementsSourceAdapter):
    __slots__ = ("_cookie",)

    def __init__(self, *, cookie: str) -> None:
        self._cookie = cookie

    @property
    @override
    def kind(self) -> SourceKind:
        return "xiaohongshu"

    @override
    async def fetch(
        self, ctx: AbstractContext, source: Source
    ) -> OnSuccess[list[SourceItem]]:
        config = source.config

        if not isinstance(config, XiaohongshuConfig):
            return FetchError.UNSUPPORTED

        browser = ctx.browser
        cookie = self._cookie

        if browser is None or cookie == "":
            return FetchError.NOT_CONFIGURED

        feed_url = _feed_url_for(config)

        if is_error(feed_url):
            return feed_url

        feed = await _render_feed(browser, cookie, feed_url)

        if is_error(feed):
            return feed

        notes = _select_notes(feed)
        semaphore = asyncio.Semaphore(_NOTE_RENDER_CONCURRENCY)

        details = await asyncio.gather(
            *(
                _render_note(browser, cookie, note, semaphore=semaphore)
                for note in notes
            )
        )

        now = ctx.clock.now()
        items: list[SourceItem] = []

        for note, detail in zip(notes, details, strict=True):
            item = _build_item(
                source, note, detail, comment_limit=config.comment_limit, now=now
            )

            if is_error(item):
                logger.warning(
                    "Skipped a note that did not normalise.",
                    extra={"note_id": note.note_id, "error": str(item)},
                )

                continue

            items.append(item)

        logger.info(
            "Fetched Xiaohongshu feed.",
            extra={
                "source": source.id,
                "mode": config.mode,
                "cards": len(feed),
                "items": len(items),
                "detailed": sum(detail is not None for detail in details),
            },
        )

        return items
