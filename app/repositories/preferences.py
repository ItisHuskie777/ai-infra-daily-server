from abc import ABC
from abc import abstractmethod
from datetime import datetime
from typing import override

from app import settings
from app.adapters.database import ImplementsDatabase
from app.models import DigestStyle
from app.models import Interests
from app.models import Profile
from app.repositories._common import to_iso

_PROFILE_KEY = "profile"
_INTERESTS_KEY = "interests"


class ImplementsPreferenceRepository(ABC):
    @abstractmethod
    async def profile(self) -> Profile:
        """The stored profile, or the one implied by the environment before any edit."""

    @abstractmethod
    async def save_profile(self, profile: Profile, *, now: datetime) -> None: ...

    @abstractmethod
    async def interests(self) -> Interests: ...

    @abstractmethod
    async def save_interests(self, interests: Interests, *, now: datetime) -> None: ...


class PreferenceRepository(ImplementsPreferenceRepository):
    __slots__ = ("_db",)

    def __init__(self, db: ImplementsDatabase) -> None:
        self._db = db

    @override
    async def profile(self) -> Profile:
        row = await self._db.fetch_one(
            "SELECT value_json FROM preferences WHERE key = ?", (_PROFILE_KEY,)
        )

        if row is None:
            return Profile(
                email=settings.OWNER_EMAIL,
                timezone=settings.TIMEZONE,
                delivery_hour=settings.DELIVERY_HOUR,
                digest_style=DigestStyle(settings.DIGEST_STYLE),
            )

        return Profile.model_validate_json(str(row["value_json"]))

    @override
    async def save_profile(self, profile: Profile, *, now: datetime) -> None:
        await self._save(_PROFILE_KEY, profile.model_dump_json(), now)

    @override
    async def interests(self) -> Interests:
        row = await self._db.fetch_one(
            "SELECT value_json FROM preferences WHERE key = ?", (_INTERESTS_KEY,)
        )

        if row is None:
            return Interests()

        return Interests.model_validate_json(str(row["value_json"]))

    @override
    async def save_interests(self, interests: Interests, *, now: datetime) -> None:
        await self._save(_INTERESTS_KEY, interests.model_dump_json(), now)

    async def _save(self, key: str, value_json: str, now: datetime) -> None:
        await self._db.execute(
            "INSERT INTO preferences (key, value_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = excluded.updated_at",
            (key, value_json, to_iso(now)),
        )
