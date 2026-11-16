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
    Topic.TOOLING: "libraries, SDKs and tools for people who build with AI",
    Topic.RESEARCH: "papers, preprints and benchmark studies, whatever their subject",
    Topic.OPEN_SOURCE: "open-weight model ecosystems and licences",
    Topic.POLICY: (
        "safety, security incidents, data leaks, regulation, lawsuits, politics "
        "and public debate"
    ),
    Topic.BUSINESS: "companies, funding, deals, hiring and market moves",
    Topic.PRODUCTS: "apps and features that end users use, built on models",
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


def diversify(
    ranked: Sequence[RankedEvent], vectors: Mapping[str, Vector]
) -> list[RankedEvent]:
    """Holds back repeats (FR5.5): walking down the ranking, an event too similar to one
    already kept tells the same story and is dropped. Events beyond the pool, and
    events without a vector, are kept as they are."""
    kept: list[RankedEvent] = []
    kept_vectors: list[Vector] = []

    for event in ranked[: settings.DIVERSIFY_POOL_SIZE]:
        vector = vectors.get(event.id)

        if vector is not None and any(
            cosine_similarity(vector, other) >= settings.REDUNDANCY_SIMILARITY_THRESHOLD
            for other in kept_vectors
        ):
            continue

        kept.append(event)

        if vector is not None:
            kept_vectors.append(vector)

    return [*kept, *ranked[settings.DIVERSIFY_POOL_SIZE :]]
