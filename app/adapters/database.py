import asyncio
import sqlite3
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import override

import aiosqlite

from app import logging
from app import settings

type Row = Mapping[str, object]
type Params = Sequence[object]

logger = logging.get_logger(__name__)


class ImplementsDatabase(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def execute(self, sql: str, params: Params = ()) -> None: ...

    @abstractmethod
    async def execute_many(self, sql: str, rows: Iterable[Params]) -> None: ...

    @abstractmethod
    async def fetch_one(self, sql: str, params: Params = ()) -> Row | None: ...

    @abstractmethod
    async def fetch_all(self, sql: str, params: Params = ()) -> list[Row]: ...

    @abstractmethod
    async def migrate(self, directory: Path) -> list[str]:
        """Applies every `*.sql` file not yet recorded, in name order; returns the
        names applied."""


class SqliteDatabase(ImplementsDatabase):
    __slots__ = ("_connection", "_lock", "_path")

    def __init__(self, path: str) -> None:
        self._path = path
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @override
    async def connect(self) -> None:
        if self._connection is not None:
            return

        await asyncio.to_thread(
            lambda: Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        )
        connection = await aiosqlite.connect(self._path)
        connection.row_factory = sqlite3.Row
        await connection.execute("PRAGMA journal_mode = WAL")
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        self._connection = connection
        logger.debug("Connected to the SQLite database.", extra={"path": self._path})

    @override
    async def close(self) -> None:
        if self._connection is None:
            return

        await self._connection.close()
        self._connection = None

    @override
    async def execute(self, sql: str, params: Params = ()) -> None:
        connection = await self._connected()

        async with self._lock:
            await connection.execute(sql, tuple(params))
            await connection.commit()

    @override
    async def execute_many(self, sql: str, rows: Iterable[Params]) -> None:
        connection = await self._connected()

        async with self._lock:
            await connection.executemany(sql, [tuple(row) for row in rows])
            await connection.commit()

    @override
    async def fetch_one(self, sql: str, params: Params = ()) -> Row | None:
        connection = await self._connected()

        async with self._lock, connection.execute(sql, tuple(params)) as cursor:
            row = await cursor.fetchone()

        return None if row is None else _to_mapping(row)

    @override
    async def fetch_all(self, sql: str, params: Params = ()) -> list[Row]:
        connection = await self._connected()

        async with self._lock, connection.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()

        return [_to_mapping(row) for row in rows]

    @override
    async def migrate(self, directory: Path) -> list[str]:
        connection = await self._connected()
        applied: list[str] = []

        async with self._lock:
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, "
                "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )

            async with connection.execute(
                "SELECT name FROM schema_migrations"
            ) as cursor:
                done = {str(row[0]) for row in await cursor.fetchall()}

            paths = await asyncio.to_thread(lambda: sorted(directory.glob("*.sql")))

            for path in paths:
                if path.name in done:
                    continue

                script = await asyncio.to_thread(path.read_text, encoding="utf-8")
                await connection.executescript(script)
                await connection.execute(
                    "INSERT INTO schema_migrations (name) VALUES (?)", (path.name,)
                )
                applied.append(path.name)

            await connection.commit()

        if applied:
            logger.info("Applied database migrations.", extra={"names": applied})

        return applied

    async def _connected(self) -> aiosqlite.Connection:
        if self._connection is None:
            await self.connect()

        assert self._connection is not None

        return self._connection


def _to_mapping(row: sqlite3.Row) -> Row:
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


def default() -> ImplementsDatabase:
    return SqliteDatabase(settings.DATABASE_PATH)
