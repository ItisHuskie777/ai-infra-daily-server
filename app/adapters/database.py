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
from app.result import OnSuccess
from app.result import ServiceError
from app.result import is_error

type Row = Mapping[str, object]
type Params = Sequence[object]

logger = logging.get_logger(__name__)


class DatabaseError(ServiceError):
    MIGRATION_FAILED = "migration_failed"

    @override
    def service(self) -> str:
        return "database"


class ImplementsDatabase(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def execute(self, sql: str, params: Params = ()) -> int:
        """Returns the number of rows the statement changed."""

    @abstractmethod
    async def execute_many(self, sql: str, rows: Iterable[Params]) -> None: ...

    @abstractmethod
    async def fetch_one(self, sql: str, params: Params = ()) -> Row | None: ...

    @abstractmethod
    async def fetch_all(self, sql: str, params: Params = ()) -> list[Row]: ...

    @abstractmethod
    async def transaction(self, statements: Sequence[tuple[str, Params]]) -> None:
        """Runs the statements as one unit: all of them take effect or none does."""

    @abstractmethod
    async def migrate(self, directory: Path) -> OnSuccess[list[str]]:
        """Applies every `*.sql` file not yet recorded, in name order, each in its own
        transaction; returns the names applied. Safe to run from several processes."""

    @abstractmethod
    async def backup(self, path: Path) -> None:
        """Writes a consistent copy of the live database to `path`."""


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
    async def execute(self, sql: str, params: Params = ()) -> int:
        connection = await self._connected()

        async with self._lock:
            cursor = await connection.execute(sql, tuple(params))
            await connection.commit()

        return cursor.rowcount

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
    async def transaction(self, statements: Sequence[tuple[str, Params]]) -> None:
        connection = await self._connected()

        async with self._lock:
            await connection.execute("BEGIN IMMEDIATE")

            try:
                for sql, params in statements:
                    await connection.execute(sql, tuple(params))
            except Exception:
                await connection.rollback()

                raise

            await connection.commit()

    @override
    async def migrate(self, directory: Path) -> OnSuccess[list[str]]:
        connection = await self._connected()
        paths = await asyncio.to_thread(lambda: sorted(directory.glob("*.sql")))
        applied: list[str] = []

        async with self._lock:
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, "
                "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            await connection.commit()

            for path in paths:
                script = await asyncio.to_thread(path.read_text, encoding="utf-8")
                outcome = await _apply_migration(connection, path.name, script)

                if is_error(outcome):
                    return outcome

                if outcome:
                    applied.append(path.name)

        if applied:
            logger.info("Applied database migrations.", extra={"names": applied})

        return applied

    @override
    async def backup(self, path: Path) -> None:
        connection = await self._connected()
        await asyncio.to_thread(lambda: path.parent.mkdir(parents=True, exist_ok=True))

        async with self._lock:
            await connection.execute("VACUUM INTO ?", (str(path),))

    async def _connected(self) -> aiosqlite.Connection:
        if self._connection is None:
            await self.connect()

        assert self._connection is not None

        return self._connection


def _to_mapping(row: sqlite3.Row) -> Row:
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


def default() -> ImplementsDatabase:
    return SqliteDatabase(settings.DATABASE_PATH)


def _statements(script: str) -> list[str]:
    """Splits a migration file into complete statements, so they can run inside one
    transaction; `executescript` would commit before starting."""
    statements: list[str] = []
    buffer = ""

    for line in script.splitlines(keepends=True):
        buffer += line

        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""

    return [statement for statement in statements if statement != ""]


async def _apply_migration(
    connection: aiosqlite.Connection, name: str, script: str
) -> OnSuccess[bool]:
    """Checks and records the migration inside an immediate transaction, so a second
    process waits and then finds it recorded; a failing statement rolls the whole
    file back."""
    await connection.execute("BEGIN IMMEDIATE")

    async with connection.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
    ) as cursor:
        recorded = await cursor.fetchone() is not None

    if recorded:
        await connection.rollback()

        return False

    try:
        for statement in _statements(script):
            await connection.execute(statement)

        await connection.execute(
            "INSERT INTO schema_migrations (name) VALUES (?)", (name,)
        )
    except sqlite3.Error:
        logger.exception("Database migration failed.", extra={"migration": name})
        await connection.rollback()

        return DatabaseError.MIGRATION_FAILED

    await connection.commit()

    return True
