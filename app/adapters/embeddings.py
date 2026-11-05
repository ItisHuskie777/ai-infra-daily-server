import asyncio
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from typing import override

import numpy
from fastembed import TextEmbedding
from fastembed.common.model_description import ModelSource
from fastembed.common.model_description import PoolingType
from numpy.typing import NDArray

from app import logging
from app import settings

type Vector = NDArray[numpy.float32]

logger = logging.get_logger(__name__)

_BGE_M3 = "BAAI/bge-m3"
_BGE_M3_DIMENSIONS = 1024


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
                self._model = await asyncio.to_thread(_load, self._model_name)

            return self._model


def _load(model_name: str) -> TextEmbedding:
    supported = {model["model"] for model in TextEmbedding.list_supported_models()}

    # NOTE: fastembed ships no dense bge-m3, so the official ONNX export is registered
    # as a custom model; its first output holds token embeddings, pooled on [CLS].
    if model_name == _BGE_M3 and _BGE_M3 not in supported:
        TextEmbedding.add_custom_model(
            model=_BGE_M3,
            pooling=PoolingType.CLS,
            normalization=True,
            sources=ModelSource(hf=_BGE_M3),
            dim=_BGE_M3_DIMENSIONS,
            model_file="onnx/model.onnx",
            additional_files=["onnx/model.onnx_data"],
        )

    return TextEmbedding(model_name=model_name)


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
