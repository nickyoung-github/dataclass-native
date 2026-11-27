"""
Builds the schema model from the @native dataclasses of some modules, and of the modules they use.

A module's schema holds all its @native classes, and all the enums it defines. Run in pure mode, as default factories
of @native classes are called, which in strict mode needs the binding being generated.
"""

import datetime as dt
import sys
import typing
from collections import abc
from collections.abc import Iterable, Iterator
from dataclasses import MISSING, Field, fields, is_dataclass
from enum import Enum, Flag
from importlib import import_module
from types import ModuleType, NoneType, UnionType
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from . import schema as s
from ._native import _registry, is_native, native_info
from .schema import SchemaError
from .types import DiscriminatedMarker, FloatSpec, IntSpec, SharedMarker

_SCALARS: dict[type, s.Type] = {
    bool: s.Bool(),
    int: s.Int(),
    float: s.Float(),
    str: s.Str(),
    bytes: s.Bytes(),
    dt.date: s.Date(),
    dt.time: s.Time(),
    dt.datetime: s.DateTime(),
    dt.timedelta: s.TimeDelta(),
}

# Containers which are, or may be, mutable: unsupported, with the immutable type to use instead
_ALTERNATIVES: dict[Any, str] = {
    list: "tuple[T, ...]",
    abc.Sequence: "tuple[T, ...]",
    abc.MutableSequence: "tuple[T, ...]",
    set: "frozenset[T]",
    abc.Set: "frozenset[T]",
    abc.MutableSet: "frozenset[T]",
    dict: "Mapping[K, V]",
    abc.MutableMapping: "Mapping[K, V]",
}

# Containers which must be parameterised, with an example
_PARAMETERISED: dict[type, str] = {
    tuple: "tuple[int, ...]",
    frozenset: "frozenset[int]",
    abc.Mapping: "Mapping[str, int]",
}


def build_schema(modules: Iterable[str | ModuleType]) -> s.Schema:
    """The schema of the given modules, and of those whose types they use"""
    return _Builder().build(modules)


def _module_name(module: str | ModuleType) -> str:
    return module.__name__ if isinstance(module, ModuleType) else module


def _check_top_level(cls: type):
    if cls.__qualname__ != cls.__name__:
        raise SchemaError(
            f"{cls.__qualname__}: nested and local classes are not supported in the schema"
        )


class _RawStruct:
    def __init__(self, cls: type, fields_: list[tuple[str, s.Type, Field]]):
        self.cls = cls
        self.fields = fields_


