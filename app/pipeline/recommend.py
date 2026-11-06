from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime

from app import settings
from app.adapters.embeddings import Vector
from app.adapters.embeddings import cosine_similarity
from app.models import EventWithItems
from app.models import Interests
from app.models import Model
from app.models import RankedEvent
from app.models import Source
from app.models import Topic

_NEUTRAL_WEIGHT = 1.0
_SECONDS_PER_HOUR = 3600.0

TOPIC_LABELS: dict[Topic, str] = {
    Topic.MODELS: "Model releases",
    Topic.INFERENCE: "Inference and serving",
    Topic.HARDWARE: "Chips and data centres",
    Topic.TRAINING: "Training and fine-tuning",
    Topic.AGENTS: "Agents",
    Topic.TOOLING: "Developer tools",
    Topic.RESEARCH: "Research papers",
    Topic.OPEN_SOURCE: "Open-source models",
    Topic.POLICY: "Safety and policy",
    Topic.BUSINESS: "Industry and funding",
    Topic.PRODUCTS: "Products and applications",
}


TOPIC_DESCRIPTIONS: dict[Topic, str] = {
    Topic.MODELS: "a new model or model version, its capabilities and benchmarks",
    Topic.INFERENCE: "running models: serving, latency, quantisation, local inference",
    Topic.HARDWARE: "chips, accelerators, clusters and data centres",
    Topic.TRAINING: "training and fine-tuning methods, data and recipes",
    Topic.AGENTS: "agents, agent frameworks and how they are built or evaluated",
    Topic.TOOLING: "libraries, SDKs and developer tools",
    Topic.RESEARCH: "papers and scientific findings",
    Topic.OPEN_SOURCE: "open-weight model ecosystems and licences",
    Topic.POLICY: "safety, regulation, politics and public debate",
    Topic.BUSINESS: "companies, funding, deals, hiring and market moves",
    Topic.PRODUCTS: "consumer and enterprise products built on models",
}


class EventSignals(Model):
    """Everything the score depends on, so scoring itself is a pure function."""

    relevance: float
    source_weight: float
    topics: tuple[Topic, ...]
    distinct_sources: int
    text_length: int
    age_hours: float


def signals_for(
    event: EventWithItems,
    *,
    relevance: float,
    topics: Sequence[Topic],
    sources: Mapping[str, Source],
    now: datetime,
) -> EventSignals:
    newest = max(item.published_at for item in event.items)

    return EventSignals(
        relevance=relevance,
        source_weight=max(
            sources[item.source_id].weight
            if item.source_id in sources
            else _NEUTRAL_WEIGHT
            for item in event.items
        ),
        topics=tuple(topics),
        distinct_sources=len({item.source_id for item in event.items}),
        text_length=sum(len(item.text) for item in event.items),
        age_hours=max(0.0, (now - newest).total_seconds() / _SECONDS_PER_HOUR),
    )


def topic_affinity(topics: Sequence[Topic], weights: Mapping[Topic, float]) -> float:
    """Mean of the owner's weights for the event's topics; untagged events and topics
    never rated are neutral."""
    if len(topics) == 0:
        return _NEUTRAL_WEIGHT

    return sum(weights.get(topic, _NEUTRAL_WEIGHT) for topic in topics) / len(topics)


def coverage_boost(distinct_sources: int) -> float:
    """A story several independent sources carry matters more than one a single feed
    mentions."""
    extra = min(max(distinct_sources, 1), settings.COVERAGE_BOOST_MAX_SOURCES) - 1

    return 1.0 + settings.COVERAGE_BOOST_PER_SOURCE * extra


def freshness(age_hours: float) -> float:
    return float(0.5 ** (max(age_hours, 0.0) / settings.FRESHNESS_HALF_LIFE_HOURS))


def substance(text_length: int) -> float:
    """Holds back events with next to no body text; there is nothing to summarise."""
    if text_length < settings.THIN_CONTENT_CHARACTERS:
        return settings.THIN_CONTENT_FACTOR

    return 1.0


def score(signals: EventSignals, interests: Interests) -> float:
    return (
        signals.relevance
        * signals.source_weight
        * topic_affinity(signals.topics, interests.topic_weights)
        * coverage_boost(signals.distinct_sources)
        * freshness(signals.age_hours)
        * substance(signals.text_length)
    )


def adjust_topic_weights(
    weights: Mapping[Topic, float], topics: Sequence[Topic], step: float
) -> dict[Topic, float]:
    """Moves each topic's weight by `step`, clamped to the configured range."""
    adjusted = dict(weights)

    for topic in dict.fromkeys(topics):
        moved = adjusted.get(topic, _NEUTRAL_WEIGHT) + step
        adjusted[topic] = round(
            min(settings.TOPIC_WEIGHT_MAX, max(settings.TOPIC_WEIGHT_MIN, moved)), 3
        )

    return adjusted


def _repeats(event: RankedEvent, picked: Vector, vectors: Mapping[str, Vector]) -> bool:
    vector = vectors.get(event.id)

    return (
        vector is not None
        and cosine_similarity(vector, picked)
        >= settings.REDUNDANCY_SIMILARITY_THRESHOLD
    )


def diversify(
    ranked: Sequence[RankedEvent], vectors: Mapping[str, Vector]
) -> list[RankedEvent]:
    """Greedy re-ordering of the best candidates: each pick is the highest score left,
    where an event that repeats a story already picked counts for less. Events beyond
    the pool, and events without a vector, keep their order."""
    pool = list(ranked[: settings.DIVERSIFY_POOL_SIZE])
    redundant = [False] * len(pool)
    remaining = list(range(len(pool)))
    chosen: list[RankedEvent] = []

    while len(remaining) > 0:
        best = max(
            remaining,
            key=lambda index: (
                pool[index].relevance_score
                * (settings.REDUNDANCY_FACTOR if redundant[index] else 1.0),
                -index,
            ),
        )
        remaining.remove(best)
        chosen.append(pool[best])
        picked = vectors.get(pool[best].id)

        if picked is None:
            continue

        for index in remaining:
            redundant[index] = redundant[index] or _repeats(
                pool[index], picked, vectors
            )

    return [*chosen, *ranked[settings.DIVERSIFY_POOL_SIZE :]]
