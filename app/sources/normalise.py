import hashlib
import html
import re
from collections.abc import Sequence
from datetime import UTC
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import override
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import pydantic

from app.models import Comment
from app.models import Engagement
from app.models import ItemKind
from app.models import Language
from app.models import SourceItem
from app.result import OnSuccess
from app.result import ServiceError

_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "yclid",
        "twclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "source",
        "_hsenc",
        "_hsmi",
    }
)
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")
# Share of letters that must be Han to count as Chinese; mixed posts quote English.
_CHINESE_RATIO = 0.3
_ENGLISH_RATIO = 0.7
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_SCRIPT_STYLE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_BREAK = re.compile(
    r"<\s*(br|/p|/div|/li|/h[1-6]|/tr|/blockquote|/pre)\b[^>]*>", re.IGNORECASE
)
_CELL = re.compile(r"<\s*/?(td|th)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_HORIZONTAL_SPACE = re.compile("[ \\t\\u00a0]+")
_LINE_SPACE = re.compile(r"\s*\n\s*")
_TITLE_MAX_LENGTH = 120
_WEB_SCHEMES = frozenset({"http", "https"})


class NormaliseError(ServiceError):
    INVALID_ITEM = "invalid_item"

    @override
    def service(self) -> str:
        return "sources"


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()

    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMS


def canonicalise_url(url: str) -> str:
    """Lower-cases the host, drops tracking parameters, sorts the rest and strips a
    trailing slash so the same page always hashes to the same item id (FR2.1)."""
    trimmed = url.strip()
    parts = urlsplit(trimmed)

    if parts.scheme == "" or parts.netloc == "":
        return trimmed

    kept = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    )
    path = parts.path.rstrip("/") if len(parts.path) > 1 else parts.path

    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(kept), "")
    )


def _is_web_link(url: str) -> bool:
    return urlsplit(url.strip()).scheme.lower() in _WEB_SCHEMES


def item_id_for(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def detect_language(text: str) -> Language:
    han = len(_HAN.findall(text))
    latin = len(_LATIN.findall(text))
    letters = han + latin

    if letters == 0:
        return Language.OTHER

    if han / letters >= _CHINESE_RATIO:
        return Language.ZH

    return Language.EN if latin / letters >= _ENGLISH_RATIO else Language.OTHER


def strip_html(markup: str) -> str:
    text = _CDATA.sub(r"\1", markup)
    text = _SCRIPT_STYLE.sub(" ", text)
    text = _HTML_COMMENT.sub(" ", text)
    text = _BLOCK_BREAK.sub("\n", text)
    text = _CELL.sub(" ", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = _HORIZONTAL_SPACE.sub(" ", text)
    text = _LINE_SPACE.sub("\n", text)

    return text.strip()


def parse_datetime(value: str | None, *, fallback: datetime) -> datetime:
    """Accepts ISO 8601 and RFC 2822; naive values are taken as UTC."""
    if value is None or value.strip() == "":
        return fallback

    text = value.strip()
    parsed: datetime | None = None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except TypeError, ValueError:
            parsed = None

    if parsed is None:
        return fallback

    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def make_title(text: str, *, max_length: int = _TITLE_MAX_LENGTH) -> str:
    first_line = next((line.strip() for line in text.split("\n") if line.strip()), "")

    if len(first_line) <= max_length:
        return first_line

    return first_line[: max_length - 1].rstrip() + "…"


def build_item(
    *,
    source_id: str,
    kind: ItemKind,
    url: str,
    title: str,
    text: str,
    published_at: datetime,
    canonical_source: str | None = None,
    author: str | None = None,
    language_hint: Language | None = None,
    engagement: Engagement | None = None,
    comments: Sequence[Comment] = (),
    tags: Sequence[str] = (),
) -> OnSuccess[SourceItem]:
    """`canonical_source` is the content URL when the discussion page differs from
    the thing it points at; `language_hint` applies when detection is inconclusive."""
    canonical_url = canonicalise_url(canonical_source or url)

    # NOTE: Links go into the email as hrefs; only web links are kept.
    if not (_is_web_link(url) and _is_web_link(canonical_url)):
        return NormaliseError.INVALID_ITEM
    detected = detect_language(f"{title}\n{text}")
    language = detected

    if detected == Language.OTHER and language_hint is not None:
        language = language_hint

    cleaned_author = None if author is None or author.strip() == "" else author.strip()

    try:
        return SourceItem(
            id=item_id_for(canonical_url),
            source_id=source_id,
            kind=kind,
            url=url.strip(),
            canonical_url=canonical_url,
            title=title.strip(),
            author=cleaned_author,
            language=language,
            published_at=published_at,
            text=text,
            engagement=engagement,
            comments=tuple(comments),
            tags=tuple(tags),
        )
    except pydantic.ValidationError:
        return NormaliseError.INVALID_ITEM
