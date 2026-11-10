import math
from collections.abc import Mapping
from collections.abc import Sequence

from pydantic import BaseModel
from pydantic import Field

from app import logging
from app import settings
from app.adapters.embeddings import Vector
from app.context import AbstractContext
from app.models import EventWithItems
from app.models import Interests
from app.models import RankedEvent
from app.models import Source
from app.models import Thread
from app.models import Topic
from app.pipeline import _untrusted
from app.pipeline import recommend
from app.pipeline.select import engagement_value
from app.result import OnSuccess
from app.result import is_error

logger = logging.get_logger(__name__)

_TOPIC_LINES = tuple(recommend.TOPIC_DESCRIPTIONS.items())

_RANK_BATCH_SIZE = 15
_ITEMS_PER_EVENT_IN_PROMPT = 5
_SUMMARY_LIMIT = 600
_RANK_SYSTEM_PROMPT = (
    "You rank AI-news events for one reader. Score how much each event matches the "
    "reader's interest profile, from 0 (irrelevant) to 1 (must read), and give a "
    "one-sentence rationale. Use the whole range: most events deserve less than 0.6 "
    "and only a few per batch more than 0.8. Where the profile is sparse, judge by "
    "how much the event matters to a practitioner who follows AI models, "
    "infrastructure and tooling. Also tag each event with one or two topics from the "
    "list given, using the topic keys exactly; put first the topic that says what "
    "kind of news the event is, since it decides the event's category. "
    + _untrusted.RULE
)
_MAX_TOPICS_PER_EVENT = 2
_TOPIC_VALUES = frozenset(topic.value for topic in Topic)
_HEURISTIC_BASE = 0.3
_HEURISTIC_PER_MATCH = 0.15
_HEURISTIC_PER_EXCLUDED = 0.25
_HEURISTIC_FOLLOWED_AUTHOR = 0.2
_HEURISTIC_PER_ENGAGEMENT_DECADE = 0.1


class _EventScore(BaseModel):
    event_id: str = Field(min_length=1)
    relevance: float = Field(ge=0, le=1)
    rationale: str
    # NOTE: Plain strings, so one unknown tag does not invalidate the whole sheet.
    topics: list[str] = Field(default_factory=list)


class _ScoreSheet(BaseModel):
    scores: list[_EventScore]


class _Score(BaseModel):
    relevance: float
    rationale: str
    topics: tuple[Topic, ...] = ()


def _describe_list(label: str, values: Sequence[str]) -> str:
    return f"{label}: {', '.join(values) if len(values) > 0 else '(none)'}"


def _describe_event(event: EventWithItems, sources: Mapping[str, Source]) -> str:
    lines = [
        f"Event {event.id}: {_untrusted.fence(event.title)}",
        f"  Summary: {_untrusted.fence(event.summary[:_SUMMARY_LIMIT])}",
    ]

    for item in event.items[:_ITEMS_PER_EVENT_IN_PROMPT]:
        source = sources[item.source_id].name if item.source_id in sources else None
        author = "" if item.author is None else f", {item.author}"
        lines.append(
            f"  - [{item.kind}] {_untrusted.fence(item.title)} "
            f"({source or item.source_id}{_untrusted.fence(author)})"
        )

    return "\n".join(lines)


def _build_rank_prompt(
    interests: Interests,
    events: Sequence[EventWithItems],
    sources: Mapping[str, Source],
) -> str:
    lines = [
        "Reader profile:",
        _describe_list("Topics", interests.topics),
        _describe_list("Keywords to favour", interests.keywords_include),
        _describe_list("Keywords to avoid", interests.keywords_exclude),
        _describe_list("Followed authors", interests.followed_authors),
        _describe_list("Blocked sources", interests.blocked_sources),
        f"Technical depth: {interests.technical_depth}",
        "",
        "Topics (key: meaning):",
        *(f"  {topic.value}: {label}" for topic, label in _TOPIC_LINES),
        "",
        "Events:",
        *(_describe_event(event, sources) for event in events),
        "",
        "Return one score for every event id listed above, as `event_id`.",
    ]

    return "\n".join(lines)


def _known_topics(values: Sequence[str]) -> tuple[Topic, ...]:
    known = dict.fromkeys(
        Topic(value.strip().lower())
        for value in values
        if value.strip().lower() in _TOPIC_VALUES
    )

    return tuple(known)[:_MAX_TOPICS_PER_EVENT]


def _blocked_source_ids(interests: Interests, sources: Sequence[Source]) -> set[str]:
    blocked = {value.lower() for value in interests.blocked_sources}

    return {
        source.id
        for source in sources
        if source.id.lower() in blocked or source.name.lower() in blocked
    }


def _count_matches(text: str, keywords: Sequence[str]) -> int:
    return sum(1 for keyword in keywords if keyword.lower() in text)


