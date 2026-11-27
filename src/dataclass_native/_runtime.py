"""
Binds @native classes to their native classes, those of their module's extension (see backends.bindings).

A bound class's instances hold a native object, their handle, as __native__. Its fields are descriptors which get and
set the handle's members, converting between Python values and those of the extension: the handles of @native
classes, the extension's enums, and lists, sets and dicts.

A function marked with @native_function is bound likewise, to the function of its name in a hand-written extension:
its arguments, and what it returns, are converted as fields are.
"""

import datetime as dt
import inspect
from collections.abc import Callable, Mapping
from copyreg import __newobj__  # pyright: ignore[reportAttributeAccessIssue]
from dataclasses import MISSING, fields
from enum import Enum
from functools import wraps
from importlib import import_module
from importlib.util import resolve_name
from threading import RLock
from types import ModuleType, NoneType
from typing import Any, get_type_hints
from weakref import WeakValueDictionary

from . import schema as s
from ._mode import ENV_VAR, Mode
from ._native import NativeBindingError, extension_name
from .introspect import describe, resolve

HANDLE = "__native__"

# Converts a value, or None if it is used as it is
type _Converter = Callable[[Any], Any] | None

_SCALARS: dict[type, type] = {
    s.Bool: bool,
    s.Int: int,
    s.Float: float,
    s.Str: str,
    s.Bytes: bytes,
    s.Date: dt.date,
    s.Time: dt.time,
    s.DateTime: dt.datetime,
    s.TimeDelta: dt.timedelta,
}

# A sequence of these is natively bytes, as in C++ both are a vector of them
_BYTE = s.Int(8, signed=False)


class _Missing(Exception):
    """A class has no native class: in mixed mode, it is then a plain dataclass"""


class _Binding:
    def __init__(self, cls: type, native: type):
        self.cls = cls
        self.native = native
        self.fields: dict[str, tuple[_Converter, _Converter]] = {}
        """By name, the converters to and from the native value"""


_lock = RLock()
# None for a class found to have no native class
_bindings: dict[type, _Binding | None] = {}
_enums: dict[type[Enum], type] = {}
# Being bound: installed together, as they may refer to each other
_pending: list[_Binding] = []
# The Python objects of the handles which have been shared, so that a Shared field gives the object it was given
_shared: WeakValueDictionary[int, Any] = WeakValueDictionary()

_REMEDY = (
    f"Build its extension, or set {ENV_VAR} to {Mode.MIXED.value} or {Mode.PURE.value}"
)


def bind(cls: type, required: bool) -> type | None:
    """
    The native class of a @native class, binding the class to it if it has not been.

    :param required: Raise if it has no native class, rather than return None
    """
    with _lock:
        try:
            return _installing(lambda: _binding(cls)).native
        except _Missing as e:
            if required:
                raise NativeBindingError(
                    f"No native binding is available for {cls.__module__}.{cls.__qualname__}: {e}. {_REMEDY}"
                ) from None
            _bindings[cls] = None
            return None


def bind_function(
    fn: Callable[..., Any], module: str, required: bool
) -> Callable[..., Any]:
    """
    What to call for a function marked with @native_function: one which calls the function of its name in the
    extension module, converting its arguments and what that returns.

    :param required: Raise if it has no native function, or a class it uses has no native class, rather than return fn
    """
    name = f"{fn.__module__}.{fn.__qualname__}"
    signature = inspect.signature(fn)
    parameters = list(signature.parameters)
    hints = get_type_hints(fn, include_extras=True)
    if unannotated := [p for p in (*parameters, "return") if p not in hints]:
        raise TypeError(f"{name}: {', '.join(unannotated)} must be annotated")

    def make() -> tuple[Callable[..., Any], list[_Converter], _Converter]:
        extension = _import(resolve_name(module, fn.__module__.rpartition(".")[0]))
        try:
            native = getattr(extension, fn.__name__)
        except AttributeError:
            raise _Missing(f"{extension.__name__} has no {fn.__name__}") from None
        returns = hints["return"]
        return (
            native,
            [_to_native(resolve(hints[p], f"{name}({p})")) for p in parameters],
            None if returns is NoneType else _from_native(resolve(returns, name)),
        )

    try:
        native, to_native, from_native = _installing(make)
    except _Missing as e:
        if required:
            raise NativeBindingError(
                f"No native function is available for {name}: {e}. {_REMEDY}"
            ) from None
        return fn

    def call(*args: Any, **kwargs: Any) -> Any:
        if kwargs or len(args) != len(parameters):
            arguments = signature.bind(*args, **kwargs)
            arguments.apply_defaults()
            args = arguments.args

        converted = []
        for parameter, convert, value in zip(parameters, to_native, args):
            try:
                converted.append(convert(value) if convert else value)
            except (TypeError, ValueError):
                raise TypeError(f"{name}: {parameter} cannot be {value!r}") from None

        result = native(*converted)
        return from_native(result) if from_native else result

    return call


