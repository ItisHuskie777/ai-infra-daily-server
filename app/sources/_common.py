from abc import ABC
from abc import abstractmethod
from typing import override

from app.context import AbstractContext
from app.models import Source
from app.models import SourceItem
from app.models import SourceKind
from app.result import OnSuccess
from app.result import ServiceError


class FetchError(ServiceError):
    FETCH_FAILED = "fetch_failed"
    PARSE_FAILED = "parse_failed"
    NOT_CONFIGURED = "not_configured"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"

    @override
    def service(self) -> str:
        return "sources"


class ImplementsSourceAdapter(ABC):
    """One per source kind. `fetch` returns the newest items the source currently
    exposes, or the reason it cannot run (missing cookie, browser or config) before
    doing any I/O; the caller caps, dedupes and records health."""

    @property
    @abstractmethod
    def kind(self) -> SourceKind: ...

    @abstractmethod
    async def fetch(
        self, ctx: AbstractContext, source: Source
    ) -> OnSuccess[list[SourceItem]]: ...
