from abc import ABC
from abc import abstractmethod
from datetime import UTC
from datetime import datetime
from typing import override


class ImplementsClock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Timezone-aware UTC."""


class SystemClock(ImplementsClock):
    @override
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


def default() -> ImplementsClock:
    return SystemClock()
