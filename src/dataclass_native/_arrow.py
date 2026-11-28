"""
Arrow tables of @native objects, and Parquet files of those tables, in the layout of ARROW_FORMAT.md.

write() and read() are the reference implementation of that layout, in Python: they get and set the fields of @native
objects as any Python code does, so work in every mode. to_arrow() and from_arrow() use them, unless the objects are
stored natively: then the C++ of dataclass_native/arrow.hpp builds and reads the tables from the native objects
themselves. pyarrow is needed only to call any of these.
"""

import datetime as dt
import struct
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote, unquote
from weakref import WeakKeyDictionary

from . import schema as s
from ._native import is_native, native_info
from ._wire import (
    _array,
    _brief,
    _Invalid,
    _member,
    _native_class,
    _numbers,
    _order,
    _ordered,
    _struct_class,
)
from ._wire import _codec as _wire_codec
from .introspect import _tag, describe, walk
from .schema import SchemaError

TAG = b"dataclass_native:tag"
"""In a table's metadata: the tag of the class its rows are of"""
SHARED = b"dataclass_native:shared"
"""In the metadata of a field which refers to a shared object: the tag of that object's class, and so its table"""

_BYTE = s.Int(8, signed=False)


class ArrowTables(NamedTuple):
    """Objects as Arrow tables, a pyarrow.Table each"""

    root: Any
    """A row for each object"""
    shared: dict[str, Any]
    """By its tag, the table of each class the objects may share: a row for each object shared"""


def to_arrow(values: Iterable[Any], cls: type | None = None) -> ArrowTables:
    """
    Objects of a @native class as Arrow tables: a row each, and a column for each field. Objects stored natively are
    held natively.

    :param cls: Their class, which need be given only if there may be none
    """
    values = list(values)
    cls = _class(values, cls)
    native = _native(cls)
    if native is None:
        return write(values, cls)

    from ._runtime import native_object

    pa = _pyarrow()
    objects = []
    for value in values:
        if type(value) is not cls:
            raise TypeError(f"{value!r} is not a {cls.__qualname__}")
        objects.append(native_object(value))
    root, shared = native.__arrow__(objects)
    return ArrowTables(
        _imported(pa, root),
        {tag: _imported(pa, capsules) for tag, capsules in shared},
    )


def from_arrow(cls: type, tables: Any) -> tuple[Any, ...]:
    """
    The objects of a @native class which Arrow tables hold. Raises ValueError if they are not tables of that class.

    :param tables: As to_arrow gives. Or the pyarrow.Table of the objects alone, if their class shares none
    """
    native = _native(cls)
    # A pydantic dataclass is made in Python, so that pydantic validates it
    if native is not None and not _wire_codec(cls).validated:
        from ._runtime import wrap

        pa = _pyarrow()
        root, shared = tables if isinstance(tables, tuple) else (tables, {})
        objects = native.__from_arrow__(
            *_exported(pa, root),
            {tag: _exported(pa, table) for tag, table in shared.items()},
        )
        # None if it does not read them: then what follows does, or says why not
        if objects is not None:
            return tuple(wrap(cls, obj) for obj in objects)
    return read(cls, tables)


def _native(cls: Any) -> Any:
    """The native class of a bound class, if that holds its objects in Arrow tables itself"""
    native = _native_class(cls)
    return native if hasattr(native, "__arrow__") else None


class _Capsules:
    """A table as the Arrow PyCapsule interface exports it, which pyarrow imports"""

    def __init__(self, capsules: tuple[Any, Any]):
        self.capsules = capsules

    def __arrow_c_array__(self, requested_schema: Any = None) -> tuple[Any, Any]:
        return self.capsules


def _imported(pa: Any, capsules: tuple[Any, Any]) -> Any:
    return pa.Table.from_batches([pa.record_batch(_Capsules(capsules))])


def _exported(pa: Any, table: Any) -> tuple[Any, Any]:
    """The capsules of a table, as one array"""
    if isinstance(table, pa.Table):
        batches = table.combine_chunks().to_batches()
        table = (
            batches[0]
            if len(batches) == 1
            else pa.RecordBatch.from_pylist([], schema=table.schema)
        )
    return table.__arrow_c_array__()


def _class(values: list[Any], cls: type | None) -> type:
    if cls is not None:
        return cls
    if not values:
        raise TypeError("The class of no objects must be given")
    return type(values[0])


