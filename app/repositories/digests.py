from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from datetime import datetime
from typing import override

from pydantic import TypeAdapter

from app.adapters.database import ImplementsDatabase
from app.adapters.database import Row
from app.models import DeliveryStatus
from app.models import Digest
from app.models import DigestEventPlacement
from app.models import DigestSection
from app.models import Feedback
from app.models import Model
from app.models import QuizQuestion
from app.models import SourceHealth
from app.models import Topic
from app.repositories._common import StoreError
from app.repositories._common import from_iso
from app.repositories._common import from_json
from app.repositories._common import to_iso
from app.repositories._common import to_json
from app.result import OnSuccess

_HEALTH = TypeAdapter(tuple[SourceHealth, ...])


class DigestPlacementRecord(Model):
    event_id: str
    section: DigestSection
    position: int
    relevance_score: float
    rationale: str
    item_ids: tuple[str, ...]
    topics: tuple[Topic, ...]


class ImplementsDigestRepository(ABC):
    @abstractmethod
    async def save(
        self, digest: Digest, placements: Sequence[DigestEventPlacement]
    ) -> None:
        """Stores the digest and its placements together; ranked events are not
        persisted."""

    @abstractmethod
    async def latest_sent_date(self) -> str | None:
        """The local date of the last digest that went out; this, not the profile,
        decides whether today's digest is still due."""

    @abstractmethod
    async def set_delivery_status(
        self, digest_id: str, status: DeliveryStatus, error: str | None
    ) -> None: ...

    @abstractmethod
    async def find_by_id(self, digest_id: str) -> OnSuccess[Digest]: ...

    @abstractmethod
    async def find_by_date(self, date: str) -> OnSuccess[Digest]: ...

    @abstractmethod
    async def list_recent(self, *, limit: int) -> list[Digest]: ...

    @abstractmethod
    async def list_placements(self, digest_id: str) -> list[DigestPlacementRecord]: ...

    @abstractmethod
    async def record_feedback(self, feedback: Feedback) -> OnSuccess[None]:
        """Each link acts once: the same signal on the same target is a conflict."""

    @abstractmethod
    async def record_quiz_answer(
        self, digest_id: str, question: QuizQuestion, answer: str, *, now: datetime
    ) -> OnSuccess[None]:
        """One answer per digest and question; a repeat is a conflict."""


class DigestRepository(ImplementsDigestRepository):
    __slots__ = ("_db",)

    def __init__(self, db: ImplementsDatabase) -> None:
        self._db = db

    @override
    async def save(
        self, digest: Digest, placements: Sequence[DigestEventPlacement]
    ) -> None:
        await self._db.transaction(
            [
                (
                    "INSERT INTO digests (id, date, style, subject, html, health_json, "
                    "delivery_status, delivery_error, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        digest.id,
                        digest.date,
                        digest.style,
                        digest.subject,
                        digest.html,
                        _HEALTH.dump_json(digest.health).decode(),
                        digest.delivery_status,
                        digest.delivery_error,
                        to_iso(digest.created_at),
                    ),
                ),
                *(
                    (
                        "INSERT INTO digest_events (digest_id, event_id, section, "
                        "position, relevance_score, rationale, item_ids_json, "
                        "topics_json) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            digest.id,
                            placement.event.id,
                            placement.section,
                            placement.position,
                            placement.event.relevance_score,
                            placement.event.rationale,
                            to_json(list(placement.item_ids)),
                            to_json([topic.value for topic in placement.event.topics]),
                        ),
                    )
                    for placement in placements
                ),
            ]
        )

    @override
    async def latest_sent_date(self) -> str | None:
        row = await self._db.fetch_one(
            "SELECT MAX(date) AS date FROM digests WHERE delivery_status = 'sent'"
        )

        return None if row is None or row["date"] is None else str(row["date"])

    @override
    async def set_delivery_status(
        self, digest_id: str, status: DeliveryStatus, error: str | None
    ) -> None:
        await self._db.execute(
            "UPDATE digests SET delivery_status = ?, delivery_error = ? WHERE id = ?",
            (status, error, digest_id),
        )

    @override
    async def find_by_id(self, digest_id: str) -> OnSuccess[Digest]:
        row = await self._db.fetch_one(
            "SELECT * FROM digests WHERE id = ?", (digest_id,)
        )

        if row is None:
            return StoreError.NOT_FOUND

        return _to_digest(row)

    @override
    async def find_by_date(self, date: str) -> OnSuccess[Digest]:
        row = await self._db.fetch_one(
            "SELECT * FROM digests WHERE date = ? ORDER BY created_at DESC LIMIT 1",
            (date,),
        )

        if row is None:
            return StoreError.NOT_FOUND

        return _to_digest(row)

    @override
    async def list_recent(self, *, limit: int) -> list[Digest]:
        rows = await self._db.fetch_all(
            "SELECT * FROM digests ORDER BY created_at DESC LIMIT ?", (limit,)
        )

        return [_to_digest(row) for row in rows]

    @override
    async def list_placements(self, digest_id: str) -> list[DigestPlacementRecord]:
        rows = await self._db.fetch_all(
            "SELECT * FROM digest_events WHERE digest_id = ? "
            "ORDER BY section, position",
            (digest_id,),
        )

        return [_to_placement(row) for row in rows]

    @override
    async def record_feedback(self, feedback: Feedback) -> OnSuccess[None]:
        changed = await self._db.execute(
            "INSERT OR IGNORE INTO feedback (created_at, digest_id, event_id, item_id, "
            "source_id, signal) VALUES (?,?,?,?,?,?)",
            (
                to_iso(feedback.created_at),
                feedback.digest_id,
                feedback.event_id,
                feedback.item_id,
                feedback.source_id,
                feedback.signal,
            ),
        )

        return StoreError.CONFLICT if changed == 0 else None

    @override
    async def record_quiz_answer(
        self, digest_id: str, question: QuizQuestion, answer: str, *, now: datetime
    ) -> OnSuccess[None]:
        changed = await self._db.execute(
            "INSERT OR IGNORE INTO quiz_answers (digest_id, question, answer, "
            "created_at) VALUES (?,?,?,?)",
            (digest_id, question, answer, to_iso(now)),
        )

        return StoreError.CONFLICT if changed == 0 else None


def _to_digest(row: Row) -> Digest:
    return Digest(
        id=str(row["id"]),
        date=str(row["date"]),
        style=str(row["style"]),
        subject=str(row["subject"]),
        html=str(row["html"]),
        health=_HEALTH.validate_json(str(row["health_json"])),
        delivery_status=str(row["delivery_status"]),
        delivery_error=None
        if row["delivery_error"] is None
        else str(row["delivery_error"]),
        created_at=from_iso(row["created_at"]),
    )


def _to_topics(value: object) -> tuple[Topic, ...]:
    known = {topic.value for topic in Topic}

    if not isinstance(value, list):
        return ()

    return tuple(Topic(str(entry)) for entry in value if str(entry) in known)


def _to_placement(row: Row) -> DigestPlacementRecord:
    item_ids = from_json(row["item_ids_json"])

    return DigestPlacementRecord(
        event_id=str(row["event_id"]),
        section=DigestSection(str(row["section"])),
        position=int(str(row["position"])),
        relevance_score=float(str(row["relevance_score"])),
        rationale=str(row["rationale"]),
        item_ids=tuple(str(value) for value in item_ids)
        if isinstance(item_ids, list)
        else (),
        topics=_to_topics(from_json(row["topics_json"])),
    )
