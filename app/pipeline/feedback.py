import re
from collections.abc import Sequence
from typing import Literal
from typing import override

from app import logging
from app import settings
from app.context import AbstractContext
from app.models import EventWithItems
from app.models import Feedback
from app.models import FeedbackSignal
from app.models import Interests
from app.models import Model
from app.models import Thread
from app.models import Topic
from app.pipeline import recommend
from app.result import OnSuccess
from app.result import ServiceError
from app.result import is_error

logger = logging.get_logger(__name__)

_WEIGHT_STEP_UP = 0.1
_WEIGHT_STEP_DOWN = 0.15
_WEIGHT_MAX = 2.0
_WEIGHT_MIN = 0.2
_MAX_KEYWORDS_PER_FEEDBACK = 3
_MIN_KEYWORD_LENGTH = 4
_TOKEN_SEPARATOR = re.compile(r"[\W_]+")
_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "against",
        "also",
        "announces",
        "announced",
        "because",
        "been",
        "before",
        "being",
        "between",
        "could",
        "does",
        "down",
        "during",
        "each",
        "from",
        "have",
        "here",
        "into",
        "introduces",
        "just",
        "launches",
        "launched",
        "like",
        "made",
        "make",
        "more",
        "most",
        "much",
        "only",
        "other",
        "over",
        "releases",
        "released",
        "report",
        "said",
        "says",
        "should",
        "some",
        "still",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "today",
        "under",
        "unveils",
        "using",
        "very",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "without",
        "would",
        "your",
    }
)

type _Direction = Literal["more", "less"]


class FeedbackError(ServiceError):
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    ALREADY_APPLIED = "already_applied"

    @override
    def service(self) -> str:
        return "feedback"


class FeedbackOutcome(Model):
    interests: Interests
    source_weight_changes: tuple[tuple[str, float], ...]


class _Targets(Model):
    source_ids: tuple[str, ...]
    title: str | None
    topics: tuple[Topic, ...] = ()


def extract_keywords(title: str, existing: Sequence[str]) -> list[str]:
    known = {keyword.lower() for keyword in existing}
    keywords: list[str] = []

    for token in _TOKEN_SEPARATOR.split(title.lower()):
        if len(token) < _MIN_KEYWORD_LENGTH or token in _STOPWORDS or token in known:
            continue

        known.add(token)
        keywords.append(token)

        if len(keywords) == _MAX_KEYWORDS_PER_FEEDBACK:
            break

    return keywords


def pinned_query_for(title: str) -> str:
    """The few distinctive words of a thread's title; later events match on them."""
    return " ".join(extract_keywords(title, ()))


def _clamp_weight(weight: float) -> float:
    return min(_WEIGHT_MAX, max(_WEIGHT_MIN, round(weight, 3)))


def _without_keywords(
    current: Sequence[str], keywords: Sequence[str]
) -> tuple[str, ...]:
    removed = set(keywords)

    return tuple(keyword for keyword in current if keyword.lower() not in removed)


async def _find_event(ctx: AbstractContext, event_id: str) -> OnSuccess[EventWithItems]:
    event = await ctx.events.find_by_id(event_id)

    if is_error(event):
        return FeedbackError.NOT_FOUND

    return event


async def _placed_topics(ctx: AbstractContext, feedback: Feedback) -> tuple[Topic, ...]:
    """Topics the ranking gave the event in the digest the feedback came from."""
    if feedback.digest_id is None:
        return ()

    for placement in await ctx.digests.list_placements(feedback.digest_id):
        if placement.event_id == feedback.event_id:
            return placement.topics

    return ()


async def _targets_for(ctx: AbstractContext, feedback: Feedback) -> OnSuccess[_Targets]:
    if feedback.event_id is not None:
        event = await _find_event(ctx, feedback.event_id)

        if is_error(event):
            return event

        source_ids = tuple(dict.fromkeys(item.source_id for item in event.items))

        return _Targets(
            source_ids=source_ids,
            title=event.title,
            topics=await _placed_topics(ctx, feedback),
        )

    if feedback.source_id is not None:
        return _Targets(source_ids=(feedback.source_id,), title=None)

    return FeedbackError.INVALID


async def _adjust_weights(
    ctx: AbstractContext, source_ids: Sequence[str], delta: float
) -> list[tuple[str, float]]:
    changes: list[tuple[str, float]] = []

    for source_id in source_ids:
        source = await ctx.sources.find_by_id(source_id)

        if is_error(source):
            logger.warning(
                "Skipping weight change for unknown source.",
                extra={"source_id": source_id},
            )

            continue

        weight = _clamp_weight(source.weight + delta)

        if weight == source.weight:
            continue

        await ctx.sources.set_weight(source_id, weight=weight, now=ctx.clock.now())
        changes.append((source_id, weight))

    return changes


