import hmac
from typing import Annotated

from fastapi import Header
from fastapi import HTTPException

from app import settings

_SCHEME = "Bearer "


def _is_owner(authorization: str) -> bool:
    if not authorization.startswith(_SCHEME):
        return False

    return hmac.compare_digest(
        authorization[len(_SCHEME) :].encode(), settings.OWNER_TOKEN.encode()
    )


async def require_owner(authorization: Annotated[str, Header()] = "") -> None:
    if _is_owner(authorization):
        return

    # NOTE: FastAPI's dependency contract rejects a request by raising HTTPException.
    raise HTTPException(status_code=401, detail="unauthorised")