def _heuristic_score(event: EventWithItems, interests: Interests) -> _Score:
    """Keyword and engagement based stand-in for when the model is unavailable."""
    text = "\n".join(
        [event.title, event.summary, *(item.title for item in event.items)]
    ).lower()

    matches = _count_matches(text, [*interests.topics, *interests.keywords_include])
    excluded = _count_matches(text, interests.keywords_exclude)
    followed = {author.lower() for author in interests.followed_authors}

    has_followed_author = any(
        item.author is not None and item.author.lower() in followed
        for item in event.items
    )

    engagement = sum(engagement_value(item) for item in event.items)

    relevance = (
        _HEURISTIC_BASE
        + _HEURISTIC_PER_MATCH * matches
        - _HEURISTIC_PER_EXCLUDED * excluded
        + (_HEURISTIC_FOLLOWED_AUTHOR if has_followed_author else 0.0)
        + _HEURISTIC_PER_ENGAGEMENT_DECADE * math.log10(1 + engagement)
    )

    return _Score(
        relevance=min(1.0, max(0.0, relevance)),
        rationale=(
            f"Heuristic score (model unavailable): {matches} keyword matches, "
            f"{excluded} excluded keywords, engagement {engagement}."
        ),
    )


async def _score_batch(
    ctx: AbstractContext,
    interests: Interests,
    events: Sequence[EventWithItems],
    sources: Mapping[str, Source],
) -> dict[str, _Score]:
    generated = await ctx.llm.complete_json(
        system=_RANK_SYSTEM_PROMPT,
        user=_build_rank_prompt(interests, events, sources),
        schema=_ScoreSheet,
        temperature=0.0,
    )

    if is_error(generated):
        logger.warning(
            "Relevance scoring failed; falling back to heuristic scores.",
            extra={"error": str(generated), "events": len(events)},
        )

        return {event.id: _heuristic_score(event, interests) for event in events}

    scores = {
        score.event_id: _Score(
            relevance=score.relevance,
            rationale=score.rationale,
            topics=_known_topics(score.topics),
        )
        for score in generated.scores
    }

    for event in events:
        if event.id not in scores:
            logger.warning(
                "Model returned no score for event; using the heuristic score.",
                extra={"event_id": event.id},
            )
            scores[event.id] = _heuristic_score(event, interests)

    return scores


async def _load_threads(
    ctx: AbstractContext, events: Sequence[EventWithItems]
) -> dict[str, Thread]:
    threads: dict[str, Thread] = {}

    for thread_id in {
        event.thread_id for event in events if event.thread_id is not None
    }:
        found = await ctx.threads.find_by_id(thread_id)

        if is_error(found):
            logger.warning(
                "Event refers to a missing thread.", extra={"thread_id": thread_id}
            )

            continue

        threads[thread_id] = found

    return threads


async def _centroid_vectors(
    ctx: AbstractContext, events: Sequence[RankedEvent]
) -> dict[str, Vector]:
    vectors: dict[str, Vector] = {}

    for event in events:
        if event.centroid_item_id is None:
            continue

        vector = await ctx.items.find_embedding(event.centroid_item_id)

        if vector is not None:
            vectors[event.id] = vector

    return vectors


async def rank_events(
    ctx: AbstractContext,
    interests: Interests,
    sources: Sequence[Source],
    events: Sequence[EventWithItems],
) -> OnSuccess[list[RankedEvent]]:
    """Scores events against the interest profile in model batches, turns each score
    into a recommendation score (source weight, topic taste, coverage, freshness,
    substance), holds back repeats of a story already ranked higher and drops events
    covered only by blocked sources (FR5.1). A model failure degrades to heuristic
    scores."""
    sources_by_id = {source.id: source for source in sources}
    blocked = _blocked_source_ids(interests, sources)
    eligible = [
        event
        for event in events
        if len(event.items) > 0
        and any(item.source_id not in blocked for item in event.items)
    ]

    if len(eligible) == 0:
        return []

    scores: dict[str, _Score] = {}

    for start in range(0, len(eligible), _RANK_BATCH_SIZE):
        batch = eligible[start : start + _RANK_BATCH_SIZE]
        scores.update(await _score_batch(ctx, interests, batch, sources_by_id))

    threads = await _load_threads(ctx, eligible)
    now = ctx.clock.now()

    ranked = [
        RankedEvent(
            **event.model_dump(),
            relevance_score=recommend.score(
                recommend.signals_for(
                    event,
                    relevance=scores[event.id].relevance,
                    topics=scores[event.id].topics,
                    sources=sources_by_id,
                    now=now,
                ),
                interests,
            ),
            rationale=scores[event.id].rationale,
            thread=None if event.thread_id is None else threads.get(event.thread_id),
            topics=scores[event.id].topics,
        )
        for event in eligible
    ]

    ranked.sort(
        key=lambda event: (
            -event.relevance_score,
            -event.updated_at.timestamp(),
            event.id,
        )
    )

    pool = ranked[: settings.DIVERSIFY_POOL_SIZE]
    ranked = recommend.diversify(ranked, await _centroid_vectors(ctx, pool))
    logger.info("Ranked events.", extra={"events": len(events), "ranked": len(ranked)})

    return ranked
