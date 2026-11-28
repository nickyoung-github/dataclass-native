"""The @native decorator, marking a dataclass as part of the schema and binding it to its native equivalent"""

import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from functools import wraps
from types import FunctionType
from typing import Any, overload
from weakref import WeakKeyDictionary

from ._mode import Mode, _fix_mode


class NativeBindingError(RuntimeError):
    """A @native class has no native binding available"""


@dataclass(frozen=True)
class NativeInfo:
    tag: str
    """Names the type when serialised, e.g. in unions: stable across languages, so not tied to Python's layout"""


_registry: WeakKeyDictionary[type, NativeInfo] = WeakKeyDictionary()


@overload
def native[C: type](cls: C, /) -> C: ...


@overload
def native[C: type](*, tag: str | None = None) -> Callable[[C], C]: ...


def native(cls: Any = None, /, *, tag: str | None = None) -> Any:
    """
    Marks a frozen dataclass as part of the schema, for which native code is generated.

    Apply above @dataclass. The class's fields are stored in its native equivalent. If that is not available, strict
    mode (the default) raises when the class is instantiated, and mixed mode leaves it a plain dataclass. In pure mode
    (DATACLASS_NATIVE_MODE=pure), the class is returned unchanged.

    :param tag: Names the type when serialised. Defaults to the module and qualified name: set it to keep the wire
        format when moving or renaming the class
    """

    def decorate(cls: type) -> type:
        _validate(cls)
        _registry[cls] = NativeInfo(tag=tag or f"{cls.__module__}.{cls.__qualname__}")
        _compare_arrays(cls)
        if (mode := _fix_mode()) is not Mode.PURE:
            _install_binding(cls, required=mode is Mode.STRICT)
        return cls

    return decorate if cls is None else decorate(cls)


def native_function[F: Callable[..., Any]](module: str) -> Callable[[F], F]:
    """
    Marks a function as implemented natively, by the function of its name in an extension module.

    Its @native arguments are given to the native function as their native objects, and what that returns becomes the
    object of the return annotation, neither being copied. The function's body is its Python implementation: called
    in pure mode, and in mixed mode if the native function, or the binding of a class it uses, is not available.
    Strict mode (the default) then raises, when the function is first called.

    :param module: The extension module: relative to the function's package if it starts with a dot, e.g. "._pricing"
    """

    def decorate(fn: Any) -> Any:
        if not isinstance(fn, FunctionType):
            raise TypeError(
                f"@native_function must be applied to a function, not to {fn!r}"
            )
        code = fn.__code__
        if code.co_kwonlyargcount or code.co_flags & (
            inspect.CO_VARARGS | inspect.CO_VARKEYWORDS
        ):
            raise TypeError(
                f"{fn.__qualname__}: keyword-only parameters, *args and **kwargs are not supported"
            )

        if (mode := _fix_mode()) is Mode.PURE:
            return fn

        # Once first called: what to call
        bound: list[Callable[..., Any]] = []

        @wraps(fn)
        def call(*args: Any, **kwargs: Any) -> Any:
            if not bound:
                # Lazily, as classes are bound: the function's module imports without the extension
                from ._runtime import bind_function

                bound.append(bind_function(fn, module, required=mode is Mode.STRICT))
            return bound[0](*args, **kwargs)

        return call

    return decorate


def extension_name(module: str) -> str:
    """The extension module holding the native classes of a schema module: pkg._mod_native for pkg.mod"""
    package, _, name = module.rpartition(".")
    return f"{package}{'.' if package else ''}_{name}_native"


def is_native(cls: type) -> bool:
    """Whether the class itself, not just a base, is decorated with @native"""
    return cls in _registry


def native_info(cls: type) -> NativeInfo:
    try:
        return _registry[cls]
    except KeyError:
        raise TypeError(f"{cls.__qualname__} is not decorated with @native") from None