def write(values: Iterable[Any], cls: type | None = None) -> ArrowTables:
    """As to_arrow, in Python alone"""
    pa = _pyarrow()
    values = list(values)
    cls = _class(values, cls)
    codec = _codec(cls)
    # Before any is written, as a class may not have one
    schemas = {c.tag: c.schema(pa) for c in (codec, *map(_codec, codec.shared))}

    writer = _Writer({_codec(c).tag: [] for c in codec.shared})
    rows = []
    for value in values:
        try:
            rows.append(codec.plain(value, writer))
        except _Invalid:
            raise TypeError(f"{value!r} is not a {cls.__qualname__}") from None

    return ArrowTables(
        pa.Table.from_pylist(rows, schema=schemas[codec.tag]),
        {
            tag: pa.Table.from_pylist(shared, schema=schemas[tag])
            for tag, shared in writer.tables.items()
        },
    )


def read(cls: type, tables: Any) -> tuple[Any, ...]:
    """As from_arrow, in Python alone"""
    codec = _codec(cls)
    root, shared = tables if isinstance(tables, tuple) else (tables, {})
    reader = _Reader({tag: table.to_pylist() for tag, table in shared.items()})
    objects = []
    for row in root.to_pylist():
        try:
            objects.append(codec.object(row, reader))
        except _Invalid:
            raise ValueError(
                f"The table does not hold {cls.__qualname__}: {_brief(row)}"
            ) from None
    return tuple(objects)


def to_parquet(values: Iterable[Any], directory: str | Path, cls: type | None = None):
    """
    Writes objects of a @native class as Parquet: root.parquet, and a file in shared/ for each class they may share.

    :param cls: Their class, which need be given only if there may be none
    """
    pq = _parquet()
    tables = to_arrow(values, cls)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(tables.root, directory / "root.parquet")
    for tag, table in tables.shared.items():
        (directory / "shared").mkdir(exist_ok=True)
        pq.write_table(table, directory / "shared" / f"{quote(tag, safe='')}.parquet")


def from_parquet(cls: type, directory: str | Path) -> tuple[Any, ...]:
    """The objects of a @native class which to_parquet wrote"""
    pq = _parquet()
    directory = Path(directory)
    shared = {
        unquote(path.stem): pq.read_table(path)
        for path in sorted((directory / "shared").glob("*.parquet"))
    }
    return from_arrow(
        cls, ArrowTables(pq.read_table(directory / "root.parquet"), shared)
    )


def _pyarrow() -> Any:
    try:
        import pyarrow
    except ImportError:
        raise ImportError(
            "pyarrow is needed for Arrow and Parquet: install dataclass-native[arrow]"
        ) from None
    return pyarrow


def _parquet() -> Any:
    _pyarrow()
    import pyarrow.parquet

    return pyarrow.parquet


class _Writer:
    """The shared objects of what is being written"""

    def __init__(self, tables: dict[str, list[Any]]):
        self.tables = tables
        """By tag: the objects, as plain rows"""
        self.rows: dict[int, int] = {}
        """By id of a shared object, its row in its class's table"""
        self.shared: list[Any] = []
        """The shared objects, kept so that no other object comes to have an id of theirs"""


class _Reader:
    """The shared objects of what is being read"""

    def __init__(self, tables: dict[str, list[Any]]):
        self.tables = tables
        self.objects: dict[tuple[str, int], Any] = {}
        """By tag and row, the objects read so far: _READING while one is being read"""


_READING = object()

# Makes a value plain: of what pyarrow makes an array of. And an object of what pyarrow gives for a value of one
type _Plain = Callable[[Any, _Writer], Any]
type _Object = Callable[[Any, _Reader], Any]


class _Codec:
    """Writes and reads the objects of a class as the rows of a table"""

    def __init__(self, cls: type):
        if not (isinstance(cls, type) and is_native(cls)):
            raise TypeError(f"{cls!r} is not a @native class")
        struct_ = describe(cls)
        assert isinstance(struct_, s.Struct)
        self.cls = cls
        self.tag = struct_.tag
        self.struct = struct_
        self.shared = _shared_classes(struct_.ref, [], set())
        """The classes its objects may share, however deeply"""
        self.fields = [
            (f.name, _plain(f.type), _object(f.type)) for f in struct_.fields
        ]

    def schema(self, pa: Any) -> Any:
        fields = [_field(f.name, f.type, pa, (self.cls,)) for f in self.struct.fields]
        return pa.schema(fields, metadata={TAG: self.tag})

    def plain(self, value: Any, writer: _Writer) -> dict[str, Any]:
        # Exactly, as a subclass is another struct
        if type(value) is not self.cls:
            raise _Invalid
        row = {}
        for name, plain, _ in self.fields:
            item = getattr(value, name)
            try:
                row[name] = plain(item, writer)
            except _Invalid:
                raise TypeError(
                    f"{self.cls.__qualname__}.{name} cannot be {item!r}"
                ) from None
        return row

    def object(self, row: Any, reader: _Reader) -> Any:
        if type(row) is not dict:
            raise _Invalid
        # Those which are columns: one which is not takes its default, and a column which is not a field is ignored
        fields = {}
        for name, _, make in self.fields:
            if name in row:
                try:
                    fields[name] = make(row[name], reader)
                except _Invalid:
                    raise ValueError(
                        f"{self.cls.__qualname__}.{name} cannot be read from {_brief(row[name])}"
                    ) from None
        try:
            return self.cls(**fields)
        except TypeError as e:
            # A field with no default has no column, or a native field rejects what its column holds
            raise ValueError(f"{self.cls.__qualname__} cannot be read: {e}") from None


