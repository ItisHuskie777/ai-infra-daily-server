from datetime import timedelta

import numpy

from app import logging
from app import settings
from app.adapters.embeddings import Vector
from app.context import AbstractContext
from app.models import Event
from app.models import EventWithItems
from app.models import Model
from app.models import SourceCategory
from app.models import StoredItem
from app.repositories.items import EmbeddedItem
from app.result import is_error

logger = logging.get_logger(__name__)

_PAPER_ORIGIN = "\x00papers"


class ClusterOutcome(Model):
    created: tuple[str, ...]
    updated: tuple[str, ...]


class _Placement(Model):
    event_id: str
    score: float


class _Window:
    """Every embedded item of the event window as one matrix, with the event each row
    belongs to; rows are assigned as the run places items."""

    __slots__ = ("event_of", "items", "matrix", "origins")

    def __init__(self, embedded: list[EmbeddedItem], paper_sources: set[str]) -> None:
        self.items = [entry.item for entry in embedded]
        self.event_of: list[str | None] = [entry.item.event_id for entry in embedded]
        # NOTE: All paper feeds count as one origin: cross-listed abstracts of one field
        # resemble each other as much as two posts of a single blog do.
        self.origins = numpy.array(
            [
                _PAPER_ORIGIN if item.source_id in paper_sources else item.source_id
                for item in self.items
            ],
            dtype=numpy.str_,
        )

        self.matrix: Vector = (
            numpy.stack([entry.vector for entry in embedded])
            if len(embedded) > 0
            else numpy.zeros((0, 0), dtype=numpy.float32)
        )

    def pending_rows(self) -> list[int]:
        rows = [row for row, event_id in enumerate(self.event_of) if event_id is None]
        rows.sort(key=lambda row: (self.items[row].published_at, self.items[row].id))

        return rows

    def nearest_event(self, row: int) -> _Placement | None:
        """The event of the most similar already placed item that clears the threshold
        for its origin; items of one origin must be closer to count as one event."""
        scores = self.matrix @ self.matrix[row]
        thresholds = numpy.where(
            self.origins == self.origins[row],
            settings.SAME_ORIGIN_SIMILARITY_THRESHOLD,
            settings.EVENT_SIMILARITY_THRESHOLD,
        )
        eligible = numpy.fromiter(
            (event_id is not None for event_id in self.event_of),
            dtype=numpy.bool_,
            count=len(self.event_of),
        )
        eligible &= scores >= thresholds
        eligible[row] = False

        if not eligible.any():
            return None

        scores[~eligible] = -numpy.inf
        best = int(numpy.argmax(scores))
        event_id = self.event_of[best]

        if event_id is None:
            return None

        return _Placement(event_id=event_id, score=float(scores[best]))


class _RunState:
    __slots__ = ("created", "events", "members", "near_duplicates", "updated")

    def __init__(self) -> None:
        self.events: dict[str, Event] = {}
        self.members: dict[str, list[str]] = {}
        self.created: list[str] = []
        self.updated: list[str] = []
        self.near_duplicates = 0


def event_id_for(item_id: str) -> str:
    """Deterministic so a retried run converges on the same event instead of a twin."""
    return f"evt-{item_id[:32]}"


def _to_event(event: EventWithItems) -> Event:
    return Event.model_validate(event.model_dump(exclude={"items"}))


async def _load_event(
    ctx: AbstractContext, state: _RunState, event_id: str
) -> Event | None:
    cached = state.events.get(event_id)

    if cached is not None:
        return cached

    found = await ctx.events.find_by_id(event_id)

    if is_error(found):
        return None

    event = _to_event(found)
    state.events[event_id] = event

    return event


async def _join_event(
    ctx: AbstractContext, state: _RunState, item: StoredItem, event_id: str
) -> bool:
    event = await _load_event(ctx, state, event_id)

    if event is None:
        logger.warning(
            "Item refers to a missing event; placing it afresh.",
            extra={"item_id": item.id, "event_id": event_id},
        )

        return False

    state.members.setdefault(event_id, []).append(item.id)

    if event_id not in state.created and event_id not in state.updated:
        state.updated.append(event_id)

    return True


async def _create_event_for(
    ctx: AbstractContext, state: _RunState, item: StoredItem
) -> str:
    event_id = event_id_for(item.id)
    existing = await _load_event(ctx, state, event_id)

    # NOTE: An existing row means an earlier attempt created the event; join it.
    if existing is not None:
        await _join_event(ctx, state, item, event_id)

        return event_id

    now = ctx.clock.now()

    state.events[event_id] = Event(
        id=event_id,
        thread_id=None,
        title=item.title,
        summary="",
        created_at=now,
        updated_at=now,
        centroid_item_id=item.id,
    )

    state.created.append(event_id)
    state.members[event_id] = [item.id]

    return event_id


async def _place_row(
    ctx: AbstractContext, state: _RunState, window: _Window, row: int
) -> None:
    item = window.items[row]
    nearest = window.nearest_event(row)

    if nearest is not None and await _join_event(ctx, state, item, nearest.event_id):
        window.event_of[row] = nearest.event_id

        if nearest.score >= settings.DUPLICATE_SIMILARITY_THRESHOLD:
            state.near_duplicates += 1
            logger.info(
                "Item is a near-duplicate of an event member (FR2.3).",
                extra={
                    "item_id": item.id,
                    "event_id": nearest.event_id,
                    "score": round(nearest.score, 4),
                },
            )
        else:
            logger.debug(
                "Assigned item to event.",
                extra={
                    "item_id": item.id,
                    "event_id": nearest.event_id,
                    "score": round(nearest.score, 4),
                },
            )

        return

    window.event_of[row] = await _create_event_for(ctx, state, item)


async def _persist(ctx: AbstractContext, state: _RunState) -> None:
    now = ctx.clock.now()

    for event_id in state.created:
        await ctx.events.create(state.events[event_id])

    for event_id in state.updated:
        touched = state.events[event_id].model_copy(update={"updated_at": now})
        state.events[event_id] = touched
        await ctx.events.update(touched)

    for event_id, item_ids in state.members.items():
        await ctx.items.assign_event(item_ids, event_id)


async def cluster_recent_items(ctx: AbstractContext) -> ClusterOutcome:
    """Groups every unclustered embedded item of the event window into events by
    vector similarity (FR3.1); the event keeps its centroid item's title until it is
    summarised."""
    window_start = ctx.clock.now() - timedelta(hours=settings.EVENT_WINDOW_HOURS)
    paper_sources = {
        source.id
        for source in await ctx.sources.list_all()
        if source.category == SourceCategory.PAPER
    }
    window = _Window(await ctx.items.list_embedded_since(window_start), paper_sources)
    state = _RunState()
    rows = window.pending_rows()

    for row in rows:
        await _place_row(ctx, state, window, row)

    await _persist(ctx, state)
    logger.info(
        "Clustered recent items.",
        extra={
            "items": len(rows),
            "events_created": len(state.created),
            "events_updated": len(state.updated),
            "near_duplicates": state.near_duplicates,
        },
    )

    return ClusterOutcome(created=tuple(state.created), updated=tuple(state.updated))
