import asyncio
import ipaddress
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from typing import override
from urllib.parse import urljoin
from urllib.parse import urlsplit

import httpx

from app import logging
from app import settings
from app.models import Model
from app.result import OnSuccess
from app.result import ServiceError
from app.result import is_error

logger = logging.get_logger(__name__)

_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_SCHEMES = frozenset({"http", "https"})
# The per-phase timeout never trips on a server that drips bytes; this bounds the whole
# request.
_TOTAL_TIMEOUT_FACTOR = 3


class HttpError(ServiceError):
    NETWORK = "network"
    TIMEOUT = "timeout"
    TOO_LARGE = "too_large"
    FORBIDDEN_ADDRESS = "forbidden_address"
    TOO_MANY_REDIRECTS = "too_many_redirects"

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
        """Follows redirects one hop at a time, refusing any hop to a private address
        and any oversized body, since source URLs come from feeds."""
        current = url

        for _ in range(_MAX_REDIRECTS + 1):
            if not await _is_public(current):
                logger.warning(
                    "Refused a request to a private address.", extra={"url": current}
                )

                return HttpError.FORBIDDEN_ADDRESS

            response = await self._get_once(current, headers, timeout_seconds)

            if is_error(response) or response.status not in _REDIRECT_STATUSES:
                return response

            location = response.headers.get("location")

            if location is None:
                return response

            current = urljoin(current, location)

        return HttpError.TOO_MANY_REDIRECTS

    async def _get_once(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> OnSuccess[HttpResponse]:
        try:
            async with asyncio.timeout(timeout_seconds * _TOTAL_TIMEOUT_FACTOR):
                request = self._client.build_request(
                    "GET", url, headers=dict(headers or {}), timeout=timeout_seconds
                )
                response = await self._client.send(
                    request, stream=True, follow_redirects=False
                )

                try:
                    body = await _read_capped(response)
                finally:
                    await response.aclose()
        except httpx.TimeoutException, TimeoutError:
            logger.warning("HTTP request timed out.", extra={"url": url})

            return HttpError.TIMEOUT
        except httpx.HTTPError:
            logger.warning("HTTP request failed.", extra={"url": url})

            return HttpError.NETWORK

        if body is None:
            logger.warning("HTTP response too large.", extra={"url": url})

            return HttpError.TOO_LARGE

        return HttpResponse(
            status=response.status_code,
            url=str(response.url),
            headers={key.lower(): value for key, value in response.headers.items()},
            body=body,
        )

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


async def _read_capped(response: httpx.Response) -> bytes | None:
    chunks: list[bytes] = []
    size = 0

    async for chunk in response.aiter_bytes():
        size += len(chunk)

        if size > settings.MAX_RESPONSE_BYTES:
            return None

        chunks.append(chunk)

    return b"".join(chunks)


async def _is_public(url: str) -> bool:
    parts = urlsplit(url)

    if parts.scheme not in _ALLOWED_SCHEMES or parts.hostname is None:
        return False

    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        )
    except OSError:
        # NOTE: getaddrinfo has no non-raising form; an unresolvable host is refused.
        return False

    return all(
        ipaddress.ip_address(str(address[4][0])).is_global for address in addresses
    )


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
