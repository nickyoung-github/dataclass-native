"""
Reads and writes values of the schema's types as msgpack, in the wire format of WIRE_FORMAT.md.

This is the reference implementation of that format, in Python alone: it gets and sets the fields of @native objects
as any Python code does, so works in every mode.
"""

import datetime as dt
import struct
from collections.abc import Callable, Mapping
from typing import Any, overload
from weakref import WeakKeyDictionary

from . import _msgpack as m
from . import schema as s
from ._native import is_native, native_info
from .introspect import _tag, describe, resolve, walk

REF = 1
"""The extension type of a reference to a shared object: the index of its table, and its row in it"""
TIMESTAMP = -1

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_EPOCH_DAY = dt.date(1970, 1, 1).toordinal()
_MICROSECOND = dt.timedelta(microseconds=1)
_SECOND = dt.timedelta(seconds=1)
_DAY_MICROSECONDS = 86_400_000_000
_BYTE = s.Int(8, signed=False)


class _Invalid(Exception):
    """A value is not of the type being written, or what is being read is not that type as written"""


class _Writer:
    """The shared objects of what is being written"""

    def __init__(self):
        self.tables: dict[str, list[bytes]] = {}
        """By tag, in the order first used: the objects, as written"""
        self.references: dict[int, tuple[int, int]] = {}
        """By id of a shared object, the index of its table and its row"""
        self.shared: list[Any] = []
        """The shared objects, kept so that no other object comes to have an id of theirs"""


class _Reader:
    """The shared objects of what is being read"""

    def __init__(self, tables: list[tuple[str, list[Any]]]):
        self.tables = tables
        self.objects: dict[tuple[int, int], Any] = {}
        """By reference, the objects read so far: _READING while one is being read"""


_READING = object()

type _Encode = Callable[[Any, bytearray, _Writer], None]
type _Decode = Callable[[Any, _Reader], Any]


@overload
def from_msgpack[T](tp: type[T], data: bytes) -> T: ...


@overload
def from_msgpack(tp: Any, data: bytes) -> Any: ...


def to_msgpack(value: Any, tp: Any = None) -> bytes:
    """
    A value as msgpack, in the wire format.

    :param tp: Its type, if not a @native class: any type a field may have, e.g. tuple[Trade, ...]
    """
    encode, _, bundled = _root(type(value) if tp is None else tp)
    writer = _Writer()
    root = bytearray()
    try:
        encode(value, root, writer)
    except _Invalid:
        raise TypeError(
            f"{value!r} cannot be written as {_name(tp or type(value))}"
        ) from None
    if not bundled:
        return bytes(root)

    out = bytearray()
    m.pack_map(out, 2)
    m.pack_str(out, "root")
    out += root
    m.pack_str(out, "shared")
    m.pack_array(out, len(writer.tables))
    for tag, rows in writer.tables.items():
        m.pack_array(out, 2)
        m.pack_str(out, tag)
        m.pack_array(out, len(rows))
        for row in rows:
            out += row
    return bytes(out)


def from_msgpack(tp: Any, data: bytes) -> Any:
    """
    The value of a type, read from msgpack in the wire format. Raises ValueError if the data is not that.

    :param tp: A @native class, or any type a field may have
    """
    _, decode, bundled = _root(tp)
    wire = m.unpack(data)
    reader = _Reader([])
    try:
        if bundled:
            wire, reader = _unbundle(wire)
        return decode(wire, reader)
    except _Invalid:
        raise ValueError(
            f"The data is not a {_name(tp)}, as written: {_brief(wire)}"
        ) from None


def _name(tp: Any) -> str:
    return tp.__qualname__ if isinstance(tp, type) else str(tp)


def _brief(wire: Any) -> str:
    text = repr(wire)
    return text if len(text) <= 80 else f"{text[:77]}..."


def _unbundle(wire: Any) -> tuple[Any, _Reader]:
    if (
        type(wire) is not dict
        or "root" not in wire
        or type(wire.get("shared")) is not list
    ):
        raise _Invalid
    tables = []
    for table in wire["shared"]:
        if type(table) is not list or len(table) != 2:
            raise _Invalid
        tag, rows = table
        if type(tag) is not str or type(rows) is not list:
            raise _Invalid
        tables.append((tag, rows))
    return wire["root"], _Reader(tables)