def _installing[T](make: Callable[[], T]) -> T:
    """What make returns, having installed the classes which it bound: or, if it raises, having unbound them"""
    with _lock:
        try:
            made = make()
            for pending in _pending:
                _install(pending)
            return made
        except BaseException:
            for pending in _pending:
                del _bindings[pending.cls]
            raise
        finally:
            _pending.clear()


def wrap(cls: type, handle: Any) -> Any:
    """An instance of a bound class, holding the given native object"""
    obj = object.__new__(cls)
    obj.__dict__[HANDLE] = handle
    return obj


def _binding(cls: type) -> _Binding:
    try:
        binding = _bindings[cls]
    except KeyError:
        pass
    else:
        if binding is None:
            raise _Missing(f"{cls.__qualname__} has none")
        return binding

    if cls.__qualname__ != cls.__name__:
        raise _Missing("only the classes at the top level of a module have one")

    struct = describe(cls)
    assert isinstance(struct, s.Struct)
    binding = _bindings[cls] = _Binding(cls, _native_type(struct))
    _pending.append(binding)
    for f in struct.fields:
        binding.fields[f.name] = _to_native(f.type), _from_native(f.type)
    return binding


def _extension(module: str) -> ModuleType:
    return _import(extension_name(module))


def _import(name: str) -> ModuleType:
    """An extension module, which may be missing"""
    try:
        return import_module(name)
    except ModuleNotFoundError as e:
        # Not, e.g., a module which the extension itself imports
        if e.name != name and not name.startswith(f"{e.name}."):
            raise
        raise _Missing(f"there is no extension module {name}") from None


def _native_type(item: s.Struct | s.Enum) -> type:
    """The native class of a struct or enum, checked to have been generated from this version of it"""
    extension = _extension(item.module)
    generated = extension.__schema__.get(item.name)
    if generated is None:
        raise _Missing(f"{extension.__name__} was built without {item.name}")
    if generated != s.schema_hash(item):
        raise NativeBindingError(
            f"{item.module}.{item.name} has changed since {extension.__name__} was built: build it again"
        )
    return getattr(extension, item.name)


def _native_enum(ref: s.EnumRef) -> type:
    try:
        return _enums[ref.cls]
    except KeyError:
        enum = describe(ref.cls)
        assert isinstance(enum, s.Enum)
        native = _enums[ref.cls] = _native_type(enum)
        return native


def _struct_class(t: s.Type) -> type | None:
    """The @native class of a struct, however it is held"""
    match t:
        case (
            s.StructRef(cls=cls)
            | s.Shared(s.StructRef(cls=cls))
            | s.Indirect(s.StructRef(cls=cls))
        ):
            return cls
        case _:
            return None


def _error(value: Any, expected: str) -> TypeError:
    return TypeError(f"{value!r} is not {expected}")


