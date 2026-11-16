from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from datetime import datetime
from typing import override

import numpy
from pydantic import TypeAdapter

from app.adapters.database import ImplementsDatabase
from app.adapters.database import Row
from app.models import Comment
from app.models import Engagement
from app.models import SourceItem
from app.models import StoredItem
from app.repositories._common import from_iso
from app.repositories._common import from_json
from app.repositories._common import placeholders
from app.repositories._common import to_iso
from app.repositories._common import to_json
from app.vectors import Vector

_COMMENTS = TypeAdapter(tuple[Comment, ...])
_ENGAGEMENT = TypeAdapter(Engagement)


_QUERY_CHUNK = 500
# Every column of a stored item except the vector, which is several kilobytes a row and
# only wanted by the clustering queries that ask for it.
_COLUMNS = (
    "id, source_id, kind, url, canonical_url, title, author, language, published_at, "
    "first_seen_at, text, engagement_json, comments_json, tags_json, event_id"
)


class EmbeddedItem:
    __slots__ = ("item", "vector")  # Thousands exist per clustering pass.

    def __init__(self, item: StoredItem, vector: Vector) -> None:
        self.item = item
        self.vector = vector


class ImplementsItemRepository(ABC):
    @abstractmethod
    async def insert_new(
        self, items: Sequence[SourceItem], *, first_seen_at: datetime
    ) -> list[str]:
        """Inserts items whose id is not stored yet; returns the new ids (FR2.1)."""

    @abstractmethod
    async def find_by_ids(self, ids: Sequence[str]) -> list[StoredItem]: ...

    @abstractmethod
    async def list_unembedded(self, *, limit: int) -> list[StoredItem]: ...

    @abstractmethod
    async def store_embeddings(self, vectors: Sequence[tuple[str, Vector]]) -> None: ...

    @abstractmethod
    async def list_embedded_since(
        self, published_after: datetime
    ) -> list[EmbeddedItem]:
        """Every embedded item in the window, clustered or not, with its vector."""

    @abstractmethod
    async def find_embedding(self, item_id: str) -> Vector | None: ...

    @abstractmethod
    async def list_by_event(self, event_id: str) -> list[StoredItem]: ...

    @abstractmethod
    async def assign_event(self, item_ids: Sequence[str], event_id: str) -> None: ...

    @abstractmethod
    async def filter_already_sent(self, ids: Sequence[str]) -> set[str]:
        """Ids among `ids` that appeared in a digest that was sent (FR2.2)."""

    @abstractmethod
    async def sent_titles_since(self, since: datetime) -> list[str]:
        """Titles of items in digests sent since `since`."""

    @abstractmethod
    async def prune_before(self, published_before: datetime) -> int:
        """Drops the bulky parts (text, comments, vector) of items too old to matter;
        the rows stay, so already-sent bookkeeping and references remain valid. An
        empty vector, unlike NULL, keeps the item out of the embedding queue."""


