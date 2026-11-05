import asyncio
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from typing import override

import numpy
from fastembed import TextEmbedding
from numpy.typing import NDArray

from app import logging
from app import settings

type Vector = NDArray[numpy.float32]

logger = logging.get_logger(__name__)


class ImplementsEmbeddings(ABC):
    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        """One unit-length float32 vector per text, in order."""


class FastembedEmbeddings(ImplementsEmbeddings):
    __slots__ = ("_lock", "_model", "_model_name")

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: TextEmbedding | None = None
        self._lock = asyncio.Lock()

    @override
    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        if len(texts) == 0:
            return []

        model = await self._loaded()
        # NOTE: ONNX inference is CPU-bound and synchronous, so it runs in a thread.
        vectors = await asyncio.to_thread(lambda: list(model.embed(list(texts))))

        return [
            normalise(numpy.asarray(vector, dtype=numpy.float32)) for vector in vectors
        ]

    async def _loaded(self) -> TextEmbedding:
        async with self._lock:
            if self._model is None:
                logger.info(
                    "Loading the embedding model.", extra={"model": self._model_name}
                )
                self._model = await asyncio.to_thread(
                    lambda: TextEmbedding(model_name=self._model_name)
                )

            return self._model


def normalise(vector: Vector) -> Vector:
    norm = float(numpy.linalg.norm(vector))

    if norm == 0.0:
        return vector

    return (vector / norm).astype(numpy.float32)


def cosine_similarity(left: Vector, right: Vector) -> float:
    """Both inputs are expected to be unit length, as `embed` returns them."""
    return float(numpy.dot(left, right))


def default() -> ImplementsEmbeddings:
    return FastembedEmbeddings(settings.EMBEDDING_MODEL)
