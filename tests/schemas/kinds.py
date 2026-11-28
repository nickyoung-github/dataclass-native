"""Kinds of field which the other schema modules do not have"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum, auto

from dataclass_native import DiscriminatedUnion, Shared, UInt8, native

from .common import Currency, Money, Side


@native
@dataclass(frozen=True)
class Kinds:
    octets: tuple[UInt8, ...] = ()
    number: int | float | str = 0
    nested: tuple[tuple[Money, ...], ...] = ()
    by_name: Mapping[str, tuple[Side, ...]] = field(default_factory=dict)
    sides: frozenset[Side] = frozenset()
    pair: tuple[int, Money] | None = None
    container: tuple[int, ...] | Mapping[str, Money] | None = None
    tagged: DiscriminatedUnion[Money, Side, int] = 0
    blobs: tuple[bytes, ...] = ()


class Level(StrEnum):
    LOW = auto()
    HIGH = auto()


@native
@dataclass(frozen=True)
class Graded:
    """Enums, which may be given by value"""

    level: Level = Level.LOW
    side: Side | None = None
    currencies: tuple[Currency, ...] = ()
    levels: frozenset[Level] = frozenset()
    by_side: Mapping[Side, Level] = field(default_factory=dict)
    either: Level | int = 0


@native
@dataclass(frozen=True)
class Link:
    """Shares objects of its own class"""

    name: str
    next: "Shared[Link] | None" = None