class _Builder:
    def __init__(self):
        self.__structs: dict[type, _RawStruct] = {}
        self.__enums: dict[type, s.Enum] = {}
        self.__modules: list[str] = []
        self.__queue: list[str] = []

    def build(self, modules: Iterable[str | ModuleType]) -> s.Schema:
        for module in modules:
            self.__use_module(_module_name(module))

        while self.__queue:
            self.__add_module(self.__queue.pop(0))

        self.__check_tags()
        recursive = self.__recursive_cycles()
        structs = {
            cls: self.__struct(raw, recursive.get(cls, frozenset()))
            for cls, raw in self.__structs.items()
        }

        modules_ = {}
        for name in self.__modules:
            module_structs = [st for st in structs.values() if st.module == name]
            modules_[name] = s.Module(
                name=name,
                enums=tuple(e for e in self.__enums.values() if e.module == name),
                structs=_ordered(module_structs),
                dependencies=_dependencies(name, module_structs),
            )

        _check_module_cycles(modules_)
        schema = s.Schema(modules_)
        _check_defaults(schema)
        return schema

    def __use_module(self, name: str):
        if name not in self.__modules:
            self.__modules.append(name)
            self.__queue.append(name)

    def __add_module(self, name: str):
        module = import_module(name)
        for cls in [c for c in list(_registry) if c.__module__ == name]:
            # Ignoring classes since redefined, e.g. on reloading
            if (
                vars(module).get(cls.__name__) is cls
                or cls.__qualname__ != cls.__name__
            ):
                _check_top_level(cls)
                self.__add_struct(cls)

        for value in list(vars(module).values()):
            if (
                isinstance(value, type)
                and issubclass(value, Enum)
                and not issubclass(value, Flag)
                and value.__module__ == name
                and value.__qualname__ == value.__name__
            ):
                self.__add_enum(value)

    def __add_struct(self, cls: type):
        if cls in self.__structs:
            return

        try:
            hints = get_type_hints(cls, include_extras=True)
        except Exception as e:
            raise SchemaError(
                f"{cls.__qualname__}: cannot resolve its type annotations: {e}"
            ) from e

        resolved = []
        for f in fields(cls):
            where = f"{cls.__qualname__}.{f.name}"
            resolved.append((f.name, self.__resolve(hints[f.name], where), f))

        self.__structs[cls] = _RawStruct(cls, resolved)

    def __add_enum(self, cls: type[Enum]):
        if cls in self.__enums:
            return

        members = tuple(cls)
        if not members:
            raise SchemaError(f"{cls.__qualname__}: enums must have members")

        int_values = all(
            type(m.value) is int and -(2**63) <= m.value < 2**63 for m in members
        )
        self.__enums[cls] = s.Enum(
            module=cls.__module__,
            name=cls.__name__,
            tag=f"{cls.__module__}.{cls.__qualname__}",
            members=tuple(
                s.EnumMember(m.name, m.value if int_values else i)
                for i, m in enumerate(members)
            ),
            cls=cls,
        )

    def __resolve(
        self, tp: Any, where: str, shared: bool = False, discriminated: bool = False
    ) -> s.Type:
        origin = get_origin(tp)

        if origin is Annotated:
            base, *metadata = get_args(tp)
            shared = shared or any(isinstance(m, SharedMarker) for m in metadata)
            discriminated = any(isinstance(m, DiscriminatedMarker) for m in metadata)
            resolved = self.__resolve(base, where, shared, discriminated)

            for m in metadata:
                if isinstance(m, IntSpec):
                    if not isinstance(resolved, s.Int) or m.bits not in (8, 16, 32, 64):
                        raise SchemaError(
                            f"{where}: {m} applies only to int, with 8, 16, 32 or 64 bits"
                        )
                    resolved = s.Int(m.bits, m.signed)
                elif isinstance(m, FloatSpec):
                    if not isinstance(resolved, s.Float) or m.bits not in (32, 64):
                        raise SchemaError(
                            f"{where}: {m} applies only to float, with 32 or 64 bits"
                        )
                    resolved = s.Float(m.bits)

            return resolved

        if isinstance(tp, typing.TypeAliasType):
            return self.__resolve(tp.__value__, where, shared, discriminated)

        if isinstance(tp, typing.NewType):
            return self.__resolve(tp.__supertype__, where, shared, discriminated)

        if origin in (typing.Union, UnionType):
            return self.__union(get_args(tp), where, shared, discriminated)

        if discriminated:
            raise SchemaError(f"{where}: DiscriminatedUnion needs at least two types")

        if isinstance(tp, type) and origin is None:
            return self.__resolve_class(tp, where, shared)

        if shared:
            raise SchemaError(
                f"{where}: Shared applies only to @native classes, or unions of them"
            )

        args = get_args(tp)
        if origin is tuple:
            if len(args) == 2 and args[1] is Ellipsis:
                return s.Sequence(self.__resolve(args[0], where))
            if not args or Ellipsis in args:
                raise SchemaError(
                    f"{where}: unsupported tuple type {tp}: use tuple[T, ...] or tuple[A, B, ...]"
                )
            return s.Tuple(tuple(self.__resolve(a, where) for a in args))

        if origin is frozenset:
            return s.Set(self.__key(args[0], where))

        if origin is abc.Mapping:
            return s.Map(self.__key(args[0], where), self.__resolve(args[1], where))

        if origin in _ALTERNATIVES:
            raise SchemaError(
                f"{where}: {tp} is not supported, as only immutable types are: use {_ALTERNATIVES[origin]}"
            )

        raise SchemaError(f"{where}: unsupported type {tp}")

    def __resolve_class(self, tp: type, where: str, shared: bool) -> s.Type:
        if tp is NoneType:
            raise SchemaError(f"{where}: None alone is not a supported type")

        if is_dataclass(tp):
            if not is_native(tp):
                raise SchemaError(
                    f"{where}: dataclass {tp.__qualname__} must be decorated with @native"
                )
            _check_top_level(tp)
            self.__use_module(tp.__module__)
            ref = s.StructRef(tp.__module__, tp.__name__, tp)
            return s.Shared(ref) if shared else ref

        if shared:
            raise SchemaError(
                f"{where}: Shared applies only to @native classes, or unions of them, not {tp}"
            )

        if tp in _SCALARS:
            return _SCALARS[tp]

        if issubclass(tp, Enum):
            if issubclass(tp, Flag):
                raise SchemaError(f"{where}: Flag enums are not supported")
            _check_top_level(tp)
            self.__use_module(tp.__module__)
            return s.EnumRef(tp.__module__, tp.__name__, tp)

        if tp in _PARAMETERISED:
            raise SchemaError(
                f"{where}: {tp.__name__} must be parameterised, e.g. {_PARAMETERISED[tp]}"
            )

        if tp in _ALTERNATIVES:
            raise SchemaError(
                f"{where}: {tp.__name__} is not supported, as only immutable types are: use {_ALTERNATIVES[tp]}"
            )

        raise SchemaError(f"{where}: unsupported type {tp.__qualname__}")

    def __key(self, tp: Any, where: str) -> s.Type:
        key = self.__resolve(tp, where)
        if not s.is_key(key):
            raise SchemaError(
                f"{where}: frozenset elements and Mapping keys must be scalars or enums, not {tp}"
            )
        return key

    def __union(
        self, args: tuple[Any, ...], where: str, shared: bool, discriminated: bool
    ) -> s.Type:
        if discriminated and NoneType in args:
            raise SchemaError(
                f"{where}: DiscriminatedUnion cannot include None: use DiscriminatedUnion[...] | None"
            )

        optional = NoneType in args
        others = len(args) - optional > 1
        members: list[s.Type] = []
        for arg in (a for a in args if a is not NoneType):
            member = self.__resolve(arg, where, shared)
            if isinstance(member, s.Optional):
                optional = True
                member = member.inner

            if isinstance(member, s.Union) and others:
                if member.discriminated:
                    raise SchemaError(
                        f"{where}: include all the members of a union in its DiscriminatedUnion"
                    )
                members.extend(member.members)
            else:
                members.append(member)

        members = list(dict.fromkeys(members))
        if not members:
            raise SchemaError(f"{where}: None alone is not a supported type")

        if len(members) == 1:
            inner = members[0]
        else:
            inner = s.Union(tuple(members), discriminated)
            self.__check_union(inner, where)

        return s.Optional(inner) if optional else inner

    def __check_union(self, union: s.Union, where: str):
        if union.discriminated:
            tags: dict[str, s.Type] = {}
            for member in union.members:
                tag = _tag(member)
                if tag is None:
                    raise SchemaError(
                        f"{where}: DiscriminatedUnion members must be scalars, enums or @native classes, not {member}"
                    )
                if tag in tags:
                    raise SchemaError(
                        f"{where}: DiscriminatedUnion members {tags[tag]} and {member} share tag {tag!r}"
                    )
                tags[tag] = member
        else:
            kinds: dict[tuple[str, ...], s.Type] = {}
            for member in union.members:
                kind = s.wire_kind(member)
                if kind in kinds:
                    raise SchemaError(
                        f"{where}: union members {kinds[kind]} and {member} cannot be told apart when serialised "
                        f"(both are {kind[0]}): use DiscriminatedUnion"
                    )
                kinds[kind] = member

    def __check_tags(self):
        tags: dict[str, type] = {}
        for cls in [*self.__structs, *self.__enums]:
            tag = (
                native_info(cls).tag if cls in self.__structs else self.__enums[cls].tag
            )
            if tag in tags:
                raise SchemaError(
                    f"{cls.__qualname__} and {tags[tag].__qualname__} have the same tag {tag!r}"
                )
            tags[tag] = cls

    def __recursive_cycles(self) -> dict[type, frozenset[type]]:
        """For each struct in a cycle of types held by value, the other structs in that cycle"""
        for raw in self.__structs.values():
            for _, t, _ in raw.fields:
                for ref, _ in _struct_refs(t):
                    if ref.cls not in self.__structs:
                        raise SchemaError(
                            f"{ref.cls.__qualname__} is not a module-level class of {ref.module}"
                        )

        edges = {
            cls: [
                ref.cls
                for _, t, _ in raw.fields
                for ref, by_value in _struct_refs(t)
                if by_value
            ]
            for cls, raw in self.__structs.items()
        }

        cycles = {}
        for component in _strongly_connected(edges):
            if len(component) > 1 or component[0] in edges[component[0]]:
                modules = {c.__module__ for c in component}
                if len(modules) > 1:
                    names = ", ".join(
                        f"{c.__module__}.{c.__qualname__}" for c in component
                    )
                    raise SchemaError(
                        f"Types in a cycle must be in the same module: {names}"
                    )
                for cls in component:
                    cycles[cls] = frozenset(component)

        return cycles

    def __struct(self, raw: _RawStruct, cycle: frozenset[type]) -> s.Struct:
        cls = raw.cls
        return s.Struct(
            module=cls.__module__,
            name=cls.__name__,
            tag=native_info(cls).tag,
            fields=tuple(
                s.Field(
                    name, _indirect(t, cycle), _default(f, f"{cls.__qualname__}.{name}")
                )
                for name, t, f in raw.fields
            ),
            cls=cls,
        )


