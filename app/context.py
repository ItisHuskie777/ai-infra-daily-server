from abc import ABC
from abc import abstractmethod
from typing import override

from app import settings
from app.adapters import browser
from app.adapters import clock
from app.adapters import database
from app.adapters import email
from app.adapters import embeddings
from app.adapters import http
from app.adapters import ids
from app.adapters import llm
from app.adapters.browser import ImplementsBrowser
from app.adapters.clock import ImplementsClock
from app.adapters.database import ImplementsDatabase
from app.adapters.email import ImplementsEmail
from app.adapters.embeddings import ImplementsEmbeddings
from app.adapters.http import ImplementsHttp
from app.adapters.ids import ImplementsIds
from app.adapters.llm import ImplementsLlm
from app.repositories import DigestRepository
from app.repositories import EventRepository
from app.repositories import ImplementsDigestRepository
from app.repositories import ImplementsEventRepository
from app.repositories import ImplementsItemRepository
from app.repositories import ImplementsPreferenceRepository
from app.repositories import ImplementsRunRepository
from app.repositories import ImplementsSourceRepository
from app.repositories import ImplementsThreadRepository
from app.repositories import ItemRepository
from app.repositories import PreferenceRepository
from app.repositories import RunRepository
from app.repositories import SourceRepository
from app.repositories import ThreadRepository


class AbstractContext(ABC):
    """Everything pipeline and source code may depend on. The entry point builds it from
    real adapters; tests build it from fakes."""

    @property
    @abstractmethod
    def database(self) -> ImplementsDatabase: ...

    @property
    @abstractmethod
    def ids(self) -> ImplementsIds: ...

    @property
    @abstractmethod
    def http(self) -> ImplementsHttp: ...

    @abstractmethod
    async def close(self) -> None: ...

    @property
    @abstractmethod
    def llm(self) -> ImplementsLlm: ...

    @property
    @abstractmethod
    def embeddings(self) -> ImplementsEmbeddings: ...

    @property
    @abstractmethod
    def browser(self) -> ImplementsBrowser | None:
        """None when no browser-rendered source is configured."""

    @property
    @abstractmethod
    def email(self) -> ImplementsEmail: ...

    @property
    @abstractmethod
    def clock(self) -> ImplementsClock: ...

    @property
    @abstractmethod
    def sources(self) -> ImplementsSourceRepository: ...

    @property
    @abstractmethod
    def items(self) -> ImplementsItemRepository: ...

    @property
    @abstractmethod
    def events(self) -> ImplementsEventRepository: ...

    @property
    @abstractmethod
    def threads(self) -> ImplementsThreadRepository: ...

    @property
    @abstractmethod
    def digests(self) -> ImplementsDigestRepository: ...

    @property
    @abstractmethod
    def preferences(self) -> ImplementsPreferenceRepository: ...

    @property
    @abstractmethod
    def runs(self) -> ImplementsRunRepository: ...


class Repositories:
    __slots__ = (
        "digests",
        "events",
        "items",
        "preferences",
        "runs",
        "sources",
        "threads",
    )

    def __init__(self, db: ImplementsDatabase) -> None:
        self.sources: ImplementsSourceRepository = SourceRepository(db)
        self.items: ImplementsItemRepository = ItemRepository(db)
        self.events: ImplementsEventRepository = EventRepository(db, self.items)
        self.threads: ImplementsThreadRepository = ThreadRepository(db)
        self.digests: ImplementsDigestRepository = DigestRepository(db)
        self.preferences: ImplementsPreferenceRepository = PreferenceRepository(db)
        self.runs: ImplementsRunRepository = RunRepository(db)


class Context(AbstractContext):
    def __init__(
        self,
        *,
        http: ImplementsHttp,
        llm: ImplementsLlm,
        embeddings: ImplementsEmbeddings,
        browser: ImplementsBrowser | None,
        email: ImplementsEmail,
        clock: ImplementsClock,
        database: ImplementsDatabase,
        ids: ImplementsIds,
    ) -> None:
        self._http = http
        self._llm = llm
        self._embeddings = embeddings
        self._browser = browser
        self._email = email
        self._clock = clock
        self._database = database
        self._ids = ids
        self._repositories = Repositories(database)

    @property
    @override
    def database(self) -> ImplementsDatabase:
        return self._database

    @property
    @override
    def ids(self) -> ImplementsIds:
        return self._ids

    @property
    @override
    def http(self) -> ImplementsHttp:
        return self._http

    @property
    @override
    def llm(self) -> ImplementsLlm:
        return self._llm

    @property
    @override
    def embeddings(self) -> ImplementsEmbeddings:
        return self._embeddings

    @property
    @override
    def browser(self) -> ImplementsBrowser | None:
        return self._browser

    @property
    @override
    def email(self) -> ImplementsEmail:
        return self._email

    @property
    @override
    def clock(self) -> ImplementsClock:
        return self._clock

    @property
    @override
    def sources(self) -> ImplementsSourceRepository:
        return self._repositories.sources

    @property
    @override
    def items(self) -> ImplementsItemRepository:
        return self._repositories.items

    @property
    @override
    def events(self) -> ImplementsEventRepository:
        return self._repositories.events

    @property
    @override
    def threads(self) -> ImplementsThreadRepository:
        return self._repositories.threads

    @property
    @override
    def digests(self) -> ImplementsDigestRepository:
        return self._repositories.digests

    @property
    @override
    def preferences(self) -> ImplementsPreferenceRepository:
        return self._repositories.preferences

    @property
    @override
    def runs(self) -> ImplementsRunRepository:
        return self._repositories.runs

    @override
    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()

        await self._embeddings.close()
        await self._http.close()
        await self._database.close()


def default() -> Context:
    http_client = http.default()
    needs_browser = settings.XIAOHONGSHU_COOKIE != "" or settings.X_COOKIE != ""

    return Context(
        http=http_client,
        llm=llm.default(),
        embeddings=embeddings.default(),
        browser=browser.default() if needs_browser else None,
        email=email.default(http_client),
        clock=clock.default(),
        database=database.default(),
        ids=ids.default(),
    )
