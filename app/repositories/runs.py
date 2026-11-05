from datetime import datetime

from app.adapters.database import ImplementsDatabase
from app.repositories._common import to_iso


class RunRepository:
    """Instrumentation: one row per pipeline run with the last stage reached (QA1)."""

    __slots__ = ("_db",)

    def __init__(self, db: ImplementsDatabase) -> None:
        self._db = db

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