def _tag(t: s.Type) -> str | None:
    match t:
        case s.Int(bits=64, signed=True):
            return "int"
        case s.Int(bits=bits, signed=signed):
            return f"{'' if signed else 'u'}int{bits}"
        case s.Float(bits=64):
            return "float"
        case s.Float(bits=bits):
            return f"float{bits}"
        case (
            s.Bool()
            | s.Str()
            | s.Bytes()
            | s.Date()
            | s.Time()
            | s.DateTime()
            | s.TimeDelta()
        ):
            return type(t).__name__.lower()
        case s.EnumRef(cls=cls):
            return f"{cls.__module__}.{cls.__qualname__}"
        case s.StructRef(cls=cls) | s.Shared(target=s.StructRef(cls=cls)):
            return native_info(cls).tag
        case _:
            return None


def walk(t: s.Type) -> Iterator[s.Type]:
    """t and all the types within it"""
    yield t
    match t:
        case s.Optional(inner):
            yield from walk(inner)
        case s.Sequence(element) | s.Set(element):
            yield from walk(element)
        case s.Tuple(members) | s.Union(members):
            for member in members:
                yield from walk(member)
        case s.Map(key, value):
            yield from walk(key)
            yield from walk(value)
        case s.Shared(target) | s.Indirect(target):
            yield target