def _root(tp: Any) -> tuple[_Encode, _Decode, bool]:
    """To write and to read a type, and whether it is written as a bundle: with the objects it shares"""
    if isinstance(tp, type) and is_native(tp):
        codec = _codec(tp)
        return codec.encode, codec.decode, codec.bundled
    t = resolve(tp, _name(tp))
    return _encoder(t), _decoder(t), _reaches_shared(t, set())


def _reaches_shared(t: s.Type, seen: set[type]) -> bool:
    """Whether a value of the type may hold, however deeply, a Shared one"""
    for inner in walk(t):
        if isinstance(inner, s.Shared):
            return True
        if isinstance(inner, s.StructRef) and inner.cls not in seen:
            seen.add(inner.cls)
            struct = describe(inner.cls)
            assert isinstance(struct, s.Struct)
            if any(_reaches_shared(f.type, seen) for f in struct.fields):
                return True
    return False


class _Codec:
    """Writes and reads a struct"""

    def __init__(self, cls: type):
        struct = describe(cls)
        assert isinstance(struct, s.Struct)
        self.cls = cls
        self.tag = struct.tag
        self.bundled = _reaches_shared(struct.ref, set())
        self.header = _packed(m.pack_map, len(struct.fields))
        # Of each field: its name, that as written, and how to write and read it
        self.fields = [
            (f.name, _packed(m.pack_str, f.name), _encoder(f.type), _decoder(f.type))
            for f in struct.fields
        ]

    def encode(self, value: Any, out: bytearray, writer: _Writer):
        # Exactly, as a subclass is another struct
        if type(value) is not self.cls:
            raise _Invalid
        out += self.header
        for name, key, encode, _ in self.fields:
            out += key
            item = getattr(value, name)
            try:
                encode(item, out, writer)
            except _Invalid:
                raise TypeError(
                    f"{self.cls.__qualname__}.{name} cannot be {item!r}"
                ) from None

    def decode(self, wire: Any, reader: _Reader) -> Any:
        if type(wire) is not dict:
            raise _Invalid
        # Those written: one which is not takes its default, and one which is not a field is ignored
        fields = {}
        for name, _, _, decode in self.fields:
            if name in wire:
                try:
                    fields[name] = decode(wire[name], reader)
                except _Invalid:
                    raise ValueError(
                        f"{self.cls.__qualname__}.{name} cannot be read from {_brief(wire[name])}"
                    ) from None
        try:
            return self.cls(**fields)
        except TypeError as e:
            # A field with no default was not written, or a native field rejects what was
            raise ValueError(f"{self.cls.__qualname__} cannot be read: {e}") from None


_codecs: WeakKeyDictionary[type, _Codec] = WeakKeyDictionary()


def _codec(cls: type) -> _Codec:
    try:
        return _codecs[cls]
    except KeyError:
        codec = _codecs[cls] = _Codec(cls)
        return codec


def _packed(pack: Callable[[bytearray, Any], None], value: Any) -> bytes:
    out = bytearray()
    pack(out, value)
    return bytes(out)


def _struct_class(t: s.Type) -> type | None:
    match t:
        case (
            s.StructRef(cls=cls)
            | s.Shared(s.StructRef(cls=cls))
            | s.Indirect(s.StructRef(cls=cls))
        ):
            return cls
        case _:
            return None


def _order(t: s.Type) -> Callable[[Any], Any] | None:
    """The key by which the elements of a set, or the keys of a map, are ordered when written"""
    if not isinstance(t, s.EnumRef):
        # As they are natively: numbers and times by value, str by code point and bytes by byte
        return None
    enum = describe(t.cls)
    assert isinstance(enum, s.Enum)
    return {t.cls[member.name]: member.value for member in enum.members}.__getitem__


def _ordered(values: Any, key: Callable[[Any], Any] | None) -> list[Any]:
    try:
        return sorted(values, key=key)
    except (TypeError, KeyError):
        # Not all of the type, so they have no order
        raise _Invalid from None


def _exactly(typ: type, pack: Callable[[bytearray, Any], None]) -> _Encode:
    def encode(value: Any, out: bytearray, writer: _Writer):
        if type(value) is not typ:
            raise _Invalid
        pack(out, value)

    return encode


