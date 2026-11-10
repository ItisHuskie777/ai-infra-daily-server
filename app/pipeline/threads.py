import re
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

from app import logging
from app import settings
from app.adapters.embeddings import Vector
from app.adapters.embeddings import cosine_similarity
from app.context import AbstractContext
from app.models import Event
from app.models import Thread
from app.pipeline.embed import build_embedding_text
from app.result import is_error

logger = logging.get_logger(__name__)

_QUERY_SEPARATOR = re.compile(r"[\s,]+")


class _Candidate:
    __slots__ = ("thread_id", "vector")

    def __init__(self, thread_id: str, vector: Vector) -> None:
        self.thread_id = thread_id
        self.vector = vector


class _LinkState:
    __slots__ = ("candidates", "continuations", "linked", "threads", "zone")

    def __init__(self, zone: ZoneInfo) -> None:
        self.zone = zone
        self.threads: dict[str, Thread] = {}
        self.candidates: list[_Candidate] = []
        self.linked = 0
        self.continuations = 0


def thread_id_for(event_id: str) -> str:
    return f"thr-{event_id.removeprefix('evt-')}"


def matches_pinned_query(query: str, text: str) -> bool:
    haystack = text.lower()
    keywords = [word for word in _QUERY_SEPARATOR.split(query.lower()) if word != ""]

    return len(keywords) > 0 and all(keyword in haystack for keyword in keywords)


def touch_thread(thread: Thread, event: Event, *, zone: ZoneInfo) -> Thread:
    """Moves the thread's last activity to the event's time; a changed local date counts
    as a new day of the story (FR4.1)."""
    is_new_day = _local_date(event.updated_at, zone) != _local_date(
        thread.last_seen_at, zone
    )

    return thread.model_copy(
        update={
            "last_seen_at": max(event.updated_at, thread.last_seen_at),
            "day_count": thread.day_count + 1 if is_new_day else thread.day_count,
        }
    )


def _local_date(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).date().isoformat()


def _event_text(event: Event) -> str:
    return f"{event.title}\n{event.summary}"


async def _load_events(ctx: AbstractContext, event_ids: Sequence[str]) -> list[Event]:
    events: list[Event] = []

    for event_id in dict.fromkeys(event_ids):
        found = await ctx.events.find_by_id(event_id)

        if is_error(found):
            logger.warning("Event to link was not found.", extra={"event_id": event_id})

            continue

        events.append(Event.model_validate(found.model_dump(exclude={"items"})))

    return events


async def _load_thread(
    ctx: AbstractContext, state: _LinkState, thread_id: str
) -> Thread | None:
    cached = state.threads.get(thread_id)

    if cached is not None:
        return cached

    found = await ctx.threads.find_by_id(thread_id)

    if is_error(found):
        return None

    state.threads[thread_id] = found

    return found


async def _attach(
    ctx: AbstractContext,
    state: _LinkState,
    event: Event,
    thread: Thread,
    *,
    continuation: bool,
) -> None:
    touched = touch_thread(thread, event, zone=state.zone)
    await ctx.threads.update(touched)
    state.threads[touched.id] = touched

    if event.thread_id != touched.id:
        await ctx.events.update(event.model_copy(update={"thread_id": touched.id}))

    state.linked += 1

    if continuation:
        state.continuations += 1


async def _match_pinned(
    ctx: AbstractContext, state: _LinkState, events: Sequence[Event]
) -> list[Event]:
    """Assigns events to pinned threads whose query keywords all appear in the event
    text (FR4.2); returns the events left over."""
    pinned = await ctx.threads.list_pinned()

    for thread in pinned:
        state.threads[thread.id] = thread

    queried = [thread for thread in pinned if thread.pinned_query is not None]
    remaining: list[Event] = []

    for event in events:
        match = next(
            (
                thread
                for thread in queried
                if matches_pinned_query(thread.pinned_query or "", _event_text(event))
            ),
            None,
        )

        if match is None:
            remaining.append(event)

            continue

        await _attach(ctx, state, event, state.threads[match.id], continuation=True)

        logger.info(
            "Matched event to pinned thread.",
            extra={"event_id": event.id, "thread_id": match.id},
        )

    return remaining


async def _embed_event(ctx: AbstractContext, event: Event) -> Vector:
    """The centroid item's stored vector when there is one, which spares a model call
    per event; otherwise the event text is embedded."""
    if event.centroid_item_id is not None:
        stored = await ctx.items.find_embedding(event.centroid_item_id)

        if stored is not None:
            return stored

    centroid = (
        []
        if event.centroid_item_id is None
        else await ctx.items.find_by_ids([event.centroid_item_id])
    )

    text = (
        _event_text(event) if len(centroid) == 0 else build_embedding_text(centroid[0])
    )

    vectors = await ctx.embeddings.embed([text])

    return vectors[0]


