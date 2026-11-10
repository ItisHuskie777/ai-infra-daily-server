from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from datetime import datetime
from typing import override

from pydantic import TypeAdapter

from app.adapters.database import ImplementsDatabase
from app.adapters.database import Row
from app.models import Model
from app.models import Source
from app.models import SourceConfig
from app.models import SourceHealth
from app.repositories._common import StoreError
from app.repositories._common import to_iso
from app.result import OnSuccess

_CONFIG = TypeAdapter[SourceConfig](SourceConfig)


class SourceRun(Model):
    source_id: str
    started_at: datetime
    finished_at: datetime
    ok: bool
    error: str | None
    detail: str | None
    item_count: int


class ImplementsSourceRepository(ABC):
    @abstractmethod
    async def list_all(self) -> list[Source]: ...

    @abstractmethod
    async def list_enabled(self) -> list[Source]: ...

    @abstractmethod
    async def find_by_id(self, source_id: str) -> OnSuccess[Source]: ...

    @abstractmethod
    async def upsert(self, source: Source, *, now: datetime) -> None: ...

    @abstractmethod
    async def insert_missing(self, sources: Sequence[Source], *, now: datetime) -> int:
        """Seeds sources that do not exist yet without touching the owner's edits."""

    @abstractmethod
    async def set_enabled(
        self, source_id: str, *, enabled: bool, now: datetime
    ) -> OnSuccess[None]: ...

    @abstractmethod
    async def set_weight(
        self, source_id: str, *, weight: float, now: datetime
    ) -> OnSuccess[None]: ...

    @abstractmethod
    async def record_run(self, run: SourceRun) -> None: ...

    @abstractmethod
    async def latest_health(self) -> list[SourceHealth]:
        """The most recent run of every enabled source, for the digest footer."""


class SourceRepository(ImplementsSourceRepository):
    __slots__ = ("_db",)

    def __init__(self, db: ImplementsDatabase) -> None:
        self._db = db

    @override
    async def list_all(self) -> list[Source]:
        rows = await self._db.fetch_all("SELECT * FROM sources ORDER BY id")

        return [_to_source(row) for row in rows]

    @override
    async def list_enabled(self) -> list[Source]:
        rows = await self._db.fetch_all(
            "SELECT * FROM sources WHERE enabled = 1 ORDER BY id"
        )

        return [_to_source(row) for row in rows]

    @override
    async def find_by_id(self, source_id: str) -> OnSuccess[Source]:
        row = await self._db.fetch_one(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        )

        if row is None:
            return StoreError.NOT_FOUND

        return _to_source(row)

    @override
    async def upsert(self, source: Source, *, now: datetime) -> None:
        await self._db.execute(
            "INSERT INTO sources (id, name, kind, category, language, enabled, weight, "
            "config_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, kind = excluded.kind, "
            "category = excluded.category, language = excluded.language, "
            "weight = excluded.weight, config_json = excluded.config_json, "
            "updated_at = excluded.updated_at",
            (
                source.id,
                source.name,
                source.kind,
                source.category,
                source.language,
                int(source.enabled),
                source.weight,
                source.config.model_dump_json(),
                to_iso(now),
                to_iso(now),
            ),
        )

    @override
    async def insert_missing(self, sources: Sequence[Source], *, now: datetime) -> int:
        existing = {
            str(row["id"]) for row in await self._db.fetch_all("SELECT id FROM sources")
        }
        new = [source for source in sources if source.id not in existing]

        if new:
            await self._db.execute_many(
                "INSERT INTO sources (id, name, kind, category, language, enabled, "
                "weight, config_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        source.id,
                        source.name,
                        source.kind,
                        source.category,
                        source.language,
                        int(source.enabled),
                        source.weight,
                        source.config.model_dump_json(),
                        to_iso(now),
                        to_iso(now),
                    )
                    for source in new
                ],
            )

        return len(new)

    @override
    async def set_enabled(
        self, source_id: str, *, enabled: bool, now: datetime
    ) -> OnSuccess[None]:
        found = await self.find_by_id(source_id)

        if isinstance(found, StoreError):
            return found

        await self._db.execute(
            "UPDATE sources SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), to_iso(now), source_id),
        )

        return None

    @override
    async def set_weight(
        self, source_id: str, *, weight: float, now: datetime
    ) -> OnSuccess[None]:
        found = await self.find_by_id(source_id)

        if isinstance(found, StoreError):
            return found

        await self._db.execute(
            "UPDATE sources SET weight = ?, updated_at = ? WHERE id = ?",
            (weight, to_iso(now), source_id),
        )

        return None

    @override
    async def record_run(self, run: SourceRun) -> None:
        await self._db.execute(
            "INSERT INTO source_runs (source_id, started_at, finished_at, ok, error, "
            "detail, item_count) VALUES (?,?,?,?,?,?,?)",
            (
                run.source_id,
                to_iso(run.started_at),
                to_iso(run.finished_at),
                int(run.ok),
                run.error,
                run.detail,
                run.item_count,
            ),
        )

    @override
    async def latest_health(self) -> list[SourceHealth]:
        rows = await self._db.fetch_all(
            "SELECT s.id AS source_id, s.name, r.ok, r.error, r.item_count "
            "FROM sources s JOIN source_runs r ON r.source_id = s.id "
            "WHERE s.enabled = 1 AND r.id = ("
            "  SELECT id FROM source_runs WHERE source_id = s.id "
            "  ORDER BY started_at DESC LIMIT 1) "
            "ORDER BY s.id"
        )

        return [
            SourceHealth(
                source_id=str(row["source_id"]),
                name=str(row["name"]),
                ok=bool(row["ok"]),
                error=None if row["error"] is None else str(row["error"]),
                item_count=int(str(row["item_count"])),
            )
            for row in rows
        ]


def _to_source(row: Row) -> Source:
    return Source(
        id=str(row["id"]),
        name=str(row["name"]),
        category=str(row["category"]),
        language=str(row["language"]),
        enabled=bool(row["enabled"]),
        weight=float(str(row["weight"])),
        config=_CONFIG.validate_json(str(row["config_json"])),
    )
