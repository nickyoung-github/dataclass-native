"""Functions implemented natively, by the hand-written extension schemas._functions: see tests/cpp/functions.cpp"""

import datetime as dt
from dataclasses import replace

import numpy as np

from dataclass_native import Shared, native_function
from dataclass_native.arrays import Matrix, Vector

from .common import Currency, Money, Party


@native_function("._functions")
def implementation() -> str:
    return "python"


@native_function("._functions")
def total(amounts: tuple[Money, ...], currency: Currency = Currency.USD) -> Money:
    """The sum of the amounts in the currency"""
    return Money(sum(m.amount for m in amounts if m.currency is currency), currency)


@native_function("._functions")
def renamed(party: Party, name: str | None = None) -> Party:
    return replace(party, name=name or "?")


@native_function("._functions")
def same(party: Shared[Party]) -> Shared[Party]:
    return party


@native_function("schemas._functions")
def later(day: dt.date, days: int) -> dt.date:
    return day + dt.timedelta(days)


@native_function("._functions")
def scaled(points: Vector[np.float64], factor: float) -> Vector[np.float64]:
    return points * factor


@native_function("._functions")
def transposed(values: Matrix[np.float64]) -> Matrix[np.float64]:
    return values.T


@native_function("._functions")
def nothing(value: int) -> None:
    pass


@native_function("._functions")
def python_only(value: int) -> int:
    return value + 1


@native_function("._absent")
def no_extension(value: int) -> int:
    return value * 2


@native_function("._functions")
def unannotated(value) -> int:
    return value
