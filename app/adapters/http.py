from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from typing import override

import httpx

from app import logging
from app import settings
from app.models import Model
from app.result import OnSuccess
from app.result import ServiceError

logger = logging.get_logger(__name__)


class HttpError(ServiceError):
    NETWORK = "network"
    TIMEOUT = "timeout"

    @override
    def service(self) -> str:
        return "http"


class HttpResponse(Model):
    status: int
    url: str
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class ImplementsHttp(ABC):
    """Any HTTP status is returned as a response; only transport failures are errors."""

    @abstractmethod
    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = settings.SOURCE_FETCH_TIMEOUT_SECONDS,
    ) -> OnSuccess[HttpResponse]: ...

    @abstractmethod
    async def post_json(
        self,
        url: str,
        payload: object,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = settings.SOURCE_FETCH_TIMEOUT_SECONDS,
    ) -> OnSuccess[HttpResponse]: ...

    @abstractmethod
    async def close(self) -> None: ...


class HttpxClient(ImplementsHttp):
    __slots__ = ("_client",)

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @override
    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = settings.SOURCE_FETCH_TIMEOUT_SECONDS,
    ) -> OnSuccess[HttpResponse]:
        try:
            response = await self._client.get(
                url, headers=dict(headers or {}), timeout=timeout_seconds
            )
        except httpx.TimeoutException:
            logger.warning("HTTP request timed out.", extra={"url": url})

            return HttpError.TIMEOUT
        except httpx.HTTPError:
            logger.warning("HTTP request failed.", extra={"url": url})

            return HttpError.NETWORK

        return _to_response(response)

    @override
    async def post_json(
        self,
        url: str,
        payload: object,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = settings.SOURCE_FETCH_TIMEOUT_SECONDS,
    ) -> OnSuccess[HttpResponse]:
        try:
            response = await self._client.post(
                url, json=payload, headers=dict(headers or {}), timeout=timeout_seconds
            )
        except httpx.TimeoutException:
            logger.warning("HTTP request timed out.", extra={"url": url})

            return HttpError.TIMEOUT
        except httpx.HTTPError:
            logger.warning("HTTP request failed.", extra={"url": url})

            return HttpError.NETWORK

        return _to_response(response)

    @override
    async def close(self) -> None:
        await self._client.aclose()


def _to_response(response: httpx.Response) -> HttpResponse:
    return HttpResponse(
        status=response.status_code,
        url=str(response.url),
        headers={key.lower(): value for key, value in response.headers.items()},
        body=response.content,
    )


def default() -> ImplementsHttp:
    client = httpx.AsyncClient(
        follow_redirects=True,
        headers={"user-agent": settings.USER_AGENT},
        limits=httpx.Limits(max_connections=settings.SOURCE_FETCH_CONCURRENCY * 2),
    )

    return HttpxClient(client)