_codecs: WeakKeyDictionary[type, _Codec] = WeakKeyDictionary()


def _codec(cls: type) -> _Codec:
    try:
        return _codecs[cls]
    except (KeyError, TypeError):
        codec = _Codec(cls)
        _codecs[cls] = codec
        return codec


def _shared_classes(t: s.Type, shared: list[type], seen: set[type]) -> list[type]:
    """The classes which a value of the type may share, however deeply, in the order met"""
    for inner in walk(t):
        if isinstance(inner, s.Shared) and inner.target.cls not in shared:
            shared.append(inner.target.cls)
        if isinstance(inner, s.StructRef) and inner.cls not in seen:
            seen.add(inner.cls)
            struct_ = describe(inner.cls)
            assert isinstance(struct_, s.Struct)
            for f in struct_.fields:
                _shared_classes(f.type, shared, seen)
    return shared


def _member_name(member: s.Type) -> str:
    """What names a union's member, as a field of the struct the union is: its tag, else what msgpack writes it as"""
    if isinstance(member, s.Sequence) and member.element == _BYTE:
        # As bytes, which it is natively
        return "bytes"
    return _tag(member) or s.wire_kind(member)[0]


def _member_names(members: tuple[s.Type, ...]) -> list[str]:
    names = [_member_name(member) for member in members]
    if len(set(names)) != len(names):
        raise SchemaError(
            f"The members of a union have the same name in Arrow: {names}"
        )
    return names


def _field(name: str, t: s.Type, pa: Any, within: tuple[type, ...]) -> Any:
    """The Arrow field of a type: one of a class in within is of a class which holds itself"""
    inner = t.inner if isinstance(t, s.Optional) else t
    metadata = (
        {SHARED: native_info(inner.target.cls).tag}
        if isinstance(inner, s.Shared)
        else None
    )
    return pa.field(name, _type(inner, pa, within), metadata=metadata)


def _type(t: s.Type, pa: Any, within: tuple[type, ...]) -> Any:
    match t:
        case s.Bool():
            return pa.bool_()
        case s.Int(bits, signed):
            return getattr(pa, f"{'' if signed else 'u'}int{bits}")()
        case s.Float(bits):
            return pa.float32() if bits == 32 else pa.float64()
        case s.Str():
            return pa.string()
        case s.Bytes():
            return pa.binary()
        case s.Date():
            return pa.date32()
        case s.Time():
            return pa.time64("us")
        case s.DateTime():
            return pa.timestamp("us", tz="UTC")
        case s.TimeDelta():
            return pa.duration("us")
        case s.EnumRef():
            return pa.dictionary(pa.int32(), pa.string())
        case s.Optional(inner):
            return _type(inner, pa, within)
        case s.Sequence(element) if element == _BYTE:
            return pa.binary()
        case s.Sequence(element) | s.Set(element):
            return pa.list_(_field("item", element, pa, within))
        case s.Array(element, dimensions):
            numbers = _type(s.Sequence(element), pa, within)
            if dimensions == 1:
                return numbers
            # As Arrow's arrow.variable_shape_tensor is stored
            shape = pa.list_(pa.field("item", pa.int32()), dimensions)
            return pa.struct([pa.field("data", numbers), pa.field("shape", shape)])
        case s.Tuple(elements):
            return pa.struct(
                [_field(str(i), e, pa, within) for i, e in enumerate(elements)]
            )
        case s.Map(key, value):
            return pa.map_(
                _type(key, pa, within),
                _field("value", value, pa, within),
                keys_sorted=True,
            )
        case s.Union(members):
            names = _member_names(members)
            return pa.struct(
                [_field(n, m, pa, within) for n, m in zip(names, members, strict=True)]
            )
        case s.Shared():
            return pa.uint32()
        case s.StructRef(cls=cls) | s.Indirect(s.StructRef(cls=cls)):
            if cls in within:
                raise SchemaError(
                    f"{within[0].__qualname__} cannot be written as Arrow, as {cls.__qualname__} holds itself: "
                    "a table's columns cannot. A Shared field may hold its own class"
                )
            struct_ = describe(cls)
            assert isinstance(struct_, s.Struct)
            return pa.struct(
                [_field(f.name, f.type, pa, (*within, cls)) for f in struct_.fields]
            )