def _struct_refs(
    t: s.Type, by_value: bool = True
) -> Iterator[tuple[s.StructRef, bool]]:
    """
    The structs t refers to, and whether each is held by value. Those in sequences are not, as the sequence's
    storage is out of line, so it may hold a type being defined (std::vector, Vec)
    """
    match t:
        case s.StructRef():
            yield t, by_value
        case s.Shared(target) | s.Indirect(target):
            yield target, False
        case s.Optional(inner):
            yield from _struct_refs(inner, by_value)
        case s.Sequence(element):
            yield from _struct_refs(element, False)
        case s.Tuple(members) | s.Union(members):
            for member in members:
                yield from _struct_refs(member, by_value)
        case s.Map(_, value):
            yield from _struct_refs(value, by_value)
        case _:
            pass


def _indirect(t: s.Type, cycle: frozenset[type]) -> s.Type:
    """t, with the structs it holds by value that are in its struct's cycle made Indirect"""
    if not cycle:
        return t

    match t:
        case s.StructRef(cls=cls) if cls in cycle:
            return s.Indirect(t)
        case s.Optional(inner):
            return s.Optional(_indirect(inner, cycle))
        case s.Tuple(members):
            return s.Tuple(tuple(_indirect(m, cycle) for m in members))
        case s.Union(members, discriminated):
            return s.Union(tuple(_indirect(m, cycle) for m in members), discriminated)
        case s.Map(key, value):
            return s.Map(key, _indirect(value, cycle))
        case _:
            return t


