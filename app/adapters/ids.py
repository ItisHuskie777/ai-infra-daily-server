import uuid
from abc import ABC
from abc import abstractmethod
from typing import override


class ImplementsIds(ABC):
    """Source of new identifiers, so a test can make run and digest ids predictable."""

    @abstractmethod
    def new(self) -> str: ...


class RandomIds(ImplementsIds):
    __slots__ = ()

    @override
    def new(self) -> str:
        return str(uuid.uuid4())


def default() -> ImplementsIds:
    return RandomIds()
