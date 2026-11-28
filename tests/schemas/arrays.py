"""Fields which are numpy arrays"""

from dataclasses import dataclass

import numpy as np

from dataclass_native import native
from dataclass_native.arrays import Matrix, Vector


@native
@dataclass(frozen=True)
class Curve:
    name: str
    points: Vector[np.float64]
    weights: Vector[np.float32] | None = None
    # Of one dimension, as numpy says it
    ids: np.ndarray[tuple[int], np.dtype[np.int32]] | None = None
    octets: Vector[np.uint8] | None = None


@native
@dataclass(frozen=True)
class Surface:
    """Arrays of more than one dimension"""

    values: Matrix[np.float64]
    counts: Matrix[np.int16] | None = None
    # Of three dimensions, as numpy says it
    image: np.ndarray[tuple[int, int, int], np.dtype[np.uint8]] | None = None


@native
@dataclass(frozen=True)
class Book:
    """Holds objects which have arrays"""

    curves: tuple[Curve, ...] = ()
    base: Curve | None = None
    surface: Surface | None = None