def _exactly(typ: type) -> _Plain:
    def plain(value: Any, writer: _Writer) -> Any:
        if type(value) is not typ:
            raise _Invalid
        return value

    return plain


def _plain(t: s.Type) -> _Plain:
    """Makes a value of the type plain, raising _Invalid if it is not one"""
    if cls := _struct_class(t):
        if not isinstance(t, s.Shared):
            # Its codec is found when used, not now, as the class may share itself
            return lambda value, writer: _codec(cls).plain(value, writer)

        def share(value: Any, writer: _Writer) -> int:
            codec = _codec(cls)
            if type(value) is not cls:
                raise _Invalid
            row = writer.rows.get(id(value))
            if row is None:
                rows = writer.tables[codec.tag]
                row = writer.rows[id(value)] = len(rows)
                writer.shared.append(value)
                # Its row is taken before it is written, as it may share objects of its own class
                rows.append(None)
                rows[row] = codec.plain(value, writer)
            return row

        return share

    match t:
        case (
            s.Bool()
            | s.Int()
            | s.Str()
            | s.Bytes()
            | s.Date()
            | s.Time()
            | s.DateTime()
            | s.TimeDelta()
        ):

            def scalar(value: Any, writer: _Writer) -> Any:
                if not s.conforms(t, value):
                    raise _Invalid
                return value

            return scalar

        case s.Float(bits=bits):

            def number(value: Any, writer: _Writer) -> float:
                if type(value) not in (float, int):
                    raise _Invalid
                try:
                    if bits == 32:
                        struct.pack(">f", value)
                    return float(value)
                except (OverflowError, struct.error):
                    raise _Invalid from None

            return number

        case s.EnumRef(cls=enum):
            names = {
                value: member.name for value, member in s.enum_members(enum).items()
            }

            def member(value: Any, writer: _Writer) -> str:
                try:
                    return names[value]
                except (KeyError, TypeError):
                    raise _Invalid from None

            return member

        case s.Optional(inner):
            plain = _plain(inner)
            return lambda value, writer: None if value is None else plain(value, writer)

        case s.Sequence(element) if element == _BYTE:

            def octets(value: Any, writer: _Writer) -> bytes:
                if type(value) is not tuple:
                    raise _Invalid
                try:
                    return bytes(value)
                except (TypeError, ValueError):
                    raise _Invalid from None

            return octets

        case s.Sequence(element) | s.Set(element):
            plain = _plain(element)
            typ, key = (
                (frozenset, _order(element)) if isinstance(t, s.Set) else (tuple, None)
            )

            def array(value: Any, writer: _Writer) -> list[Any]:
                if type(value) is not typ:
                    raise _Invalid
                items = _ordered(value, key) if typ is frozenset else value
                return [plain(item, writer) for item in items]

            return array

        case s.Array(element, dimensions):
            numbers, plain = _numbers(t), _plain(s.Sequence(element))
            if dimensions == 1:
                return lambda value, writer: plain(numbers(value), writer)
            return lambda value, writer: {
                "data": plain(numbers(value), writer),
                "shape": list(value.shape),
            }

        case s.Tuple(elements):
            plains = [_plain(e) for e in elements]

            def fixed(value: Any, writer: _Writer) -> dict[str, Any]:
                if type(value) is not tuple or len(value) != len(plains):
                    raise _Invalid
                return {
                    str(i): plain(item, writer)
                    for i, (plain, item) in enumerate(zip(plains, value, strict=True))
                }

            return fixed

        case s.Map(key_type, value_type):
            plain_key, plain_value = _plain(key_type), _plain(value_type)
            key = _order(key_type)

            def mapping(value: Any, writer: _Writer) -> list[tuple[Any, Any]]:
                if not isinstance(value, Mapping):
                    raise _Invalid
                return [
                    (plain_key(k, writer), plain_value(value[k], writer))
                    for k in _ordered(value, key)
                ]

            return mapping

        case s.Union(members):
            plains = [_plain(member) for member in members]
            names = _member_names(members)

            def union(value: Any, writer: _Writer) -> dict[str, Any]:
                index = _member(members, value)
                return {names[index]: plains[index](value, writer)}

            return union

        case _:
            raise AssertionError(t)


