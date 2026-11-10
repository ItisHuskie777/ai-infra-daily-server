from abc import ABC
from abc import abstractmethod
from datetime import datetime
from typing import override

from app.adapters.database import ImplementsDatabase
from app.adapters.database import Row
from app.models import Thread
from app.repositories._common import StoreError
from app.repositories._common import from_iso
from app.repositories._common import to_iso
from app.result import OnSuccess


class ImplementsThreadRepository(ABC):
    @abstractmethod
    async def create(self, thread: Thread) -> None: ...

    @abstractmethod
    async def update(self, thread: Thread) -> None:
        """Pipeline progress only; pinning is the owner's and changes through
        `set_pinned`, so a run holding an older copy cannot undo it."""

    @abstractmethod
    async def set_pinned(
        self, thread_id: str, *, pinned: bool, query: str | None
    ) -> OnSuccess[None]: ...

    @abstractmethod
    async def find_by_id(self, thread_id: str) -> OnSuccess[Thread]: ...

    @abstractmethod
    async def list_active(self, *, since: datetime) -> list[Thread]: ...

    @abstractmethod
    async def list_pinned(self, *, since: datetime) -> list[Thread]:
        """Pinned threads seen since `since`; a pin lapses with inactivity (FR4.2)."""


class ThreadRepository(ImplementsThreadRepository):
    __slots__ = ("_db",)

    def __init__(self, db: ImplementsDatabase) -> None:
        self._db = db

    @override
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

    @override
    async def update(self, thread: Thread) -> None:
        await self._db.execute(
            "UPDATE threads SET title = ?, last_seen_at = ?, day_count = ? "
            "WHERE id = ?",
            (thread.title, to_iso(thread.last_seen_at), thread.day_count, thread.id),
        )

    @override
    async def set_pinned(
        self, thread_id: str, *, pinned: bool, query: str | None
    ) -> OnSuccess[None]:
        changed = await self._db.execute(
            "UPDATE threads SET pinned = ?, pinned_query = ? WHERE id = ?",
            (int(pinned), query if pinned else None, thread_id),
        )

        return StoreError.NOT_FOUND if changed == 0 else None

    @override
    async def find_by_id(self, thread_id: str) -> OnSuccess[Thread]:
        row = await self._db.fetch_one(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        )

        if row is None:
            return StoreError.NOT_FOUND

        return _to_thread(row)

    @override
    async def list_active(self, *, since: datetime) -> list[Thread]:
        rows = await self._db.fetch_all(
            "SELECT * FROM threads WHERE last_seen_at >= ? ORDER BY last_seen_at DESC",
            (to_iso(since),),
        )

        return [_to_thread(row) for row in rows]

    @override
    async def list_pinned(self, *, since: datetime) -> list[Thread]:
        rows = await self._db.fetch_all(
            "SELECT * FROM threads WHERE pinned = 1 AND last_seen_at >= ? "
            "ORDER BY last_seen_at DESC",
            (to_iso(since),),
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
