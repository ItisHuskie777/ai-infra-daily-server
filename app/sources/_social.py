import math
import re
from datetime import UTC
from datetime import datetime

from app.adapters.browser import RenderedPage
from app.models import Model
from app.sources import normalise

_COUNT = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmMwW万亿]?)")
_COUNT_MULTIPLIERS = {
    "k": 1_000,
    "m": 1_000_000,
    "w": 10_000,
    "万": 10_000,
    "亿": 100_000_000,
}
_ARIA_COUNT = re.compile(
    r"(\d[\d,.]*\s*[kKmM]?)\s*"
    r"(replies|reply|reposts|repost|retweets|retweet|likes|like)\b",
    re.IGNORECASE,
)
# Markers that appear on login, captcha and "verify you are human" pages of both sites.
_LOGIN_WALL_MARKERS = (
    "登录后查看",
    "扫码登录",
    "手机号登录",
    "安全验证",
    "滑动验证",
    "请完成验证",
    "login-container",
    "captcha",
    "sign in to x",
    "log in to x",
    "sign in to twitter",
    "log in to twitter",
    'data-testid="loginbutton"',
    'data-testid="login"',
    "/i/flow/login",
    "/i/flow/signup",
)
_LOGIN_URL_MARKERS = ("/login", "/i/flow/", "/captcha", "/website-login")
# Epoch milliseconds up to the year 2100; anything else is not a timestamp.
_MAX_EPOCH_MILLISECONDS = 4_102_444_800_000

type Count = str | int | float | None


class AriaCounts(Model):
    replies: int | None = None
    reposts: int | None = None
    likes: int | None = None


def optional_string(value: str | None) -> str | None:
    if value is None:
        return None

    trimmed = value.strip()

    return None if trimmed == "" else trimmed


def looks_like_login_wall(html: str) -> bool:
    lowered = html.lower()

    return any(marker.lower() in lowered for marker in _LOGIN_WALL_MARKERS)


def looks_like_login_url(url: str) -> bool:
    lowered = url.lower()

    return any(marker in lowered for marker in _LOGIN_URL_MARKERS)


def is_blocked(page: RenderedPage, *, has_content: bool) -> bool:
    """The wall markers are ordinary words a post may contain, so the page text only
    counts when nothing was extracted from the page."""
    if looks_like_login_url(page.final_url):
        return True

    return not has_content and looks_like_login_wall(page.html)


def parse_count(raw: Count) -> int | None:
    """Parses display counts such as `1,234`, `1.2K` or `3.4万`; no digits is None."""
    if isinstance(raw, bool):
        return None

    if isinstance(raw, int | float):
        return round(raw) if math.isfinite(raw) and raw >= 0 else None

    if raw is None:
        return None

    match = _COUNT.search(raw.replace(",", ""))

    if match is None:
        return None

    multiplier = _COUNT_MULTIPLIERS.get(match.group(2).lower(), 1)

    return round(float(match.group(1)) * multiplier)


def parse_aria_counts(label: str) -> AriaCounts:
    """Reads counts out of X `aria-label`s such as `12 replies, 3 reposts, 45 likes`."""
    counts: dict[str, int] = {}

    for match in _ARIA_COUNT.finditer(label):
        value = parse_count(match.group(1))
        unit = match.group(2).lower()

        if value is None:
            continue

        if unit.startswith("repl"):
            counts["replies"] = value
        elif unit.startswith(("repo", "retw")):
            counts["reposts"] = value
        else:
            counts["likes"] = value

    return AriaCounts(**counts)


def to_datetime(value: Count, *, fallback: datetime) -> datetime:
    """Numbers are epoch milliseconds (the sites' `time` fields); strings are dates."""
    if isinstance(value, bool) or value is None:
        return fallback

    if isinstance(value, int | float):
        if not 0 <= value < _MAX_EPOCH_MILLISECONDS:
            return fallback

        return datetime.fromtimestamp(value / 1000, tz=UTC)

    return normalise.parse_datetime(value, fallback=fallback)