def _to_native(t: s.Type) -> _Converter:
    """Converts a Python value of a type to that its native member is set to, checking what nanobind would not"""
    if cls := _struct_class(t):
        _binding(cls)

        def handle(value: Any) -> Any:
            # Exactly, as a subclass has a native class of its own
            if type(value) is not cls:
                raise _error(value, f"a {cls.__qualname__}")
            try:
                return value.__dict__[HANDLE]
            except KeyError:
                return _adopt(value)

        def share(value: Any) -> Any:
            shared = handle(value)
            _shared[id(shared)] = value
            return shared

        return share if isinstance(t, s.Shared) else handle

    match t:
        case s.EnumRef(cls=enum):
            native = _native_enum(t)
            members = {member: getattr(native, member.name) for member in enum}

            def member(value: Any) -> Any:
                try:
                    return members[value]
                except (KeyError, TypeError):
                    raise _error(value, f"a {enum.__qualname__}") from None

            return member

        case s.Optional(inner):
            convert = _to_native(inner)
            return convert and (lambda value: None if value is None else convert(value))

        case s.Sequence(element):
            convert = bytes if element == _BYTE else _each(_to_native(element), list)

            def sequence(value: Any) -> Any:
                if type(value) is not tuple:
                    raise _error(value, "a tuple")
                return convert(value) if convert else value

            return sequence

        case s.Tuple(elements):
            convert = _zipped([_to_native(e) for e in elements])

            def fixed(value: Any) -> Any:
                if type(value) is not tuple or len(value) != len(elements):
                    raise _error(value, f"a tuple of {len(elements)}")
                return convert(value) if convert else value

            return fixed

        case s.Set(element):
            convert = _each(_to_native(element), set)

            def set_(value: Any) -> Any:
                if type(value) is not frozenset:
                    raise _error(value, "a frozenset")
                return convert(value) if convert else value

            return set_

        case s.Map(key, value_type):
            convert = _items(_to_native(key), _to_native(value_type)) or dict

            def mapping(value: Any) -> Any:
                if not isinstance(value, Mapping):
                    raise _error(value, "a Mapping")
                return convert(value)

            return mapping

        case s.Union(members):
            converters: dict[type, _Converter] = {}
            for m in members:
                converters.setdefault(_python_type(m), _to_native(m))
            if any(isinstance(m, s.Float) for m in members):
                converters.setdefault(int, None)
            expected = " or ".join(typ.__name__ for typ in converters)

            def union(value: Any) -> Any:
                typ = type(value)
                if typ not in converters and isinstance(value, Mapping):
                    typ = Mapping
                try:
                    convert = converters[typ]
                except KeyError:
                    raise _error(value, expected) from None
                return convert(value) if convert else value

            return union

        case _:
            return None


def _from_native(t: s.Type) -> _Converter:
    """Converts the value of a native member to the Python value of its type"""
    if cls := _struct_class(t):
        if not isinstance(t, s.Shared):
            return lambda handle: wrap(cls, handle)

        def shared(handle: Any) -> Any:
            try:
                return _shared[id(handle)]
            except KeyError:
                obj = _shared[id(handle)] = wrap(cls, handle)
                return obj

        return shared

    match t:
        case s.EnumRef(cls=enum):
            native = _native_enum(t)
            return {getattr(native, member.name): member for member in enum}.__getitem__
        case s.Optional(inner):
            convert = _from_native(inner)
            return convert and (lambda value: None if value is None else convert(value))
        case s.Sequence(element):
            return (
                tuple
                if element == _BYTE
                else _each(_from_native(element), tuple) or tuple
            )
        case s.Tuple(elements):
            return _zipped([_from_native(e) for e in elements])
        case s.Set(element):
            return _each(_from_native(element), frozenset) or frozenset
        case s.Map(key, value_type):
            return _items(_from_native(key), _from_native(value_type))
        case s.Union(members):
            converters = {_native_value_type(m): _from_native(m) for m in members}
            if not any(converters.values()):
                return None

            def union(value: Any) -> Any:
                convert = converters[type(value)]
                return convert(value) if convert else value

            return union
        case _:
            return None


def _each(convert: _Converter, container: Callable[[Any], Any]) -> _Converter:
    return convert and (lambda values: container(map(convert, values)))


def _zipped(converters: list[_Converter]) -> _Converter:
    if not any(converters):
        return None
    return lambda values: tuple(
        c(v) if c else v for c, v in zip(converters, values, strict=True)
    )


def _items(key: _Converter, value: _Converter) -> _Converter:
    if not key and not value:
        return None
    return lambda items: {
        (key(k) if key else k): (value(v) if value else v) for k, v in items.items()
    }


