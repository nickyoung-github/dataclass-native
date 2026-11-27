"""
Generates a C++ header per schema module: plain C++20 structs and enums, with no dependency on Python.

- pkg/sub/mod.py -> pkg/sub/mod.hpp, namespace pkg::sub::mod
- Each struct is an aggregate, with default member initialisers and a defaulted operator==
- Names which are C++ keywords have "_" appended
"""

import datetime as dt
import math
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .. import schema as s
from ..schema import SchemaError

RUNTIME = "dataclass_native"
RUNTIME_HEADER = "dataclass_native/types.hpp"

# Including alternative tokens, and identifiers with special meaning
KEYWORDS = frozenset(
    [
        "alignas",
        "alignof",
        "and",
        "and_eq",
        "asm",
        "auto",
        "bitand",
        "bitor",
        "bool",
        "break",
        "case",
        "catch",
        "char",
        "char8_t",
        "char16_t",
        "char32_t",
        "class",
        "compl",
        "concept",
        "const",
        "consteval",
        "constexpr",
        "constinit",
        "const_cast",
        "continue",
        "co_await",
        "co_return",
        "co_yield",
        "decltype",
        "default",
        "delete",
        "do",
        "double",
        "dynamic_cast",
        "else",
        "enum",
        "explicit",
        "export",
        "extern",
        "false",
        "float",
        "for",
        "friend",
        "goto",
        "if",
        "inline",
        "int",
        "long",
        "mutable",
        "namespace",
        "new",
        "noexcept",
        "not",
        "not_eq",
        "nullptr",
        "operator",
        "or",
        "or_eq",
        "private",
        "protected",
        "public",
        "register",
        "reinterpret_cast",
        "requires",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "static_assert",
        "static_cast",
        "struct",
        "switch",
        "template",
        "this",
        "thread_local",
        "throw",
        "true",
        "try",
        "typedef",
        "typeid",
        "typename",
        "union",
        "unsigned",
        "using",
        "virtual",
        "void",
        "volatile",
        "wchar_t",
        "while",
        "xor",
        "xor_eq",
        "final",
        "override",
        "import",
        "module",
    ]
)

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_EPOCH_DATE = dt.date(1970, 1, 1)


def identifier(name: str) -> str:
    if "__" in name or (name.startswith("_") and name[1:2].isupper()):
        raise SchemaError(
            f"{name!r} is reserved in C++: names may not contain '__', or start with '_' and a capital"
        )
    return f"{name}_" if name in KEYWORDS else name


def namespace(module: str) -> str:
    return "::".join(identifier(part) for part in module.split("."))


def header_path(module: str) -> str:
    return module.replace(".", "/") + ".hpp"


@cache
def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )


def render(template: str, **context: Any) -> str:
    return _environment().get_template(template).render(context)


def generate(schema: s.Schema) -> dict[str, str]:
    """The header for each module of the schema, by its path relative to the include directory"""
    return {header_path(name): generate_module(schema, name) for name in schema.modules}


def generate_module(schema: s.Schema, module_name: str) -> str:
    return _ModuleGenerator(schema, schema.modules[module_name]).generate()


