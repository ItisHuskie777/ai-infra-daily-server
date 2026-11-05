from datetime import datetime

from app.adapters.database import ImplementsDatabase
from app.adapters.database import Row
from app.models import Thread
from app.repositories._common import StoreError
from app.repositories._common import from_iso
from app.repositories._common import to_iso
from app.result import OnSuccess


class ThreadRepository:
    __slots__ = ("_db",)

    def __init__(self, db: ImplementsDatabase) -> None:
        self._db = db

    async def create(self, thread: Thread) -> None:
        await self._db.execute(
            "INSERT INTO threads (id, title, first_seen_at, last_seen_at, day_count, "
            "pinned, pinned_query) VALUES (?,?,?,?,?,?,?)",
            (
                thread.id,
                thread.title,
                to_iso(thread.first_seen_at),
                to_iso(thread.last_seen_at),
                thread.day_count,
                int(thread.pinned),
                thread.pinned_query,
            ),
        )

    async def update(self, thread: Thread) -> None:
        await self._db.execute(
            "UPDATE threads SET title = ?, last_seen_at = ?, day_count = ?, "
            "pinned = ?, pinned_query = ? WHERE id = ?",
            (
                thread.title,
                to_iso(thread.last_seen_at),
                thread.day_count,
                int(thread.pinned),
                thread.pinned_query,
                thread.id,
            ),
        )

    async def find_by_id(self, thread_id: str) -> OnSuccess[Thread]:
        row = await self._db.fetch_one(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        )

        if row is None:
            return StoreError.NOT_FOUND

        return _to_thread(row)

    async def list_active(self, *, since: datetime) -> list[Thread]:
        rows = await self._db.fetch_all(
            "SELECT * FROM threads WHERE last_seen_at >= ? OR pinned = 1 "
            "ORDER BY last_seen_at DESC",
            (to_iso(since),),
        )

        return [_to_thread(row) for row in rows]

    async def list_pinned(self) -> list[Thread]:
        rows = await self._db.fetch_all(
            "SELECT * FROM threads WHERE pinned = 1 ORDER BY last_seen_at DESC"
        )

        return [_to_thread(row) for row in rows]


def _to_thread(row: Row) -> Thread:
    return Thread(
        id=str(row["id"]),
        title=str(row["title"]),
        first_seen_at=from_iso(row["first_seen_at"]),
        last_seen_at=from_iso(row["last_seen_at"]),
        day_count=int(str(row["day_count"])),
        pinned=bool(row["pinned"]),
        pinned_query=None if row["pinned_query"] is None else str(row["pinned_query"]),
    )