def _encoder(t: s.Type) -> _Encode:
    """Writes a value of the type, raising _Invalid if it is not one"""
    if cls := _struct_class(t):
        if not isinstance(t, s.Shared):
            # Its codec is found when used, not now, as the type may be recursive
            return lambda value, out, writer: _codec(cls).encode(value, out, writer)

        def share(value: Any, out: bytearray, writer: _Writer):
            codec = _codec(cls)
            if type(value) is not cls:
                raise _Invalid
            reference = writer.references.get(id(value))
            if reference is None:
                rows = writer.tables.setdefault(codec.tag, [])
                reference = (list(writer.tables).index(codec.tag), len(rows))
                writer.references[id(value)] = reference
                writer.shared.append(value)
                # Its row is taken before it is written, as it may share objects of its own type
                rows.append(b"")
                row = bytearray()
                codec.encode(value, row, writer)
                rows[reference[1]] = bytes(row)
            m.pack_ext(out, REF, struct.pack(">II", *reference))

        return share

    match t:
        case s.Bool():
            return _exactly(bool, m.pack_bool)

        case s.Int(min=low, max=high):

            def integer(value: Any, out: bytearray, writer: _Writer):
                if type(value) is not int or not low <= value <= high:
                    raise _Invalid
                m.pack_int(out, value)

            return integer

        case s.Float(bits=bits):

            def number(value: Any, out: bytearray, writer: _Writer):
                if type(value) not in (float, int):
                    raise _Invalid
                try:
                    m.pack_float(out, value, bits)
                except (OverflowError, struct.error):
                    raise _Invalid from None

            return number

        case s.Str():

            def text(value: Any, out: bytearray, writer: _Writer):
                if type(value) is not str:
                    raise _Invalid
                try:
                    m.pack_str(out, value)
                except UnicodeEncodeError:
                    raise _Invalid from None

            return text

        case s.Bytes():
            return _exactly(bytes, m.pack_bin)

        case s.Date():
            return _exactly(
                dt.date,
                lambda out, value: m.pack_int(out, value.toordinal() - _EPOCH_DAY),
            )

        case s.Time():

            def time(value: Any, out: bytearray, writer: _Writer):
                if type(value) is not dt.time or value.tzinfo is not None:
                    raise _Invalid
                seconds = (value.hour * 60 + value.minute) * 60 + value.second
                m.pack_int(out, seconds * 1_000_000 + value.microsecond)

            return time

        case s.DateTime():

            def instant(value: Any, out: bytearray, writer: _Writer):
                if type(value) is not dt.datetime or value.utcoffset() is None:
                    raise _Invalid
                seconds, rest = divmod(value - _EPOCH, _SECOND)
                nanoseconds = rest // _MICROSECOND * 1000
                # The shortest of msgpack's three timestamps which holds it
                if not 0 <= seconds < 1 << 34:
                    data = struct.pack(">Iq", nanoseconds, seconds)
                elif nanoseconds or seconds >= 1 << 32:
                    data = struct.pack(">Q", nanoseconds << 34 | seconds)
                else:
                    data = struct.pack(">I", seconds)
                m.pack_ext(out, TIMESTAMP, data)

            return instant

        case s.TimeDelta():
            return _exactly(
                dt.timedelta, lambda out, value: m.pack_int(out, value // _MICROSECOND)
            )

        case s.EnumRef(cls=enum):
            names = {member: _packed(m.pack_str, member.name) for member in enum}

            def member(value: Any, out: bytearray, writer: _Writer):
                try:
                    out += names[value]
                except (KeyError, TypeError):
                    raise _Invalid from None

            return member

        case s.Optional(inner):
            encode = _encoder(inner)

            def optional(value: Any, out: bytearray, writer: _Writer):
                if value is None:
                    m.pack_nil(out)
                else:
                    encode(value, out, writer)

            return optional

        case s.Sequence(element) if element == _BYTE:

            def octets(value: Any, out: bytearray, writer: _Writer):
                if type(value) is not tuple:
                    raise _Invalid
                try:
                    m.pack_bin(out, bytes(value))
                except (TypeError, ValueError):
                    raise _Invalid from None

            return octets

        case s.Sequence(element) | s.Set(element):
            encode = _encoder(element)
            typ, key = (
                (frozenset, _order(element)) if isinstance(t, s.Set) else (tuple, None)
            )

            def array(value: Any, out: bytearray, writer: _Writer):
                if type(value) is not typ:
                    raise _Invalid
                m.pack_array(out, len(value))
                for item in _ordered(value, key) if typ is frozenset else value:
                    encode(item, out, writer)

            return array

        case s.Tuple(elements):
            encoders = [_encoder(e) for e in elements]

            def fixed(value: Any, out: bytearray, writer: _Writer):
                if type(value) is not tuple or len(value) != len(encoders):
                    raise _Invalid
                m.pack_array(out, len(value))
                for encode, item in zip(encoders, value, strict=True):
                    encode(item, out, writer)

            return fixed

        case s.Map(key_type, value_type):
            encode_key, encode_value = _encoder(key_type), _encoder(value_type)
            key = _order(key_type)

            def mapping(value: Any, out: bytearray, writer: _Writer):
                if not isinstance(value, Mapping):
                    raise _Invalid
                m.pack_map(out, len(value))
                for k in _ordered(value, key):
                    encode_key(k, out, writer)
                    encode_value(value[k], out, writer)

            return mapping

        case s.Union(members, discriminated):
            encoders = [_encoder(member) for member in members]
            tags = [_packed(m.pack_str, _tag(member) or "") for member in members]

            def union(value: Any, out: bytearray, writer: _Writer):
                index = _member(members, value)
                if discriminated:
                    m.pack_map(out, 2)
                    out += _T + tags[index] + _V
                encoders[index](value, out, writer)

            return union

        case _:
            raise AssertionError(t)


_T, _V = _packed(m.pack_str, "t"), _packed(m.pack_str, "v")


def _member(members: tuple[s.Type, ...], value: Any) -> int:
    """The member of a union which a value is of: by its exact type, so an int is a float only if not an int"""
    whole = type(value) is int
    for as_float in (False, True):
        for index, member in enumerate(members):
            if (whole and isinstance(member, s.Float)) is as_float and s.conforms(
                member, value
            ):
                return index
    raise _Invalid


def _decoder(t: s.Type) -> _Decode:
    """Reads a value of the type from what msgpack holds, raising _Invalid if that is not one as written"""
    if cls := _struct_class(t):
        if not isinstance(t, s.Shared):
            return lambda wire, reader: _codec(cls).decode(wire, reader)

        def shared(wire: Any, reader: _Reader) -> Any:
            codec = _codec(cls)
            if _reference(wire, reader) != codec.tag:
                raise _Invalid
            reference = struct.unpack(">II", wire.data)
            obj = reader.objects.get(reference)
            if obj is None:
                reader.objects[reference] = _READING
                row = reader.tables[reference[0]][1][reference[1]]
                obj = reader.objects[reference] = codec.decode(row, reader)
            elif obj is _READING:
                # It shares itself, which no object written could
                raise _Invalid
            return obj

        return shared

    match t:
        case s.Bool():
            return _of(bool)

        case s.Int(min=low, max=high):

            def integer(wire: Any, reader: _Reader) -> Any:
                if type(wire) is not int or not low <= wire <= high:
                    raise _Invalid
                return wire

            return integer

        case s.Float():
            # Of either size
            return _of(float)

        case s.Str():
            return _of(str)

        case s.Bytes():
            return _of(bytes)

        case s.Date():
            return _of(int, lambda days: dt.date.fromordinal(_EPOCH_DAY + days))

        case s.Time():

            def time(wire: Any, reader: _Reader) -> Any:
                if type(wire) is not int or not 0 <= wire < _DAY_MICROSECONDS:
                    raise _Invalid
                return (dt.datetime.min + wire * _MICROSECOND).time()

            return time

        case s.DateTime():

            def instant(wire: Any, reader: _Reader) -> Any:
                if type(wire) is not m.Ext or wire.code != TIMESTAMP:
                    raise _Invalid
                match len(wire.data):
                    case 4:
                        nanoseconds, seconds = 0, struct.unpack(">I", wire.data)[0]
                    case 8:
                        both = struct.unpack(">Q", wire.data)[0]
                        nanoseconds, seconds = both >> 34, both & ((1 << 34) - 1)
                    case 12:
                        nanoseconds, seconds = struct.unpack(">Iq", wire.data)
                    case _:
                        raise _Invalid
                # A whole number of microseconds, as that is what a datetime holds
                if nanoseconds >= 1_000_000_000 or nanoseconds % 1000:
                    raise _Invalid
                try:
                    return _EPOCH + dt.timedelta(
                        seconds=seconds, microseconds=nanoseconds // 1000
                    )
                except OverflowError:
                    raise _Invalid from None

            return instant

        case s.TimeDelta():
            return _of(int, lambda microseconds: microseconds * _MICROSECOND)

        case s.EnumRef(cls=enum):
            members = {member.name: member for member in enum}

            def member(wire: Any, reader: _Reader) -> Any:
                try:
                    return members[wire]
                except (KeyError, TypeError):
                    raise _Invalid from None

            return member

        case s.Optional(inner):
            decode = _decoder(inner)
            return lambda wire, reader: None if wire is None else decode(wire, reader)

        case s.Sequence(element) if element == _BYTE:
            return _of(bytes, tuple)

        case s.Sequence(element) | s.Set(element):
            decode = _decoder(element)
            container = frozenset if isinstance(t, s.Set) else tuple

            def array(wire: Any, reader: _Reader) -> Any:
                if type(wire) is not list:
                    raise _Invalid
                return container(decode(item, reader) for item in wire)

            return array

        case s.Tuple(elements):
            decoders = [_decoder(e) for e in elements]

            def fixed(wire: Any, reader: _Reader) -> Any:
                if type(wire) is not list or len(wire) != len(decoders):
                    raise _Invalid
                return tuple(
                    decode(item, reader)
                    for decode, item in zip(decoders, wire, strict=True)
                )

            return fixed

        case s.Map(key_type, value_type):
            decode_key, decode_value = _decoder(key_type), _decoder(value_type)

            def mapping(wire: Any, reader: _Reader) -> Any:
                if type(wire) is not dict:
                    raise _Invalid
                return {
                    decode_key(k, reader): decode_value(v, reader)
                    for k, v in wire.items()
                }

            return mapping

        case s.Union(members, discriminated=True):
            by_tag = {_tag(member): _decoder(member) for member in members}

            def tagged(wire: Any, reader: _Reader) -> Any:
                if type(wire) is not dict or wire.keys() != {"t", "v"}:
                    raise _Invalid
                try:
                    decode = by_tag[wire["t"]]
                except (KeyError, TypeError):
                    raise _Invalid from None
                return decode(wire["v"], reader)

            return tagged

        case s.Union(members):
            # No two are written as the same kind of msgpack value, or with the same tag if shared
            by_kind = {_kind(member): _decoder(member) for member in members}

            def union(wire: Any, reader: _Reader) -> Any:
                kind = _KINDS.get(type(wire)) or _extension_kind(wire, reader)
                try:
                    decode = by_kind[kind]
                except KeyError:
                    raise _Invalid from None
                return decode(wire, reader)

            return union

        case _:
            raise AssertionError(t)


def _of(typ: type, convert: Callable[[Any], Any] | None = None) -> _Decode:
    def decode(wire: Any, reader: _Reader) -> Any:
        if type(wire) is not typ:
            raise _Invalid
        if convert is None:
            return wire
        try:
            return convert(wire)
        except (OverflowError, ValueError):
            raise _Invalid from None

    return decode


def _reference(wire: Any, reader: _Reader) -> str:
    """The tag of the shared object which a reference is to"""
    if type(wire) is not m.Ext or wire.code != REF or len(wire.data) != 8:
        raise _Invalid
    table, row = struct.unpack(">II", wire.data)
    if table >= len(reader.tables) or row >= len(reader.tables[table][1]):
        raise _Invalid
    return reader.tables[table][0]


_KINDS: dict[type, tuple[str, ...]] = {
    bool: ("bool",),
    int: ("int",),
    float: ("float",),
    str: ("str",),
    bytes: ("bin",),
    list: ("array",),
    dict: ("map",),
}


def _kind(member: s.Type) -> tuple[str, ...]:
    """As schema.wire_kind, but a shared struct by its tag, which is what a reference to one gives"""
    if isinstance(member, s.Shared):
        return ("ref", native_info(member.target.cls).tag)
    return s.wire_kind(member)


def _extension_kind(wire: Any, reader: _Reader) -> tuple[str, ...]:
    if type(wire) is m.Ext and wire.code == TIMESTAMP:
        return ("timestamp",)
    return ("ref", _reference(wire, reader))
