import json
from datetime import datetime
from typing import override

from app.result import ServiceError


class StoreError(ServiceError):
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"

    @override
    def service(self) -> str:
        return "store"


def to_iso(value: datetime) -> str:
    return value.isoformat()


def from_iso(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def to_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: object) -> object:
    return json.loads(str(value))


def placeholders(count: int) -> str:
    return ",".join("?" * count)
