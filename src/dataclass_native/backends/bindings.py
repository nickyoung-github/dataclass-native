"""
Generates the source of a Python extension module per schema module, binding the C++ of dataclass_native.backends.cpp
with nanobind: the native classes which @native classes store their fields in.

- pkg/sub/mod.py -> pkg/sub/_mod_native.cpp, the extension module pkg.sub._mod_native
- extensions.cmake lists the extension modules, for dataclass_native_add_extensions
"""

import sys
from pathlib import Path
from typing import Any

from .. import schema as s
from .._native import extension_name
from .cpp import header_path, identifier, in_arrow, namespace, render

MANIFEST = "extensions.cmake"


def source_path(module: str) -> str:
    return extension_name(module).replace(".", "/") + ".cpp"


def generate(schema: s.Schema) -> dict[str, str]:
    """The source of each module's extension, and the manifest, by path relative to the output directory"""
    files = {
        source_path(name): generate_module(schema, name) for name in schema.modules
    }
    files[MANIFEST] = render(
        MANIFEST + ".j2",
        extensions=[extension_name(name) for name in schema.modules],
        schemas=[
            Path(file).as_posix()
            for name in schema.modules
            if (file := getattr(sys.modules[name], "__file__", None))
        ],
    )
    return files


def generate_module(schema: s.Schema, module_name: str) -> str:
    module = schema.modules[module_name]

    def described(item: s.Struct | s.Enum) -> dict[str, Any]:
        return {
            "name": item.name,
            "type": f"::{namespace(item.module)}::{identifier(item.name)}",
            "hash": s.schema_hash(item),
        }

    def named(name: str) -> dict[str, str]:
        return {"name": name, "identifier": identifier(name)}

    return render(
        "bindings.cpp.j2",
        module=module.name,
        extension=extension_name(module.name).rpartition(".")[2],
        header=header_path(module.name),
        dependencies=[extension_name(d) for d in module.dependencies],
        enums=[
            described(e) | {"members": [named(m.name) for m in e.members]}
            for e in module.enums
        ],
        structs=[
            described(st)
            | {
                "fields": [named(f.name) for f in st.fields],
                "arrow": in_arrow(schema, st),
            }
            for st in module.structs
        ],
    )
