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
from app.adapters import llm
from app.adapters.browser import ImplementsBrowser
from app.adapters.clock import ImplementsClock
from app.adapters.database import ImplementsDatabase
from app.adapters.email import ImplementsEmail
from app.adapters.embeddings import ImplementsEmbeddings
from app.adapters.http import ImplementsHttp
from app.adapters.llm import ImplementsLlm
from app.repositories import DigestRepository
from app.repositories import EventRepository
from app.repositories import ItemRepository
from app.repositories import PreferenceRepository
from app.repositories import RunRepository
from app.repositories import SourceRepository
from app.repositories import ThreadRepository


class AbstractContext(ABC):
    """Everything pipeline and source code may depend on. The entry point builds it from
    real adapters; tests build it from fakes. Nothing below this reads settings."""

    @property
    @abstractmethod
    def http(self) -> ImplementsHttp: ...

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
    def xiaohongshu_cookie(self) -> str: ...

    @property
    @abstractmethod
    def x_cookie(self) -> str: ...

    @property
    @abstractmethod
    def sources(self) -> SourceRepository: ...

    @property
    @abstractmethod
    def items(self) -> ItemRepository: ...

    @property
    @abstractmethod
    def events(self) -> EventRepository: ...

    @property
    @abstractmethod
    def threads(self) -> ThreadRepository: ...

    @property
    @abstractmethod
    def digests(self) -> DigestRepository: ...

    @property
    @abstractmethod
    def preferences(self) -> PreferenceRepository: ...

    @property
    @abstractmethod
    def runs(self) -> RunRepository: ...


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
        self.sources = SourceRepository(db)
        self.items = ItemRepository(db)
        self.events = EventRepository(db, self.items)
        self.threads = ThreadRepository(db)
        self.digests = DigestRepository(db)
        self.preferences = PreferenceRepository(db)
        self.runs = RunRepository(db)


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
        xiaohongshu_cookie: str,
        x_cookie: str,
    ) -> None:
        self._http = http
        self._llm = llm
        self._embeddings = embeddings
        self._browser = browser
        self._email = email
        self._clock = clock
        self._database = database
        self._xiaohongshu_cookie = xiaohongshu_cookie
        self._x_cookie = x_cookie
        self._repositories = Repositories(database)

    @property
    def database(self) -> ImplementsDatabase:
        return self._database

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
    def xiaohongshu_cookie(self) -> str:
        return self._xiaohongshu_cookie

    @property
    @override
    def x_cookie(self) -> str:
        return self._x_cookie

    @property
    @override
    def sources(self) -> SourceRepository:
        return self._repositories.sources

    @property
    @override
    def items(self) -> ItemRepository:
        return self._repositories.items

    @property
    @override
    def events(self) -> EventRepository:
        return self._repositories.events

    @property
    @override
    def threads(self) -> ThreadRepository:
        return self._repositories.threads

    @property
    @override
    def digests(self) -> DigestRepository:
        return self._repositories.digests

    @property
    @override
    def preferences(self) -> PreferenceRepository:
        return self._repositories.preferences

    @property
    @override
    def runs(self) -> RunRepository:
        return self._repositories.runs

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()

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
        xiaohongshu_cookie=settings.XIAOHONGSHU_COOKIE,
        x_cookie=settings.X_COOKIE,
    )
