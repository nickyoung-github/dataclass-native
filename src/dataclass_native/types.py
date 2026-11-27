"""
Annotations for schema fields, beyond the builtin types:

- Sized numbers, e.g. Int32 or Float32 (int and float are 64-bit)
- Shared[T]: the field shares its value, rather than holding its own copy, and is serialised by reference
- DiscriminatedUnion[A, B, ...]: a union serialised with a tag naming the member type

None of these depend on the rest of dataclass_native: to type checkers, they are the types they annotate.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, TypeVar, Union


@dataclass(frozen=True)
class IntSpec:
    bits: int
    signed: bool = True


@dataclass(frozen=True)
class FloatSpec:
    bits: int


Int8 = Annotated[int, IntSpec(8)]
Int16 = Annotated[int, IntSpec(16)]
Int32 = Annotated[int, IntSpec(32)]
Int64 = Annotated[int, IntSpec(64)]
UInt8 = Annotated[int, IntSpec(8, signed=False)]
UInt16 = Annotated[int, IntSpec(16, signed=False)]
UInt32 = Annotated[int, IntSpec(32, signed=False)]
UInt64 = Annotated[int, IntSpec(64, signed=False)]
Float32 = Annotated[float, FloatSpec(32)]
Float64 = Annotated[float, FloatSpec(64)]


class SharedMarker:
    """Annotated metadata marking a field as Shared"""

    def __repr__(self) -> str:
        return "Shared"


class DiscriminatedMarker:
    """Annotated metadata marking a union as discriminated"""

    def __repr__(self) -> str:
        return "Discriminated"


SHARED = SharedMarker()
DISCRIMINATED = DiscriminatedMarker()

T = TypeVar("T")

Shared = Annotated[T, SHARED]
"""
The value is shared, not copied: Shared[A], Shared[A | B], Shared[A] | None.

Natively a reference-counted pointer to an immutable value, compared by value. Serialised once per write, with
references to it, so that sharing survives a round trip and unions of shared types need no union columns.
"""

if TYPE_CHECKING:
    # For type checkers, a union of its members
    DiscriminatedUnion = Union
else:

    class DiscriminatedUnion:
        """A union serialised as {"t": tag, "v": value}, the tag naming the member's type"""

        def __class_getitem__(cls, params):
            if not isinstance(params, tuple) or len(params) < 2:
                raise TypeError("DiscriminatedUnion needs at least two types")
            return Annotated[Union[params], DISCRIMINATED]  # noqa: UP007