def _of(typ: type, convert: Callable[[Any], Any] | None = None) -> _Object:
    def make(plain: Any, reader: _Reader) -> Any:
        if type(plain) is not typ:
            raise _Invalid
        return convert(plain) if convert else plain

    return make


def _object(t: s.Type) -> _Object:
    """Makes a value of the type from what pyarrow gives for one, raising _Invalid if that is not one"""
    if cls := _struct_class(t):
        if not isinstance(t, s.Shared):
            return lambda plain, reader: _codec(cls).object(plain, reader)

        def shared(plain: Any, reader: _Reader) -> Any:
            codec = _codec(cls)
            rows = reader.tables.get(codec.tag)
            if type(plain) is not int or rows is None or not 0 <= plain < len(rows):
                raise _Invalid
            key = (codec.tag, plain)
            obj = reader.objects.get(key)
            if obj is None:
                reader.objects[key] = _READING
                obj = reader.objects[key] = codec.object(rows[plain], reader)
            elif obj is _READING:
                # It shares itself, which no object written could
                raise _Invalid
            return obj

        return shared

    match t:
        case s.Bool():
            return _of(bool)

        case s.Int(min=low, max=high):

            def integer(plain: Any, reader: _Reader) -> int:
                if type(plain) is not int or not low <= plain <= high:
                    raise _Invalid
                return plain

            return integer

        case s.Float():
            return _of(float)
        case s.Str():
            return _of(str)
        case s.Bytes():
            return _of(bytes)
        case s.Date():
            return _of(dt.date)
        case s.Time():
            return _of(dt.time)
        case s.TimeDelta():
            return _of(dt.timedelta)

        case s.DateTime():

            def instant(plain: Any, reader: _Reader) -> dt.datetime:
                if type(plain) is not dt.datetime or plain.utcoffset() is None:
                    raise _Invalid
                return plain.astimezone(dt.UTC)

            return instant

        case s.EnumRef(cls=enum):
            members = {member.name: member for member in enum}

            def member(plain: Any, reader: _Reader) -> Any:
                try:
                    return members[plain]
                except (KeyError, TypeError):
                    raise _Invalid from None

            return member

        case s.Optional(inner):
            make = _object(inner)
            return lambda plain, reader: None if plain is None else make(plain, reader)

        case s.Sequence(element) if element == _BYTE:
            return _of(bytes, tuple)

        case s.Sequence(element) | s.Set(element):
            make = _object(element)
            container = frozenset if isinstance(t, s.Set) else tuple

            def array(plain: Any, reader: _Reader) -> Any:
                if type(plain) is not list:
                    raise _Invalid
                return container(make(item, reader) for item in plain)

            return array

        case s.Array(element, dimensions):
            held, make = _array(t), _object(s.Sequence(element))
            if dimensions == 1:
                return lambda plain, reader: held(make(plain, reader))
            make_shape = _object(s.Sequence(s.Int(32)))

            def shaped(plain: Any, reader: _Reader) -> Any:
                if type(plain) is not dict or plain.keys() != {"data", "shape"}:
                    raise _Invalid
                return held(
                    make(plain["data"], reader), make_shape(plain["shape"], reader)
                )

            return shaped

        case s.Tuple(elements):
            makes = [_object(e) for e in elements]

            def fixed(plain: Any, reader: _Reader) -> tuple[Any, ...]:
                if type(plain) is not dict:
                    raise _Invalid
                try:
                    return tuple(
                        make(plain[str(i)], reader) for i, make in enumerate(makes)
                    )
                except KeyError:
                    raise _Invalid from None

            return fixed

        case s.Map(key_type, value_type):
            make_key, make_value = _object(key_type), _object(value_type)

            def mapping(plain: Any, reader: _Reader) -> dict[Any, Any]:
                if type(plain) is not list:
                    raise _Invalid
                items = {make_key(k, reader): make_value(v, reader) for k, v in plain}
                if len(items) != len(plain):
                    # A key twice
                    raise _Invalid
                return items

            return mapping

        case s.Union(members):
            makes = dict(
                zip(_member_names(members), map(_object, members), strict=True)
            )

            def union(plain: Any, reader: _Reader) -> Any:
                if type(plain) is not dict:
                    raise _Invalid
                # The one member which is not null
                held = [
                    (n, v) for n, v in plain.items() if v is not None and n in makes
                ]
                if len(held) != 1:
                    raise _Invalid
                return makes[held[0][0]](held[0][1], reader)

            return union

        case _:
            raise AssertionError(t)
