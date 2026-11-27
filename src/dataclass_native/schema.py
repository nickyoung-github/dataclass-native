"""
The schema model: a language-neutral description of the @native dataclasses and enums, from which each backend
generates code. Built by dataclass_native.introspect.
"""

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum as PyEnum
from typing import Any


class SchemaError(TypeError):
    """A class cannot be described by the schema"""


@dataclass(frozen=True)
class Bool:
    pass


@dataclass(frozen=True)
class Int:
    bits: int = 64
    signed: bool = True

    @property
    def min(self) -> int:
        return -(2 ** (self.bits - 1)) if self.signed else 0

    @property
    def max(self) -> int:
        return 2 ** (self.bits - 1) - 1 if self.signed else 2**self.bits - 1


@dataclass(frozen=True)
class Float:
    bits: int = 64


@dataclass(frozen=True)
class Str:
    pass


@dataclass(frozen=True)
class Bytes:
    pass


@dataclass(frozen=True)
class Date:
    pass


@dataclass(frozen=True)
class Time:
    """A time of day, without a timezone"""


@dataclass(frozen=True)
class DateTime:
    """An instant: a timezone-aware datetime, stored as microseconds since the epoch, UTC"""


@dataclass(frozen=True)
class TimeDelta:
    pass


@dataclass(frozen=True)
class EnumRef:
    module: str
    name: str
    cls: type[PyEnum] = field(compare=False, repr=False)


@dataclass(frozen=True)
class StructRef:
    module: str
    name: str
    cls: type = field(compare=False, repr=False)


@dataclass(frozen=True)
class Optional:
    inner: "Type"


@dataclass(frozen=True)
class Sequence:
    """tuple[T, ...]"""

    element: "Type"


@dataclass(frozen=True)
class Tuple:
    """tuple[A, B, ...]: a fixed number of elements"""

    elements: "tuple[Type, ...]"


@dataclass(frozen=True)
class Set:
    """frozenset[T], of scalars or enums"""

    element: "Type"


@dataclass(frozen=True)
class Map:
    """Mapping[K, V], keyed by scalars or enums"""

    key: "Type"
    value: "Type"


@dataclass(frozen=True)
class Union:
    """Two or more members, never Optional. Serialised with a tag if discriminated"""

    members: "tuple[Type, ...]"
    discriminated: bool = False


@dataclass(frozen=True)
class Shared:
    """A value shared rather than copied, serialised by reference"""

    target: StructRef


@dataclass(frozen=True)
class Indirect:
    """
    A struct held by value but stored out of line, as it is part of a recursive cycle of types. Inserted where needed
    by introspection, so that all backends break cycles at the same fields
    """

    target: StructRef


type Scalar = Bool | Int | Float | Str | Bytes | Date | Time | DateTime | TimeDelta
type Type = (
    Scalar
    | EnumRef
    | StructRef
    | Optional
    | Sequence
    | Tuple
    | Set
    | Map
    | Union
    | Shared
    | Indirect
)

SCALARS = (Bool, Int, Float, Str, Bytes, Date, Time, DateTime, TimeDelta)


class _NoDefault:
    def __repr__(self) -> str:
        return "NO_DEFAULT"


NO_DEFAULT: Any = _NoDefault()


@dataclass(frozen=True)
class Field:
    name: str
    type: Type
    default: Any = NO_DEFAULT
    """A Python value conforming to type, from the field's default or default_factory"""

    @property
    def has_default(self) -> bool:
        return self.default is not NO_DEFAULT


@dataclass(frozen=True)
class Struct:
    module: str
    name: str
    tag: str
    fields: tuple[Field, ...]
    """All fields, including those inherited, in dataclass order"""
    cls: type = field(compare=False, repr=False)

    @property
    def ref(self) -> StructRef:
        return StructRef(self.module, self.name, self.cls)


@dataclass(frozen=True)
class EnumMember:
    name: str
    value: int
    """The Python value if an int, else the member's position"""


