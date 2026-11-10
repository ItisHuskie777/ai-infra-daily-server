from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime

from app.adapters.database import ImplementsDatabase
from app.adapters.database import Row
from app.models import Event
from app.models import EventScore
from app.models import EventWithItems
from app.repositories._common import StoreError
from app.repositories._common import from_iso
from app.repositories._common import placeholders
from app.repositories._common import to_iso
from app.repositories.items import ItemRepository
from app.result import OnSuccess


class EventRepository:
    __slots__ = ("_db", "_items")

    def __init__(self, db: ImplementsDatabase, items: ItemRepository) -> None:
        self._db = db
        self._items = items

    async def create(self, event: Event) -> None:
        await self._db.execute(
            "INSERT INTO events (id, thread_id, title, summary, created_at, "
            "updated_at, centroid_item_id) VALUES (?,?,?,?,?,?,?)",
            (
                event.id,
                event.thread_id,
                event.title,
                event.summary,
                to_iso(event.created_at),
                to_iso(event.updated_at),
                event.centroid_item_id,
            ),
        )

    async def update(self, event: Event) -> None:
        await self._db.execute(
            "UPDATE events SET thread_id = ?, title = ?, summary = ?, updated_at = ?, "
            "centroid_item_id = ? WHERE id = ?",
            (
                event.thread_id,
                event.title,
                event.summary,
                to_iso(event.updated_at),
                event.centroid_item_id,
                event.id,
            ),
        )

    async def scores(self, event_ids: Sequence[str]) -> dict[str, EventScore]:
        if len(event_ids) == 0:
            return {}

        rows = await self._db.fetch_all(
            "SELECT id, score_json FROM events WHERE score_json IS NOT NULL "
            f"AND id IN ({placeholders(len(event_ids))})",
            tuple(event_ids),
        )

        return {
            str(row["id"]): EventScore.model_validate_json(str(row["score_json"]))
            for row in rows
        }

    async def save_scores(self, scores: Mapping[str, EventScore]) -> None:
        await self._db.execute_many(
            "UPDATE events SET score_json = ? WHERE id = ?",
            [(score.model_dump_json(), event_id) for event_id, score in scores.items()],
        )

    async def find_by_id(self, event_id: str) -> OnSuccess[EventWithItems]:
        row = await self._db.fetch_one("SELECT * FROM events WHERE id = ?", (event_id,))

        if row is None:
            return StoreError.NOT_FOUND

        return await self._with_items(row)

    async def list_updated_since(self, since: datetime) -> list[EventWithItems]:
        rows = await self._db.fetch_all(
            "SELECT * FROM events WHERE updated_at >= ? ORDER BY updated_at DESC",
            (to_iso(since),),
        )

        return [await self._with_items(row) for row in rows]

    async def list_by_thread(self, thread_id: str) -> list[EventWithItems]:
        rows = await self._db.fetch_all(
            "SELECT * FROM events WHERE thread_id = ? ORDER BY created_at", (thread_id,)
        )

        return [await self._with_items(row) for row in rows]

    async def list_unthreaded_since(self, since: datetime) -> list[EventWithItems]:
        rows = await self._db.fetch_all(
            "SELECT * FROM events WHERE thread_id IS NULL AND updated_at >= ? "
            "ORDER BY updated_at",
            (to_iso(since),),
        )

        return [await self._with_items(row) for row in rows]

    async def _with_items(self, row: Row) -> EventWithItems:
        event = _to_event(row)
        items = await self._items.list_by_event(event.id)

        return EventWithItems(**event.model_dump(), items=tuple(items))


def _to_event(row: Row) -> Event:
    return Event(
        id=str(row["id"]),
        thread_id=None if row["thread_id"] is None else str(row["thread_id"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        centroid_item_id=None
        if row["centroid_item_id"] is None
        else str(row["centroid_item_id"]),
    )
