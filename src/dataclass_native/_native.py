"""The @native decorator, marking a dataclass as part of the schema and binding it to its native equivalent"""

from collections.abc import Callable
from dataclasses import dataclass, is_dataclass
from typing import Any, overload
from weakref import WeakKeyDictionary

from ._mode import ENV_VAR, Mode, _fix_mode


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
        if (mode := _fix_mode()) is not Mode.PURE:
            _install_binding(cls, required=mode is Mode.STRICT)
        return cls

    return decorate if cls is None else decorate(cls)


def is_native(cls: type) -> bool:
    """Whether the class itself, not just a base, is decorated with @native"""
    return cls in _registry


def native_info(cls: type) -> NativeInfo:
    try:
        return _registry[cls]
    except KeyError:
        raise TypeError(f"{cls.__qualname__} is not decorated with @native") from None


def _validate(cls: Any):
    if not isinstance(cls, type) or not is_dataclass(cls):
        raise TypeError(
            f"@native must be applied to a dataclass, above @dataclass, not to {cls!r}"
        )

    if not cls.__dataclass_params__.frozen:  # pyright: ignore[reportAttributeAccessIssue]
        raise TypeError(
            f"{cls.__qualname__}: only frozen dataclasses are supported: use @dataclass(frozen=True)"
        )

    if getattr(cls, "__type_params__", ()) or getattr(cls, "__parameters__", ()):
        raise TypeError(f"{cls.__qualname__}: generic dataclasses are not supported")


def _install_binding(cls: type, required: bool):
    def __new__(klass: type, *_args: Any, **_kwargs: Any) -> Any:
        if not is_native(klass):
            raise TypeError(
                f"{klass.__qualname__} derives from @native {cls.__qualname__}, so must be decorated with @native"
            )

        # Binding to the generated extension is not implemented yet, so it is always missing
        if required:
            raise NativeBindingError(
                f"No native binding is available for {klass.__module__}.{klass.__qualname__}: build its extension, "
                f"or set {ENV_VAR} to {Mode.MIXED.value} or {Mode.PURE.value}"
            )
        return object.__new__(klass)

    cls.__new__ = staticmethod(__new__)  # pyright: ignore[reportAttributeAccessIssue]