class ItemRepository(ImplementsItemRepository):
    __slots__ = ("_db",)

    def __init__(self, db: ImplementsDatabase) -> None:
        self._db = db

    @override
    async def insert_new(
        self, items: Sequence[SourceItem], *, first_seen_at: datetime
    ) -> list[str]:
        if len(items) == 0:
            return []

        ids = [item.id for item in items]
        existing = {
            str(row["id"])
            for row in await self._db.fetch_all(
                f"SELECT id FROM items WHERE id IN ({placeholders(len(ids))})", ids
            )
        }
        seen: set[str] = set()
        new: list[SourceItem] = []

        for item in items:
            if item.id in existing or item.id in seen:
                continue

            seen.add(item.id)
            new.append(item)

        await self._db.execute_many(
            "INSERT INTO items (id, source_id, kind, url, canonical_url, title, "
            "author, language, published_at, first_seen_at, text, engagement_json, "
            "comments_json, tags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    item.id,
                    item.source_id,
                    item.kind,
                    item.url,
                    item.canonical_url,
                    item.title,
                    item.author,
                    item.language,
                    to_iso(item.published_at),
                    to_iso(first_seen_at),
                    item.text,
                    None
                    if item.engagement is None
                    else item.engagement.model_dump_json(),
                    _COMMENTS.dump_json(item.comments).decode(),
                    to_json(list(item.tags)),
                )
                for item in new
            ],
        )

        return [item.id for item in new]

    @override
    async def find_by_ids(self, ids: Sequence[str]) -> list[StoredItem]:
        if len(ids) == 0:
            return []

        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM items WHERE id IN ({placeholders(len(ids))})",
            list(ids),
        )
        by_id = {str(row["id"]): to_stored_item(row) for row in rows}

        return [by_id[item_id] for item_id in ids if item_id in by_id]

    @override
    async def list_unembedded(self, *, limit: int) -> list[StoredItem]:
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM items WHERE embedding IS NULL "
            "ORDER BY published_at DESC LIMIT ?",
            (limit,),
        )

        return [to_stored_item(row) for row in rows]

    @override
    async def store_embeddings(self, vectors: Sequence[tuple[str, Vector]]) -> None:
        await self._db.execute_many(
            "UPDATE items SET embedding = ? WHERE id = ?",
            [
                (numpy.asarray(vector, dtype="<f4").tobytes(), item_id)
                for item_id, vector in vectors
            ],
        )

    @override
    async def list_embedded_since(
        self, published_after: datetime
    ) -> list[EmbeddedItem]:
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS}, embedding FROM items "
            "WHERE length(embedding) > 0 AND published_at >= ? ORDER BY published_at",
            (to_iso(published_after),),
        )

        return [
            EmbeddedItem(to_stored_item(row), _to_vector(row["embedding"]))
            for row in rows
        ]

    @override
    async def find_embedding(self, item_id: str) -> Vector | None:
        row = await self._db.fetch_one(
            "SELECT embedding FROM items WHERE id = ? AND length(embedding) > 0",
            (item_id,),
        )

        return None if row is None else _to_vector(row["embedding"])

    @override
    async def list_by_event(self, event_id: str) -> list[StoredItem]:
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM items WHERE event_id = ? ORDER BY published_at",
            (event_id,),
        )

        return [to_stored_item(row) for row in rows]

    @override
    async def assign_event(self, item_ids: Sequence[str], event_id: str) -> None:
        if len(item_ids) == 0:
            return

        await self._db.execute(
            "UPDATE items SET event_id = ? "
            f"WHERE id IN ({placeholders(len(item_ids))})",
            [event_id, *item_ids],
        )

    @override
    async def filter_already_sent(self, ids: Sequence[str]) -> set[str]:
        if len(ids) == 0:
            return set()

        sent: set[str] = set()
        unique = list(dict.fromkeys(ids))

        for start in range(0, len(unique), _QUERY_CHUNK):
            chunk = unique[start : start + _QUERY_CHUNK]
            rows = await self._db.fetch_all(
                "SELECT DISTINCT listed.value AS item_id FROM digest_events de "
                "JOIN digests d ON d.id = de.digest_id, "
                "json_each(de.item_ids_json) AS listed "
                "WHERE d.delivery_status = 'sent' "
                f"AND listed.value IN ({placeholders(len(chunk))})",
                tuple(chunk),
            )
            sent.update(str(row["item_id"]) for row in rows)

        return sent

    @override
    async def sent_titles_since(self, since: datetime) -> list[str]:
        rows = await self._db.fetch_all(
            "SELECT DISTINCT i.title FROM digest_events de "
            "JOIN digests d ON d.id = de.digest_id, "
            "json_each(de.item_ids_json) AS listed "
            "JOIN items i ON i.id = listed.value "
            "WHERE d.delivery_status = 'sent' AND d.created_at >= ?",
            (to_iso(since),),
        )

        return [str(row["title"]) for row in rows]

    @override
    async def prune_before(self, published_before: datetime) -> int:
        return await self._db.execute(
            "UPDATE items SET text = '', comments_json = '[]', embedding = X'' "
            "WHERE published_at < ? AND (text != '' OR length(embedding) > 0)",
            (to_iso(published_before),),
        )


def to_stored_item(row: Row) -> StoredItem:
    engagement = row["engagement_json"]
    tags = from_json(row["tags_json"])

    return StoredItem(
        id=str(row["id"]),
        source_id=str(row["source_id"]),
        kind=str(row["kind"]),
        url=str(row["url"]),
        canonical_url=str(row["canonical_url"]),
        title=str(row["title"]),
        author=None if row["author"] is None else str(row["author"]),
        language=str(row["language"]),
        published_at=from_iso(row["published_at"]),
        text=str(row["text"]),
        engagement=None
        if engagement is None
        else _ENGAGEMENT.validate_json(str(engagement)),
        comments=_COMMENTS.validate_json(str(row["comments_json"])),
        tags=tuple(str(value) for value in tags) if isinstance(tags, list) else (),
        first_seen_at=from_iso(row["first_seen_at"]),
        event_id=None if row["event_id"] is None else str(row["event_id"]),
    )


def _to_vector(blob: object) -> Vector:
    return numpy.frombuffer(bytes(blob), dtype="<f4").astype(numpy.float32)  # type: ignore[call-overload]  # The column is a BLOB.
