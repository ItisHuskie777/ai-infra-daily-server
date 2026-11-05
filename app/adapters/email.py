from abc import ABC
from abc import abstractmethod
from typing import override

from app import logging
from app import settings
from app.adapters.http import ImplementsHttp
from app.result import OnSuccess
from app.result import ServiceError
from app.result import is_error

logger = logging.get_logger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"


class EmailError(ServiceError):
    NETWORK = "network"
    REJECTED = "rejected"
    NOT_CONFIGURED = "not_configured"

    @override
    def service(self) -> str:
        return "email"


class ImplementsEmail(ABC):
    @abstractmethod
    async def send(self, *, to: str, subject: str, html: str) -> OnSuccess[str]:
        """Returns the provider's message id."""


class ResendEmail(ImplementsEmail):
    __slots__ = ("_api_key", "_http", "_sender")

    def __init__(self, http: ImplementsHttp, *, api_key: str, sender: str) -> None:
        self._http = http
        self._api_key = api_key
        self._sender = sender

    @override
    async def send(self, *, to: str, subject: str, html: str) -> OnSuccess[str]:
        if self._api_key == "":
            return EmailError.NOT_CONFIGURED

        response = await self._http.post_json(
            _RESEND_ENDPOINT,
            {"from": self._sender, "to": [to], "subject": subject, "html": html},
            headers={"authorization": f"Bearer {self._api_key}"},
            timeout_seconds=30.0,
        )

        if is_error(response):
            return EmailError.NETWORK

        if not response.ok:
            logger.error(
                "Email provider rejected the message.",
                extra={"status": response.status, "body": response.text[:500]},
            )

            return EmailError.REJECTED

        return _message_id(response.text)


def _message_id(body: str) -> str:
    marker = '"id":"'
    start = body.find(marker)

    if start == -1:
        return ""

    start += len(marker)

    return body[start : body.find('"', start)]


def default(http: ImplementsHttp) -> ImplementsEmail:
    return ResendEmail(
        http, api_key=settings.RESEND_API_KEY, sender=settings.EMAIL_FROM
    )