async def _stored_thread_vector(ctx: AbstractContext, thread: Thread) -> Vector | None:
    """The vector of the latest event's centroid item, already in the database."""
    events = await ctx.events.list_by_thread(thread.id)
    latest = max(events, key=lambda event: event.updated_at, default=None)

    if latest is None or latest.centroid_item_id is None:
        return None

    return await ctx.items.find_embedding(latest.centroid_item_id)


async def _load_active_candidates(ctx: AbstractContext, state: _LinkState) -> None:
    """Uses stored vectors; only a thread without one has its title embedded."""
    active = await ctx.threads.list_active(since=inactive_since(ctx.clock.now()))
    missing: list[Thread] = []

    for thread in active:
        state.threads.setdefault(thread.id, thread)
        vector = await _stored_thread_vector(ctx, thread)

        if vector is None:
            missing.append(thread)
        else:
            state.candidates.append(_Candidate(thread.id, vector))

    vectors = await ctx.embeddings.embed([thread.title for thread in missing])

    for thread, vector in zip(missing, vectors, strict=True):
        state.candidates.append(_Candidate(thread.id, vector))


def inactive_since(now: datetime) -> datetime:
    """Threads not seen since this moment are no longer active (FR4.2)."""
    return now - timedelta(days=settings.THREAD_INACTIVE_DAYS)


def _best_candidate(state: _LinkState, vector: Vector) -> str | None:
    best_id: str | None = None
    best_score = 0.0

    for candidate in state.candidates:
        score = cosine_similarity(vector, candidate.vector)

        if score >= settings.THREAD_SIMILARITY_THRESHOLD and score > best_score:
            best_id = candidate.thread_id
            best_score = score

    return best_id


async def _create_thread_for(
    ctx: AbstractContext, state: _LinkState, event: Event, vector: Vector
) -> None:
    thread_id = thread_id_for(event.id)
    existing = await _load_thread(ctx, state, thread_id)

    # NOTE: An existing row means an earlier attempt created the thread; reuse it.
    if existing is not None:
        await _attach(ctx, state, event, existing, continuation=False)
        state.candidates.append(_Candidate(thread_id, vector))

        return

    thread = Thread(
        id=thread_id,
        title=event.title,
        first_seen_at=event.updated_at,
        last_seen_at=event.updated_at,
        day_count=1,
        pinned=False,
        pinned_query=None,
    )
    await ctx.threads.create(thread)
    await ctx.events.update(event.model_copy(update={"thread_id": thread_id}))
    state.threads[thread_id] = thread
    state.candidates.append(_Candidate(thread_id, vector))
    state.linked += 1


async def _link_by_similarity(
    ctx: AbstractContext, state: _LinkState, event: Event
) -> None:
    vector = await _embed_event(ctx, event)
    match = _best_candidate(state, vector)

    if match is None:
        await _create_thread_for(ctx, state, event, vector)

        return

    await _attach(ctx, state, event, state.threads[match], continuation=True)


async def link_events_to_threads(ctx: AbstractContext, event_ids: Sequence[str]) -> int:
    """Pinned queries take precedence; an event already in a thread only refreshes that
    thread's day count; the rest join the most similar active thread or start a new
    one (FR4.1). Returns the number of events linked."""
    events = await _load_events(ctx, event_ids)
    profile = await ctx.preferences.profile()
    state = _LinkState(ZoneInfo(profile.timezone))
    unpinned = await _match_pinned(ctx, state, events)
    unlinked: list[Event] = []

    for event in unpinned:
        if event.thread_id is None:
            unlinked.append(event)

            continue

        thread = await _load_thread(ctx, state, event.thread_id)

        if thread is None:
            logger.warning(
                "Event refers to a missing thread; relinking it.",
                extra={"event_id": event.id, "thread_id": event.thread_id},
            )
            unlinked.append(event)

            continue

        await _attach(ctx, state, event, thread, continuation=True)

    if len(unlinked) > 0:
        await _load_active_candidates(ctx, state)

    for event in unlinked:
        await _link_by_similarity(ctx, state, event)

    logger.info(
        "Linked events to threads.",
        extra={
            "events": len(events),
            "linked": state.linked,
            "continuations": state.continuations,
        },
    )

    return state.linked
