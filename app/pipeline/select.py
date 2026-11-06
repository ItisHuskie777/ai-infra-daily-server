import math
from collections.abc import Sequence

from app import settings
from app.models import DigestEventPlacement
from app.models import DigestSection
from app.models import DigestStyle
from app.models import ItemKind
from app.models import Model
from app.models import RankedEvent
from app.models import StoredItem
from app.models import Topic

_KIND_ORDER = {
    ItemKind.PRIMARY: 0,
    ItemKind.MEDIA: 1,
    ItemKind.COMMENTARY: 2,
    ItemKind.DISCUSSION: 3,
}
_VOICE_KINDS = frozenset({ItemKind.COMMENTARY, ItemKind.DISCUSSION})
_SECTION_OF_TOPIC: dict[Topic, DigestSection] = {
    Topic.MODELS: DigestSection.MODELS,
    Topic.OPEN_SOURCE: DigestSection.MODELS,
    Topic.RESEARCH: DigestSection.RESEARCH,
    Topic.TRAINING: DigestSection.RESEARCH,
    Topic.INFERENCE: DigestSection.INFRASTRUCTURE,
    Topic.HARDWARE: DigestSection.INFRASTRUCTURE,
    Topic.AGENTS: DigestSection.AGENTS,
    Topic.TOOLING: DigestSection.AGENTS,
    Topic.BUSINESS: DigestSection.BUSINESS,
    Topic.PRODUCTS: DigestSection.BUSINESS,
    Topic.POLICY: DigestSection.POLICY,
}


class _Candidate(Model):
    rank: int
    event: RankedEvent
    items: tuple[StoredItem, ...]
    section: DigestSection
    has_voice: bool


def engagement_value(item: StoredItem) -> int:
    engagement = item.engagement

    if engagement is None:
        return 0

    return (
        (engagement.likes or 0)
        + (engagement.score or 0)
        + (engagement.comments or 0)
        + (engagement.shares or 0)
    )


def _item_order(item: StoredItem) -> tuple[int, int, float]:
    return (
        _KIND_ORDER[item.kind],
        -engagement_value(item),
        item.published_at.timestamp(),
    )


def section_for(topics: Sequence[Topic]) -> DigestSection:
    """The category of an event is that of its main (first) topic."""
    if len(topics) == 0:
        return DigestSection.OTHER

    return _SECTION_OF_TOPIC[topics[0]]


def _by_section(
    candidates: Sequence[_Candidate],
) -> dict[DigestSection, list[_Candidate]]:
    queues: dict[DigestSection, list[_Candidate]] = {}

    for candidate in candidates:
        queues.setdefault(candidate.section, []).append(candidate)

    return queues


def _to_candidate(
    event: RankedEvent,
    rank: int,
    *,
    already_sent_item_ids: set[str],
) -> _Candidate | None:
    items = sorted(
        (item for item in event.items if item.id not in already_sent_item_ids),
        key=_item_order,
    )

    if len(items) == 0:
        return None

    return _Candidate(
        rank=rank,
        event=event,
        items=tuple(items),
        section=section_for(event.topics),
        has_voice=any(item.kind in _VOICE_KINDS for item in items),
    )


def _is_pinned(candidate: _Candidate, pinned_thread_ids: set[str]) -> bool:
    thread = candidate.event.thread

    return thread is not None and (thread.pinned or thread.id in pinned_thread_ids)


def _pick_with_voice_share(
    candidates: Sequence[_Candidate], *, limit: int, pinned_thread_ids: set[str]
) -> list[_Candidate]:
    """The first `limit` candidates in the order given, with pinned threads always
    kept (FR4.2) and voice events swapped in from below the cut until their share is
    met (FR5.2); the result is back in rank order."""
    pinned = [c for c in candidates if _is_pinned(c, pinned_thread_ids)]
    others = [c for c in candidates if not _is_pinned(c, pinned_thread_ids)]
    budget = max(0, limit - len(pinned))
    selected = others[:budget]
    reserve = [candidate for candidate in others[budget:] if candidate.has_voice]
    minimum_voice = math.ceil(limit * settings.INDIVIDUAL_VOICE_MIN_SHARE)
    voice_count = sum(1 for candidate in [*pinned, *selected] if candidate.has_voice)

    while voice_count < minimum_voice and len(reserve) > 0:
        replaceable = [
            index for index, candidate in enumerate(selected) if not candidate.has_voice
        ]

        if len(replaceable) == 0:
            break

        selected[replaceable[-1]] = reserve.pop(0)
        voice_count += 1

    return sorted([*pinned, *selected], key=lambda candidate: candidate.rank)


def select_for_digest(
    ranked: Sequence[RankedEvent],
    *,
    style: DigestStyle,
    already_sent_item_ids: set[str],
    pinned_thread_ids: set[str],
) -> list[DigestEventPlacement]:
    """Pure assignment of ranked events to digest categories: each category gets its
    best events up to the style's per-category budget, so no single category fills
    the digest (FR5.6); off-topic events below the minimum score are left out. Items
    already sent are removed first, so an event with nothing new is dropped entirely
    (FR2.2, FR5.2)."""
    candidates = [
        candidate
        for rank, event in enumerate(ranked)
        if event.relevance_score >= settings.MIN_RECOMMENDATION_SCORE
        and (
            candidate := _to_candidate(
                event, rank, already_sent_item_ids=already_sent_item_ids
            )
        )
        is not None
    ]

    chosen = sorted(
        (
            candidate
            for queue in _by_section(candidates).values()
            for candidate in _pick_with_voice_share(
                queue,
                limit=settings.DIGEST_EVENTS_PER_CATEGORY[style],
                pinned_thread_ids=pinned_thread_ids,
            )
        ),
        key=lambda candidate: candidate.rank,
    )

    positions: dict[DigestSection, int] = {}
    placements: list[DigestEventPlacement] = []

    for candidate in chosen:
        position = positions.get(candidate.section, 0)
        positions[candidate.section] = position + 1

        placements.append(
            DigestEventPlacement(
                event=candidate.event,
                section=candidate.section,
                position=position,
                item_ids=tuple(item.id for item in candidate.items),
            )
        )

    return placements