@dataclass(frozen=True)
class Enum:
    module: str
    name: str
    tag: str
    members: tuple[EnumMember, ...]
    cls: type[PyEnum] = field(compare=False, repr=False)


@dataclass(frozen=True)
class Module:
    name: str
    enums: tuple[Enum, ...]
    structs: tuple[Struct, ...]
    """Ordered so that each struct follows those it holds by value"""
    dependencies: tuple[str, ...]
    """Other modules whose types this module's structs use"""


@dataclass(frozen=True)
class Schema:
    modules: Mapping[str, Module]

    def struct(self, ref: StructRef) -> Struct:
        return next(s for s in self.modules[ref.module].structs if s.name == ref.name)

    def enum(self, ref: EnumRef) -> Enum:
        return next(e for e in self.modules[ref.module].enums if e.name == ref.name)


def is_key(t: Type) -> bool:
    """Whether t may be a frozenset element or Mapping key: scalars and enums only"""
    return isinstance(t, SCALARS + (EnumRef,))


def wire_kind(t: Type) -> tuple[str, ...]:
    """How t is represented when serialised: plain union members must differ by this, else need discrimination"""
    match t:
        case Bool():
            return ("bool",)
        case Int() | Date() | Time() | TimeDelta():
            return ("int",)
        case Float():
            return ("float",)
        case Str() | EnumRef():
            return ("str",)
        case Bytes():
            return ("bin",)
        case DateTime():
            return ("timestamp",)
        case StructRef() | Indirect() | Map():
            return ("map",)
        case Sequence() | Tuple() | Set():
            return ("array",)
        case Shared(target):
            # The reference names its target's type
            return ("ref", target.module, target.name)
        case Union(discriminated=True):
            return ("map",)
        case Optional() | Union():
            raise ValueError(f"{t} is not a union member")


def conforms(t: Type, value: Any, schema: "Schema | None" = None) -> bool:
    """Whether value is valid for t: with schema, also checks the fields of struct values"""
    match t:
        case Bool():
            return type(value) is bool
        case Int():
            return type(value) is int and t.min <= value <= t.max
        case Float():
            return type(value) in (float, int)
        case Str():
            return type(value) is str
        case Bytes():
            return type(value) is bytes
        case Date():
            return type(value) is dt.date
        case Time():
            return type(value) is dt.time and value.tzinfo is None
        case DateTime():
            return type(value) is dt.datetime and value.utcoffset() is not None
        case TimeDelta():
            return type(value) is dt.timedelta
        case EnumRef(cls=cls):
            return type(value) is cls
        case StructRef() | Shared(target=StructRef()) | Indirect(target=StructRef()):
            ref = t if isinstance(t, StructRef) else t.target
            if type(value) is not ref.cls:
                return False
            return schema is None or all(
                conforms(f.type, getattr(value, f.name), schema)
                for f in schema.struct(ref).fields
            )
        case Optional(inner):
            return value is None or conforms(inner, value, schema)
        case Sequence(element):
            return type(value) is tuple and all(
                conforms(element, v, schema) for v in value
            )
        case Tuple(elements):
            return (
                type(value) is tuple
                and len(value) == len(elements)
                and all(
                    conforms(e, v, schema) for e, v in zip(elements, value, strict=True)
                )
            )
        case Set(element):
            return type(value) is frozenset and all(
                conforms(element, v, schema) for v in value
            )
        case Map(key, val):
            return isinstance(value, Mapping) and all(
                conforms(key, k, schema) and conforms(val, v, schema)
                for k, v in value.items()
            )
        case Union(members):
            return any(conforms(m, value, schema) for m in members)


def union_member(u: Union, value: Any, schema: "Schema | None" = None) -> int:
    """The index of the first member of u that value conforms to"""
    for i, member in enumerate(u.members):
        if conforms(member, value, schema):
            return i
    raise ValueError(f"{value!r} conforms to no member of {u}")
