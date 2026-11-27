"""Whether @native classes bind to their native equivalents, or behave as plain dataclasses"""

import os
from enum import StrEnum

ENV_VAR = "DATACLASS_NATIVE_MODE"


class Mode(StrEnum):
    STRICT = "strict"
    """Fields are stored in the native object, which must be available: a missing binding raises"""
    MIXED = "mixed"
    """As strict, but a class whose binding is missing behaves as a plain stdlib dataclass"""
    PURE = "pure"
    """@native returns its class unchanged: a plain stdlib dataclass"""


def _from_env() -> Mode:
    value = os.environ.get(ENV_VAR, "").strip().lower()
    if not value:
        return Mode.STRICT
    try:
        return Mode(value)
    except ValueError:
        expected = ", ".join(m.value for m in Mode)
        raise ValueError(
            f"{ENV_VAR}={value!r} is invalid: expected one of {expected}"
        ) from None


_mode = _from_env()
# Set once a class has been decorated, as it was decorated per the mode then in effect
_fixed = False


def mode() -> Mode:
    """The mode in effect, from DATACLASS_NATIVE_MODE (strict by default) unless set by set_mode()"""
    return _mode


def set_mode(new: Mode | str) -> None:
    """Set the mode, which must happen before any @native class is defined, i.e. before importing schema modules"""
    global _mode
    new = Mode(new)
    if _fixed and new is not _mode:
        raise RuntimeError(
            f"Cannot change the mode to {new.value!r}: @native classes have already been defined in mode "
            f"{_mode.value!r}. Set it before importing schema modules, or via {ENV_VAR}"
        )
    _mode = new


def _fix_mode() -> Mode:
    """The mode, which may no longer change, as a class is being decorated per it"""
    global _fixed
    _fixed = True
    return _mode