def _default(f: Field, where: str) -> Any:
    if f.default is not MISSING:
        return f.default

    factory = f.default_factory
    if factory is MISSING:
        return s.NO_DEFAULT

    if factory in (tuple, frozenset, dict) or (
        isinstance(factory, type) and is_native(factory)
    ):
        return factory()

    raise SchemaError(
        f"{where}: default_factory {factory!r} is not supported: only tuple, frozenset, dict or a @native class, "
        "as a default must be the same in each language"
    )


def _check_defaults(schema: s.Schema):
    for module in schema.modules.values():
        for struct in module.structs:
            for f in struct.fields:
                if f.has_default and not s.conforms(f.type, f.default, schema):
                    raise SchemaError(
                        f"{struct.cls.__qualname__}.{f.name}: default {f.default!r} does not conform to its type"
                    )


def _strongly_connected(edges: dict[type, list[type]]) -> list[list[type]]:
    """Tarjan's algorithm"""
    index: dict[type, int] = {}
    low: dict[type, int] = {}
    stack: list[type] = []
    on_stack: set[type] = set()
    components: list[list[type]] = []

    def visit(node: type):
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)

        for target in edges[node]:
            if target not in index:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index[target])

        if low[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member is node:
                    break
            components.append(component)

    for node in edges:
        if node not in index:
            visit(node)

    return components


def _ordered(structs: list[s.Struct]) -> tuple[s.Struct, ...]:
    """In definition order, except that each follows the structs of its module it holds by value"""
    names = {st.name for st in structs}
    needs = {
        st.name: {
            ref.name
            for f in st.fields
            for ref, by_value in _struct_refs(f.type)
            if by_value and ref.module == st.module and ref.name in names
        }
        for st in structs
    }

    ordered: list[s.Struct] = []
    done: set[str] = set()
    remaining = list(structs)
    while remaining:
        st = next(st for st in remaining if needs[st.name] <= done)
        ordered.append(st)
        done.add(st.name)
        remaining.remove(st)

    return tuple(ordered)


def _dependencies(name: str, structs: list[s.Struct]) -> tuple[str, ...]:
    modules = {
        t.module
        for st in structs
        for f in st.fields
        for t in walk(f.type)
        if isinstance(t, s.StructRef | s.EnumRef)
    }
    return tuple(sorted(modules - {name}))


def _check_module_cycles(modules: dict[str, s.Module]):
    visiting: list[str] = []
    done: set[str] = set()

    def visit(name: str):
        if name in visiting:
            cycle = " -> ".join([*visiting[visiting.index(name) :], name])
            raise SchemaError(f"Modules cannot use each other's types: {cycle}")
        if name in done or name not in modules:
            return
        visiting.append(name)
        for dependency in modules[name].dependencies:
            visit(dependency)
        visiting.pop()
        done.add(name)

    for module in modules:
        visit(module)


def loaded_modules(package: str) -> list[str]:
    """The modules of an imported package, and its subpackages, defining @native classes"""
    prefix = package + "."
    return sorted(
        {
            cls.__module__
            for cls in list(_registry)
            if cls.__module__ == package or cls.__module__.startswith(prefix)
        }
        & set(sys.modules)
    )