def _python_type(t: s.Type) -> type:
    """The type of the Python values of a union's member, which tells them from those of its other members"""
    match t:
        case s.EnumRef(cls=cls):
            return cls
        case s.Sequence() | s.Tuple():
            return tuple
        case s.Set():
            return frozenset
        case s.Map():
            return Mapping
        case _:
            return _struct_class(t) or _SCALARS[type(t)]


def _native_value_type(t: s.Type) -> type:
    """As _python_type, of the member's native values"""
    match t:
        case s.EnumRef():
            return _native_enum(t)
        case s.Sequence(element):
            return bytes if element == _BYTE else list
        case s.Tuple():
            return tuple
        case s.Set():
            return set
        case s.Map():
            return dict
        case _:
            cls = _struct_class(t)
            return _binding(cls).native if cls else _SCALARS[type(t)]


class _Field:
    """A field of a bound class: gets and sets the member of the instance's native object"""

    __slots__ = ("default", "from_native", "name", "to_native")

    def __init__(
        self, name: str, default: Any, to_native: _Converter, from_native: _Converter
    ):
        self.name = name
        self.default = default
        self.to_native = to_native
        self.from_native = from_native

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        if obj is None:
            # As the dataclass's own attribute: its default, if any
            if self.default is MISSING:
                raise AttributeError(self.name)
            return self.default

        attributes = obj.__dict__
        try:
            handle = attributes[HANDLE]
        except KeyError:
            if _bindings.get(type(obj)) is not None:
                handle = _adopt(obj)
            else:
                # Of a subclass which is not bound, so a plain dataclass
                try:
                    return attributes[self.name]
                except KeyError:
                    raise AttributeError(self.name) from None

        value = getattr(handle, self.name)
        return self.from_native(value) if self.from_native else value

    def __set__(self, obj: Any, value: Any):
        attributes = obj.__dict__
        if HANDLE in attributes:
            _set(attributes[HANDLE], obj, self.name, value, self.to_native)
        else:
            attributes[self.name] = value


def _set(handle: Any, obj: Any, name: str, value: Any, to_native: _Converter):
    try:
        setattr(handle, name, to_native(value) if to_native else value)
    except (
        TypeError,
        ValueError,
    ):  # A ValueError from bytes(), of ints which are not bytes
        raise TypeError(
            f"{type(obj).__qualname__}.{name} cannot be {value!r}"
        ) from None


def _adopt(obj: Any) -> Any:
    """
    Gives an instance of a bound class a native object, holding the fields which are in its __dict__, and returns it.
    Such is an instance made by pydantic, which sets an instance's __dict__ once it has validated its fields
    """
    binding = _bindings[type(obj)]
    assert binding is not None
    attributes = obj.__dict__
    handle = binding.native()
    for name, (to_native, _) in binding.fields.items():
        if name in attributes:
            _set(handle, obj, name, attributes[name], to_native)
    for name in binding.fields:
        attributes.pop(name, None)
    attributes[HANDLE] = handle
    return handle


def _reduce(self: Any) -> tuple[Any, ...]:
    """As a plain dataclass pickles, so that a pickle made in one mode loads in any"""
    return (
        __newobj__,
        (type(self),),
        {f.name: getattr(self, f.name) for f in fields(self)},
    )


def _setstate(self: Any, state: dict[str, Any]):
    for name, value in state.items():
        object.__setattr__(self, name, value)


def _install(binding: _Binding):
    cls = binding.cls
    for f in fields(cls):
        # The attribute the dataclass has is its default, if any
        default = getattr(cls, f.name, MISSING)
        setattr(cls, f.name, _Field(f.name, default, *binding.fields[f.name]))
    # Else copying and pickling would copy the handle
    cls.__reduce__ = _reduce  # pyright: ignore[reportAttributeAccessIssue]
    cls.__setstate__ = _setstate  # pyright: ignore[reportAttributeAccessIssue]

    if hasattr(cls, "__pydantic_validator__"):
        init = cls.__init__

        @wraps(init)
        def __init__(self: Any, *args: Any, **kwargs: Any):
            init(self, *args, **kwargs)
            # Now, so that what the native class rejects is rejected here, not when a field is first read
            if HANDLE not in self.__dict__:
                _adopt(self)

        cls.__init__ = __init__
