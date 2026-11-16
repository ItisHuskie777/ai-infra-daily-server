import math
from collections.abc import Sequence

from app import settings
from app.models import DigestEventPlacement
from app.models import DigestSection
from app.models import DigestStyle
from app.models import EventWithItems
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
_VOICE_KINDS = frozenset({ItemKind.COMMENTARY})
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
    community_only: bool


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


_REPORT_KINDS = frozenset({ItemKind.PRIMARY, ItemKind.MEDIA})


def title_key(title: str) -> str:
    """Letters and digits only, lower case: the same headline published again under a
    new address has the same key."""
    return "".join(character for character in title.lower() if character.isalnum())


def fresh_events(
    events: Sequence[EventWithItems],
    already_sent_item_ids: set[str],
    sent_title_keys: set[str],
) -> list[EventWithItems]:
    """Events reduced to their unsent items (FR2.2); an item whose headline was already
    sent counts as sent. An event that was partly sent before is a follow-up and
    returns only with enough new material: a new primary or media report, or several
    new items; a single repost does not bring a story back."""
    fresh: list[EventWithItems] = []

    for event in events:
        unsent = tuple(
            item
            for item in event.items
            if item.id not in already_sent_item_ids
            and title_key(item.title) not in sent_title_keys
        )

        if len(unsent) == 0:
            continue

        is_follow_up = len(unsent) < len(event.items)
        substantial = len(unsent) >= settings.FOLLOW_UP_MIN_NEW_ITEMS or any(
            item.kind in _REPORT_KINDS for item in unsent
        )

        if is_follow_up and not substantial:
            continue

        fresh.append(event.model_copy(update={"items": unsent}))

    return fresh


def _limit_community(candidates: Sequence[_Candidate], limit: int) -> list[_Candidate]:
    """Keeps at most `limit` events that only forum threads cover (Reddit, Hacker
    News), so a category is not filled with questions and opinion posts."""
    kept: list[_Candidate] = []
    community = 0

    for candidate in candidates:
        if candidate.community_only:
            if community == limit:
                continue

            community += 1

        kept.append(candidate)

    return kept


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
        community_only=all(item.kind is ItemKind.DISCUSSION for item in items),
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
    spare: int = 0,
) -> list[DigestEventPlacement]:
    """Pure assignment of ranked events to digest categories: each category gets its
    best events up to the style's per-category budget plus `spare`, so no single
    category fills the digest (FR5.6) and the editor review has room to merge and
    drop stories before `trim_to_budget`. Off-topic events below the minimum score are
    left out. Items already sent are removed first, so an event with nothing new is
    dropped entirely (FR2.2, FR5.2)."""
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
                _limit_community(queue, settings.COMMUNITY_EVENTS_PER_CATEGORY),
                limit=settings.DIGEST_EVENTS_PER_CATEGORY[style] + spare,
                pinned_thread_ids=pinned_thread_ids,
            )
        ),
        key=lambda candidate: candidate.rank,
    )

    return _numbered(
        [
            DigestEventPlacement(
                event=candidate.event,
                section=candidate.section,
                position=0,
                item_ids=tuple(item.id for item in candidate.items),
            )
            for candidate in chosen
        ]
    )


def _numbered(placements: Sequence[DigestEventPlacement]) -> list[DigestEventPlacement]:
    positions: dict[DigestSection, int] = {}
    numbered: list[DigestEventPlacement] = []

    for placement in placements:
        position = positions.get(placement.section, 0)
        positions[placement.section] = position + 1
        numbered.append(placement.model_copy(update={"position": position}))

    return numbered


def trim_to_budget(
    placements: Sequence[DigestEventPlacement], *, style: DigestStyle
) -> list[DigestEventPlacement]:
    """Cuts each category back to the style's budget, keeping the order given."""
    budget = settings.DIGEST_EVENTS_PER_CATEGORY[style]
    counts: dict[DigestSection, int] = {}
    kept: list[DigestEventPlacement] = []

    for placement in placements:
        count = counts.get(placement.section, 0)

        if count < budget:
            kept.append(placement)
            counts[placement.section] = count + 1

    return _numbered(kept)