def _with_preference(
    interests: Interests, title: str | None, direction: _Direction
) -> Interests:
    if direction == "more":
        include = extract_keywords(title or "", interests.keywords_include)

        return interests.model_copy(
            update={
                "keywords_include": (*interests.keywords_include, *include),
                "keywords_exclude": _without_keywords(
                    interests.keywords_exclude, include
                ),
            }
        )

    exclude = extract_keywords(title or "", interests.keywords_exclude)

    return interests.model_copy(
        update={
            "keywords_exclude": (*interests.keywords_exclude, *exclude),
            "keywords_include": _without_keywords(interests.keywords_include, exclude),
        }
    )


async def _apply_preference(
    ctx: AbstractContext,
    feedback: Feedback,
    interests: Interests,
    direction: _Direction,
) -> OnSuccess[FeedbackOutcome]:
    targets = await _targets_for(ctx, feedback)

    if is_error(targets):
        return targets

    delta = _WEIGHT_STEP_UP if direction == "more" else -_WEIGHT_STEP_DOWN
    changes = await _adjust_weights(ctx, targets.source_ids, delta)
    step = (
        settings.TOPIC_WEIGHT_STEP
        if direction == "more"
        else -settings.TOPIC_WEIGHT_STEP
    )
    preferred = _with_preference(interests, targets.title, direction)

    return FeedbackOutcome(
        interests=preferred.model_copy(
            update={
                "topic_weights": recommend.adjust_topic_weights(
                    preferred.topic_weights, targets.topics, step
                )
            }
        ),
        source_weight_changes=tuple(changes),
    )


async def _mute_source(
    ctx: AbstractContext, feedback: Feedback, interests: Interests
) -> OnSuccess[FeedbackOutcome]:
    if feedback.source_id is None:
        return FeedbackError.INVALID

    disabled = await ctx.sources.set_enabled(
        feedback.source_id, enabled=False, now=ctx.clock.now()
    )

    if is_error(disabled):
        return FeedbackError.NOT_FOUND

    blocked = interests.blocked_sources

    if feedback.source_id not in blocked:
        blocked = (*blocked, feedback.source_id)

    return FeedbackOutcome(
        interests=interests.model_copy(update={"blocked_sources": blocked}),
        source_weight_changes=(),
    )


async def _find_thread(ctx: AbstractContext, feedback: Feedback) -> OnSuccess[Thread]:
    if feedback.event_id is None:
        return FeedbackError.INVALID

    event = await _find_event(ctx, feedback.event_id)

    if is_error(event):
        return event

    if event.thread_id is None:
        return FeedbackError.NOT_FOUND

    thread = await ctx.threads.find_by_id(event.thread_id)

    if is_error(thread):
        return FeedbackError.NOT_FOUND

    return thread


async def _pin_thread(
    ctx: AbstractContext, feedback: Feedback, interests: Interests
) -> OnSuccess[FeedbackOutcome]:
    thread = await _find_thread(ctx, feedback)

    if is_error(thread):
        return thread

    pinned = await ctx.threads.set_pinned(
        thread.id, pinned=True, query=pinned_query_for(thread.title)
    )

    if is_error(pinned):
        return FeedbackError.NOT_FOUND

    logger.info("Thread pinned.", extra={"thread_id": thread.id})

    return FeedbackOutcome(interests=interests, source_weight_changes=())


async def apply_feedback(
    ctx: AbstractContext, feedback: Feedback, interests: Interests
) -> OnSuccess[FeedbackOutcome]:
    """Records the feedback first so a later failure never loses the signal (FR7.2);
    a link that already acted changes nothing. The caller persists the interests."""
    if is_error(await ctx.digests.record_feedback(feedback)):
        return FeedbackError.ALREADY_APPLIED

    match feedback.signal:
        case FeedbackSignal.MORE_LIKE_THIS:
            return await _apply_preference(ctx, feedback, interests, "more")
        case FeedbackSignal.LESS_LIKE_THIS:
            return await _apply_preference(ctx, feedback, interests, "less")
        case FeedbackSignal.MUTE_SOURCE:
            return await _mute_source(ctx, feedback, interests)
        case FeedbackSignal.PIN_THREAD:
            return await _pin_thread(ctx, feedback, interests)