class _ModuleGenerator:
    def __init__(self, schema: s.Schema, module: s.Module):
        self.__schema = schema
        self.__module = module
        self.__std_includes: set[str] = set()
        self.__runtime = False

    def generate(self) -> str:
        module = self.__module
        # Before the includes, which rendering them finds
        enums = [self.__enum(e) for e in module.enums]
        structs = [self.__struct(st) for st in module.structs]

        includes = [f"<{h}>" for h in sorted(self.__std_includes)]
        if self.__runtime:
            includes.append(f'"{RUNTIME_HEADER}"')
        includes.extend(f'"{header_path(d)}"' for d in module.dependencies)

        return render(
            "header.hpp.j2",
            module=module.name,
            namespace=namespace(module.name),
            includes=includes,
            enums=enums,
            forward_declarations=[identifier(st.name) for st in module.structs]
            if self.__needs_forward_declarations()
            else [],
            structs=structs,
        )

    def __needs_forward_declarations(self) -> bool:
        defined: set[str] = set()
        for st in self.__module.structs:
            for f in st.fields:
                for t in _walk(f.type):
                    if (
                        isinstance(t, s.StructRef)
                        and t.module == st.module
                        and t.name not in defined
                    ):
                        return True
            defined.add(st.name)
        return False

    def __enum(self, enum: s.Enum) -> dict[str, Any]:
        values = [m.value for m in enum.members]
        underlying = (
            "std::int32_t"
            if all(-(2**31) <= v < 2**31 for v in values)
            else "std::int64_t"
        )
        self.__std_includes.add("cstdint")
        return {
            "name": identifier(enum.name),
            "underlying": underlying,
            "members": [
                {"name": identifier(m.name), "value": _int(m.value, 64, True)}
                for m in enum.members
            ],
        }

    def __struct(self, struct: s.Struct) -> dict[str, Any]:
        members = []
        for f in struct.fields:
            where = f"{struct.cls.__qualname__}.{f.name}"
            typ = self.type(f.type)
            member = identifier(f.name)
            if f.has_default:
                members.append(
                    f"{typ} {member} = {self.value(f.type, f.default, where)};"
                )
            else:
                members.append(f"{typ} {member}{{}};")

        return {"name": identifier(struct.name), "members": members}

    def __name(self, module: str, name: str) -> str:
        if module == self.__module.name:
            return identifier(name)
        return f"::{namespace(module)}::{identifier(name)}"

    def type(self, t: s.Type) -> str:
        match t:
            case s.Bool():
                return "bool"
            case s.Int(bits, signed):
                self.__std_includes.add("cstdint")
                return f"std::{'' if signed else 'u'}int{bits}_t"
            case s.Float(bits):
                return "float" if bits == 32 else "double"
            case s.Str():
                self.__std_includes.add("string")
                return "std::string"
            case s.Bytes() | s.Date() | s.Time() | s.DateTime() | s.TimeDelta():
                self.__runtime = True
                name = "TimeOfDay" if isinstance(t, s.Time) else type(t).__name__
                return f"{RUNTIME}::{name}"
            case s.EnumRef(module, name) | s.StructRef(module, name):
                return self.__name(module, name)
            case s.Optional(inner):
                self.__std_includes.add("optional")
                return f"std::optional<{self.type(inner)}>"
            case s.Sequence(element):
                self.__std_includes.add("vector")
                return f"std::vector<{self.type(element)}>"
            case s.Tuple(elements):
                self.__std_includes.add("tuple")
                return f"std::tuple<{', '.join(self.type(e) for e in elements)}>"
            case s.Set(element):
                self.__std_includes.add("set")
                return f"std::set<{self.type(element)}>"
            case s.Map(key, value):
                self.__std_includes.add("map")
                return f"std::map<{self.type(key)}, {self.type(value)}>"
            case s.Union(members):
                self.__std_includes.add("variant")
                return f"std::variant<{', '.join(self.type(m) for m in members)}>"
            case s.Shared(target):
                self.__runtime = True
                return f"{RUNTIME}::Shared<{self.type(target)}>"
            case s.Indirect(target):
                self.__runtime = True
                return f"{RUNTIME}::Indirect<{self.type(target)}>"

    def value(self, t: s.Type, v: Any, where: str) -> str:
        """A C++ expression of type t, for the Python value v"""
        match t:
            case s.Bool():
                return "true" if v else "false"
            case s.Int(bits, signed):
                return _int(v, bits, signed)
            case s.Float(bits):
                return self.__float(float(v), bits)
            case s.Str():
                return _string(v)
            case s.Bytes():
                return f"{self.type(t)}{{{', '.join(f'0x{b:02x}' for b in v)}}}"
            case s.Date():
                return (
                    f"{self.type(t)}{{std::chrono::days{{{(v - _EPOCH_DATE).days}}}}}"
                )
            case s.Time():
                micros = (
                    (v.hour * 60 + v.minute) * 60 + v.second
                ) * 1_000_000 + v.microsecond
                return f"{self.type(t)}{{std::chrono::microseconds{{{micros}}}}}"
            case s.DateTime():
                micros = (v - _EPOCH) // dt.timedelta(microseconds=1)
                return f"{self.type(t)}{{std::chrono::microseconds{{{micros}}}}}"
            case s.TimeDelta():
                return f"{self.type(t)}{{{v // dt.timedelta(microseconds=1)}}}"
            case s.EnumRef():
                return f"{self.type(t)}::{identifier(v.name)}"
            case s.StructRef():
                struct = self.__schema.struct(t)
                inits = ", ".join(
                    f".{identifier(f.name)} = {self.value(f.type, getattr(v, f.name), where)}"
                    for f in struct.fields
                )
                return f"{self.type(t)}{{{inits}}}"
            case s.Optional(inner):
                if v is None:
                    self.__std_includes.add("optional")
                    return "std::nullopt"
                return f"{self.type(t)}{{{self.value(inner, v, where)}}}"
            case s.Sequence(element):
                return f"{self.type(t)}{{{', '.join(self.value(element, e, where) for e in v)}}}"
            case s.Tuple(elements):
                values = ", ".join(
                    self.value(e, ev, where) for e, ev in zip(elements, v, strict=True)
                )
                return f"{self.type(t)}{{{values}}}"
            case s.Set(element):
                return f"{self.type(t)}{{{', '.join(sorted(self.value(element, e, where) for e in v))}}}"
            case s.Map(key, value):
                items = sorted(
                    f"{{{self.value(key, k, where)}, {self.value(value, kv, where)}}}"
                    for k, kv in v.items()
                )
                return f"{self.type(t)}{{{', '.join(items)}}}"
            case s.Union():
                index = s.union_member(t, v, self.__schema)
                self.__std_includes.add("utility")
                return f"{self.type(t)}{{std::in_place_index<{index}>, {self.value(t.members[index], v, where)}}}"
            case s.Shared(target):
                return f"{self.type(t)}{{{self.value(target, v, where)}}}"
            case s.Indirect():
                raise SchemaError(
                    f"{where}: in C++, a default may not hold a value of a recursive type"
                )

    def __float(self, v: float, bits: int) -> str:
        typ = "float" if bits == 32 else "double"
        if math.isnan(v) or math.isinf(v):
            self.__std_includes.add("limits")
            limit = "quiet_NaN" if math.isnan(v) else "infinity"
            return f"{'-' if v < 0 else ''}std::numeric_limits<{typ}>::{limit}()"
        literal = repr(v)
        return f"{literal}f" if bits == 32 else literal


