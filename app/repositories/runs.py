from datetime import datetime
from datetime import timedelta

from app.adapters.database import ImplementsDatabase
from app.repositories._common import to_iso

_PIPELINE_LEASE = "pipeline"
_FAILED_OUTCOMES = ("failed", "crashed", "abandoned")


class RunRepository:
    """Instrumentation: one row per pipeline run with the last stage reached (QA1),
    plus the lease that keeps two runs from overlapping."""

    __slots__ = ("_db",)

    def __init__(self, db: ImplementsDatabase) -> None:
        self._db = db

    async def acquire_lease(
        self, holder: str, *, now: datetime, duration: timedelta
    ) -> bool:
        """Takes the pipeline lease unless another holder's lease is still live; the
        upsert only overwrites an expired lease, so two processes cannot both win."""
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

    async def release_lease(self, holder: str) -> None:
        await self._db.execute(
            "DELETE FROM run_lease WHERE name = ? AND holder = ?",
            (_PIPELINE_LEASE, holder),
        )

    async def abandon_unfinished(self, *, except_id: str, now: datetime) -> None:
        """Runs left open by a killed process; only called while holding the lease."""
        await self._db.execute(
            "UPDATE runs SET finished_at = ?, outcome = 'abandoned' "
            "WHERE finished_at IS NULL AND id != ?",
            (to_iso(now), except_id),
        )

    async def count_failed_since(self, since: datetime) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS failures FROM runs WHERE started_at >= ? "
            "AND outcome IN (?,?,?)",
            (to_iso(since), *_FAILED_OUTCOMES),
        )

        return 0 if row is None else int(str(row["failures"]))

    async def prune_before(self, before: datetime) -> int:
        """Run and source-run history older than `before`."""
        runs = await self._db.execute(
            "DELETE FROM runs WHERE started_at < ? AND finished_at IS NOT NULL",
            (to_iso(before),),
        )
        source_runs = await self._db.execute(
            "DELETE FROM source_runs WHERE started_at < ?", (to_iso(before),)
        )

        return runs + source_runs

    async def start(self, run_id: str, *, now: datetime) -> None:
        await self._db.execute(
            "INSERT INTO runs (id, started_at, stage) VALUES (?,?,?)",
            (run_id, to_iso(now), "started"),
        )

    async def set_stage(self, run_id: str, stage: str) -> None:
        await self._db.execute(
            "UPDATE runs SET stage = ? WHERE id = ?", (stage, run_id)
        )

    async def finish(
        self,
        run_id: str,
        *,
        now: datetime,
        outcome: str,
        detail: str | None,
        digest_id: str | None,
    ) -> None:
        await self._db.execute(
            "UPDATE runs SET finished_at = ?, outcome = ?, detail = ?, digest_id = ? "
            "WHERE id = ?",
            (to_iso(now), outcome, detail, digest_id, run_id),
        )
