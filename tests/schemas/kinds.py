"""Kinds of field which the other schema modules do not have"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from dataclass_native import DiscriminatedUnion, UInt8, native

from .common import Money, Side


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