def _walk(t: s.Type) -> Iterator[s.Type]:
    yield t
    match t:
        case s.Optional(inner):
            yield from _walk(inner)
        case s.Sequence(element) | s.Set(element):
            yield from _walk(element)
        case s.Tuple(members) | s.Union(members):
            for member in members:
                yield from _walk(member)
        case s.Map(key, value):
            yield from _walk(key)
            yield from _walk(value)
        case s.Shared(target) | s.Indirect(target):
            yield target
        case _:
            pass


def _int(v: int, bits: int, signed: bool) -> str:
    if not signed:
        return f"{v}ULL"
    if v == -(2**63):
        # As 9223372036854775808 is not a valid literal
        return "(-9223372036854775807LL - 1)"
    return f"{v}LL" if bits == 64 and not -(2**31) <= v < 2**31 else str(v)


def _string(v: str) -> str:
    """A string literal, escaping all but printable ASCII as octal, which unlike hex escapes cannot run on"""
    data = v.encode()
    chars = []
    for b in data:
        c = chr(b)
        if c in '"\\':
            chars.append("\\" + c)
        elif 0x20 <= b < 0x7F:
            chars.append(c)
        else:
            chars.append(f"\\{b:03o}")
    literal = f'"{"".join(chars)}"'
    # With an embedded NUL, the length must be given
    return (
        f"std::string({literal}, {len(data)})"
        if b"\0" in data
        else f"std::string{{{literal}}}"
    )
