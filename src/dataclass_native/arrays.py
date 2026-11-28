"""
Annotations for fields which are numpy arrays, whose number of dimensions must be known:

- Vector[np.float64]: of one dimension
- Matrix[np.float64]: of two

Each is numpy's own annotation, as is that of an array of more: np.ndarray[tuple[int, int, int], np.dtype[np.float64]]
"""

import numpy as np

type Vector[T: np.generic] = np.ndarray[tuple[int], np.dtype[T]]
type Matrix[T: np.generic] = np.ndarray[tuple[int, int], np.dtype[T]]
