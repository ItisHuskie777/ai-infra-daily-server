from abc import abstractmethod
from enum import StrEnum
from typing import TypeIs


class ServiceError(StrEnum):
    """Base of every error enum; a function that can fail returns `OnSuccess[X]`."""

    @abstractmethod
    def service(self) -> str: ...


# NOTE: An enum body treats every plain name as a member, so the alias lives here.
type OnSuccess[T] = T | ServiceError


def is_error(value: object) -> TypeIs[ServiceError]:
    return isinstance(value, ServiceError)


def is_success[T](value: T | ServiceError) -> TypeIs[T]:
    return not isinstance(value, ServiceError)
