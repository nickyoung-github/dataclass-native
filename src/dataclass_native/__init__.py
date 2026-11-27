"""Python dataclasses as a data description language, stored in generated native structs"""

from ._mode import ENV_VAR, Mode, mode, set_mode
from ._native import NativeBindingError, is_native, native, native_function
from ._wire import from_msgpack, to_msgpack
from .cli import cmake_dir, include_dir
from .types import (
    DiscriminatedUnion,
    Float32,
    Float64,
    Int8,
    Int16,
    Int32,
    Int64,
    Shared,
    UInt8,
    UInt16,
    UInt32,
    UInt64,
)

__all__ = (
    "ENV_VAR",
    "DiscriminatedUnion",
    "Float32",
    "Float64",
    "Int8",
    "Int16",
    "Int32",
    "Int64",
    "Mode",
    "NativeBindingError",
    "Shared",
    "UInt8",
    "UInt16",
    "UInt32",
    "UInt64",
    "cmake_dir",
    "from_msgpack",
    "include_dir",
    "is_native",
    "mode",
    "native",
    "native_function",
    "set_mode",
    "to_msgpack",
)
