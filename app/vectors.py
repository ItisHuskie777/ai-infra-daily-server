import numpy
from numpy.typing import NDArray

type Vector = NDArray[numpy.float32]


def normalise(vector: Vector) -> Vector:
    norm = float(numpy.linalg.norm(vector))

    if norm == 0.0:
        return vector

    return (vector / norm).astype(numpy.float32)


def cosine_similarity(left: Vector, right: Vector) -> float:
    """Both inputs are expected to be unit length, as the embedding adapter returns
    them."""
    return float(numpy.dot(left, right))