def _validate(cls: Any):
    if hasattr(cls, "model_fields"):
        raise TypeError(
            f"{cls.__qualname__}: pydantic models are not supported: use pydantic.dataclasses.dataclass"
        )

    if not isinstance(cls, type) or not is_dataclass(cls):
        raise TypeError(
            f"@native must be applied to a dataclass, above @dataclass, not to {cls!r}"
        )

    if not cls.__dataclass_params__.frozen:  # pyright: ignore[reportAttributeAccessIssue]
        raise TypeError(
            f"{cls.__qualname__}: only frozen dataclasses are supported: use @dataclass(frozen=True)"
        )

    if "__slots__" in vars(cls):
        raise TypeError(
            f"{cls.__qualname__}: dataclasses with slots are not supported, as fields are stored natively"
        )

    if getattr(cls, "__type_params__", ()) or getattr(cls, "__parameters__", ()):
        raise TypeError(f"{cls.__qualname__}: generic dataclasses are not supported")


def _compare_arrays(cls: Any):
    """
    Gives a class with fields which are arrays an __eq__ and __hash__ which compare them by what they hold, as the
    dataclass's own compare each field with ==, which of arrays gives an array. Arrays are equal which hold the same
    bits: so one of NaN equals itself, and 0.0 is not -0.0
    """
    np = sys.modules.get("numpy")
    if np is None or not cls.__dataclass_params__.eq:
        return

    # Here, as introspect imports this module
    from . import schema as s
    from .introspect import resolve

    arrays = set()
    for f in fields(cls):
        try:
            # Each alone, as others may name classes which are not yet defined
            tp = (
                eval(f.type, dict(vars(sys.modules[cls.__module__])))
                if isinstance(f.type, str)
                else f.type
            )
            t = resolve(tp, f.name)
        except (NameError, AttributeError, KeyError, SyntaxError, TypeError):
            continue
        if isinstance(t.inner if isinstance(t, s.Optional) else t, s.Array):
            arrays.add(f.name)
    if not arrays:
        return

    names = [f.name for f in fields(cls) if f.compare]

    def bits(value: Any) -> Any:
        if isinstance(value, np.ndarray):
            return value.dtype.str, value.shape, value.tobytes()
        return value

    def key(self: Any) -> tuple[Any, ...]:
        values = ((name, getattr(self, name)) for name in names)
        return tuple(bits(value) if name in arrays else value for name, value in values)

    def __eq__(self: Any, other: Any) -> Any:
        return (
            key(self) == key(other)
            if other.__class__ is self.__class__
            else NotImplemented
        )

    def __hash__(self: Any) -> int:
        return hash(key(self))

    cls.__eq__ = __eq__
    cls.__hash__ = __hash__


def _install_binding(cls: type, required: bool):
    # Once bound: the native class, or None if it has none (mixed mode)
    native: list[type | None] = []

    def bound() -> type | None:
        if not native:
            # Lazily, so that schema modules import without their extension, and before all their classes exist
            from ._runtime import bind

            native.append(bind(cls, required))
        return native[0]

    def __new__(klass: type, *_args: Any, **_kwargs: Any) -> Any:
        if not is_native(klass):
            raise TypeError(
                f"{klass.__qualname__} derives from @native {cls.__qualname__}, so must be decorated with @native"
            )

        self = object.__new__(klass)
        if (native_class := bound()) is not None:
            self.__dict__["__native__"] = native_class()
        return self

    def _from_native(klass: type, handle: Any) -> Any:
        """An instance holding the given native object, e.g. one returned by C++, without copying it"""
        if klass is not cls or type(handle) is not bound():
            raise TypeError(
                f"{handle!r} is not the native object of a {klass.__qualname__}"
            )
        self = object.__new__(klass)
        self.__dict__["__native__"] = handle
        return self

    cls.__new__ = staticmethod(__new__)  # pyright: ignore[reportAttributeAccessIssue]
    cls._from_native = classmethod(_from_native)  # pyright: ignore[reportAttributeAccessIssue]
