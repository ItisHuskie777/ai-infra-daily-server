import asyncio
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from typing import override

import numpy
from fastembed import TextEmbedding
from fastembed.common.model_description import ModelSource
from fastembed.common.model_description import PoolingType

from app import logging
from app import settings
from app import vectors
from app.vectors import Vector

logger = logging.get_logger(__name__)

_BGE_M3 = "BAAI/bge-m3"
_BGE_M3_DIMENSIONS = 1024
# A batch is padded to its longest text and attention memory grows with the square of
# that length, so large batches of long posts need gigabytes; eight stays under 2 GB.
_INFERENCE_BATCH_SIZE = 8


class ImplementsEmbeddings(ABC):
    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        """One unit-length float32 vector per text, in order."""

    @abstractmethod
    async def close(self) -> None:
        """Drops the loaded model, which holds about 3 GB; the next `embed` loads it
        again."""


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
        embedded = await asyncio.to_thread(
            lambda: list(model.embed(list(texts), batch_size=_INFERENCE_BATCH_SIZE))
        )

        return [
            vectors.normalise(numpy.asarray(vector, dtype=numpy.float32))
            for vector in embedded
        ]

    @override
    async def close(self) -> None:
        async with self._lock:
            self._model = None

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


def default() -> ImplementsEmbeddings:
    return FastembedEmbeddings(settings.EMBEDDING_MODEL)
