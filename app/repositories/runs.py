from abc import ABC
from abc import abstractmethod
from datetime import datetime
from datetime import timedelta
from typing import override

from app.adapters.database import ImplementsDatabase
from app.models import RunStatus
from app.repositories._common import to_iso

_PIPELINE_LEASE = "pipeline"
_FAILED_OUTCOMES = (RunStatus.FAILED, RunStatus.CRASHED, RunStatus.ABANDONED)


class ImplementsRunRepository(ABC):
    """Instrumentation: one row per pipeline run with the last stage reached (QA1),
    plus the lease that keeps two runs from overlapping."""

    @abstractmethod
    async def acquire_lease(
        self, holder: str, *, now: datetime, duration: timedelta
    ) -> bool:
        """Takes the pipeline lease unless another holder's lease is still live; the
        upsert only overwrites an expired lease, so two processes cannot both win."""

    @abstractmethod
    async def release_lease(self, holder: str) -> None: ...

    @abstractmethod
    async def abandon_unfinished(self, *, except_id: str, now: datetime) -> None:
        """Runs left open by a killed process; only called while holding the lease."""

    @abstractmethod
    async def count_failed_since(self, since: datetime) -> int: ...

    @abstractmethod
    async def prune_before(self, before: datetime) -> int:
        """Run and source-run history older than `before`."""

    @abstractmethod
    async def start(self, run_id: str, *, now: datetime) -> None: ...

    @abstractmethod
    async def set_stage(self, run_id: str, stage: str) -> None: ...

    @abstractmethod
    async def finish(
        self,
        run_id: str,
        *,
        now: datetime,
        outcome: RunStatus,
        detail: str | None,
        digest_id: str | None,
    ) -> None: ...


class RunRepository(ImplementsRunRepository):
    __slots__ = ("_db",)

    def __init__(self, db: ImplementsDatabase) -> None:
        self._db = db

    @override
    async def acquire_lease(
        self, holder: str, *, now: datetime, duration: timedelta
    ) -> bool:
        await self._db.execute(
            "INSERT INTO run_lease (name, holder, expires_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET holder = excluded.holder, "
            "expires_at = excluded.expires_at WHERE run_lease.expires_at < ?",
            (_PIPELINE_LEASE, holder, to_iso(now + duration), to_iso(now)),
        )
        row = await self._db.fetch_one(
            "SELECT holder FROM run_lease WHERE name = ?", (_PIPELINE_LEASE,)
        )

        return row is not None and str(row["holder"]) == holder

    @override
    async def release_lease(self, holder: str) -> None:
        await self._db.execute(
            "DELETE FROM run_lease WHERE name = ? AND holder = ?",
            (_PIPELINE_LEASE, holder),
        )

    @override
    async def abandon_unfinished(self, *, except_id: str, now: datetime) -> None:
        await self._db.execute(
            "UPDATE runs SET finished_at = ?, outcome = ? "
            "WHERE finished_at IS NULL AND id != ?",
            (to_iso(now), RunStatus.ABANDONED, except_id),
        )

    @override
    async def count_failed_since(self, since: datetime) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS failures FROM runs WHERE started_at >= ? "
            "AND outcome IN (?,?,?)",
            (to_iso(since), *_FAILED_OUTCOMES),
        )

        return 0 if row is None else int(str(row["failures"]))

    @override
    async def prune_before(self, before: datetime) -> int:
        runs = await self._db.execute(
            "DELETE FROM runs WHERE started_at < ? AND finished_at IS NOT NULL",
            (to_iso(before),),
        )
        source_runs = await self._db.execute(
            "DELETE FROM source_runs WHERE started_at < ?", (to_iso(before),)
        )

        return runs + source_runs

    @override
    async def start(self, run_id: str, *, now: datetime) -> None:
        await self._db.execute(
            "INSERT INTO runs (id, started_at, stage) VALUES (?,?,?)",
            (run_id, to_iso(now), "started"),
        )

    @override
    async def set_stage(self, run_id: str, stage: str) -> None:
        await self._db.execute(
            "UPDATE runs SET stage = ? WHERE id = ?", (stage, run_id)
        )

    @override
    async def finish(
        self,
        run_id: str,
        *,
        now: datetime,
        outcome: RunStatus,
        detail: str | None,
        digest_id: str | None,
    ) -> None:
        await self._db.execute(
            "UPDATE runs SET finished_at = ?, outcome = ?, detail = ?, digest_id = ? "
            "WHERE id = ?",
            (to_iso(now), outcome, detail, digest_id, run_id),
        )
