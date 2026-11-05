import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import UTC
from datetime import datetime
from typing import override

from pydantic import Field
from pydantic import ValidationError

from app.models import Model
from app.result import OnSuccess
from app.result import ServiceError

_BASE64URL = re.compile(r"^[A-Za-z0-9_-]*$")


class FeedbackTokenError(ServiceError):
    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"

    @override
    def service(self) -> str:
        return "feedback_token"


class FeedbackClaims(Model):
    digest_id: str
    expires_at: datetime


class _Payload(Model):
    digest_id: str = Field(min_length=1)
    exp: int = Field(gt=0)


def _to_base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _from_base64url(text: str) -> bytes | None:
    if _BASE64URL.match(text) is None or len(text) % 4 == 1:
        return None

    padded = text + "=" * (-len(text) % 4)

    try:
        return base64.urlsafe_b64decode(padded)
    except binascii.Error:
        # NOTE: The base64 module has no non-raising decoder; a rejected body is a
        # malformed token.
        return None


def _signature(secret: str, body: str) -> bytes:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()


def _parse_payload(data: bytes) -> _Payload | None:
    try:
        return _Payload.model_validate_json(data)
    except ValidationError:
        # NOTE: Pydantic signals an invalid document by raising; the token is malformed.
        return None


def sign(secret: str, *, digest_id: str, expires_at: datetime) -> str:
    payload = {"digest_id": digest_id, "exp": int(expires_at.timestamp())}
    body = _to_base64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = _to_base64url(_signature(secret, body))

    return f"{body}.{signature}"


def verify(secret: str, token: str, *, now: datetime) -> OnSuccess[FeedbackClaims]:
    body, separator, signature_text = token.partition(".")

    if separator == "" or body == "" or "." in signature_text:
        return FeedbackTokenError.MALFORMED

    body_bytes = _from_base64url(body)
    signature = _from_base64url(signature_text)

    if body_bytes is None or signature is None:
        return FeedbackTokenError.MALFORMED

    if not hmac.compare_digest(_signature(secret, body), signature):
        return FeedbackTokenError.BAD_SIGNATURE

    payload = _parse_payload(body_bytes)

    if payload is None:
        return FeedbackTokenError.MALFORMED

    expires_at = datetime.fromtimestamp(payload.exp, tz=UTC)

    if expires_at <= now:
        return FeedbackTokenError.EXPIRED

    return FeedbackClaims(digest_id=payload.digest_id, expires_at=expires_at)
