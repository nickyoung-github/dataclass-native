import datetime as dt

import pytest
from schemas import common, trades, tree

from dataclass_native import schema as s
from dataclass_native.introspect import build_schema
from dataclass_native.schema import SchemaError


def _fields(struct: s.Struct) -> dict[str, s.Field]:
    return {f.name: f for f in struct.fields}


def _struct(schema: s.Schema, cls: type) -> s.Struct:
    return schema.struct(s.StructRef(cls.__module__, cls.__name__, cls))


def test_dependencies_are_included():
    schema = build_schema(["schemas.trades"])
    assert list(schema.modules) == ["schemas.trades", "schemas.common"]
    assert schema.modules["schemas.trades"].dependencies == ("schemas.common",)
    assert schema.modules["schemas.common"].dependencies == ()


def test_types():
    schema = build_schema([trades])
    fields = {
        name: f.type for name, f in _fields(_struct(schema, trades.Trade)).items()
    }

    party = s.StructRef("schemas.common", "Party", common.Party)
    fund = s.StructRef("schemas.common", "Fund", common.Fund)
    currency = s.EnumRef("schemas.common", "Currency", common.Currency)

    assert fields["id"] == s.Int()
    assert fields["side"] == s.EnumRef("schemas.common", "Side", common.Side)
    assert fields["instrument"] == s.Union(
        (
            s.StructRef("schemas.trades", "Cash", trades.Cash),
            s.StructRef("schemas.trades", "Bond", trades.Bond),
        ),
        discriminated=True,
    )
    assert fields["counterparty"] == s.Union((s.Shared(party), s.Shared(fund)))
    assert fields["flags"] == s.Int(8, signed=False)
    assert fields["tags"] == s.Sequence(s.Str())
    assert fields["labels"] == s.Set(s.Str())
    assert fields["limits"] == s.Map(currency, s.Float())
    assert fields["pair"] == s.Tuple((s.Int(), s.Str()))
    assert fields["payload"] == s.Bytes()
    assert fields["note"] == s.Optional(s.Union((s.Str(), s.Int())))
    assert fields["trade_date"] == s.Date()
    assert fields["cut_off"] == s.Time()
    assert fields["executed"] == s.DateTime()
    assert fields["settle_lag"] == s.TimeDelta()
    assert fields["broker"] == s.Optional(s.Shared(party))
    assert fields["parties"] == s.Sequence(s.Shared(party))

    assert _fields(_struct(schema, trades.Bond))["coupon"].type == s.Float(32)


def test_defaults():
    schema = build_schema([trades])
    fields = _fields(_struct(schema, trades.Trade))
    assert not fields["id"].has_default
    assert fields["limits"].default == {}
    assert fields["price"].default == common.Money(100.0, common.Currency.EUR)
    assert fields["executed"].default == dt.datetime(2026, 11, 27, 12, tzinfo=dt.UTC)


def test_inherited_fields_and_tags():
    schema = build_schema([common])
    assert [f.name for f in _struct(schema, common.Fund).fields] == [
        "name",
        "lei",
        "rating",
        "manager",
    ]
    assert _struct(schema, common.Party).tag == "party.v1"
    assert _struct(schema, common.Money).tag == "schemas.common.Money"


def test_enums():
    module = build_schema([common]).modules["schemas.common"]
    enums = {e.name: e for e in module.enums}
    assert [(m.name, m.value) for m in enums["Currency"].members] == [
        ("USD", 1),
        ("EUR", 2),
        ("GBP", 3),
    ]
    assert [(m.name, m.value) for m in enums["Side"].members] == [
        ("BUY", 0),
        ("SELL", 1),
    ]


def test_recursive_types_are_indirect():
    schema = build_schema([tree])
    node = s.StructRef("schemas.tree", "Node", tree.Node)
    expr = s.StructRef("schemas.tree", "Expr", tree.Expr)
    leaf = s.StructRef("schemas.tree", "Leaf", tree.Leaf)
    wrapper = s.StructRef("schemas.tree", "Wrapper", tree.Wrapper)

    node_fields = _fields(schema.struct(node))
    assert node_fields["next"].type == s.Optional(s.Indirect(node))
    # Sequences store their elements out of line already
    assert node_fields["children"].type == s.Sequence(node)

    assert _fields(schema.struct(expr))["left"].type == s.Union(
        (s.Indirect(expr), s.Indirect(leaf)), discriminated=True
    )
    assert _fields(schema.struct(leaf))["parent"].type == s.Optional(
        s.Indirect(wrapper)
    )
    assert _fields(schema.struct(wrapper))["expr"].type == s.Indirect(expr)


def test_structs_follow_those_they_hold(make_module):
    module = make_module(
        """
        from dataclasses import dataclass
        from dataclass_native import native

        @native
        @dataclass(frozen=True)
        class A:
            b: "B"
            c: "tuple[C, ...]" = ()

        @native
        @dataclass(frozen=True)
        class B:
            value: int

        @native
        @dataclass(frozen=True)
        class C:
            value: int
        """
    )
    schema = build_schema([module])
    assert [st.name for st in schema.modules[module.__name__].structs] == [
        "B",
        "A",
        "C",
    ]


def test_aliases(make_module):
    module = make_module(
        """
        from dataclasses import dataclass
        from typing import NewType
        from dataclass_native import Int16, native

        UserId = NewType("UserId", int)
        type Names = tuple[str, ...]

        @native
        @dataclass(frozen=True)
        class User:
            id: UserId
            names: Names
            small: Int16 | None
        """
    )
    fields = _fields(build_schema([module]).modules[module.__name__].structs[0])
    assert fields["id"].type == s.Int()
    assert fields["names"].type == s.Sequence(s.Str())
    assert fields["small"].type == s.Optional(s.Int(16))


_PREAMBLE = """
import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, Flag
from dataclass_native import DiscriminatedUnion, Int8, Shared, native

class Colour(Enum):
    RED = 1

@native
@dataclass(frozen=True)
class A:
    value: int = 0

@native
@dataclass(frozen=True)
class B:
    value: str = ""
"""


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        (
            "list[int]",
            r": list\[int\] is not supported, as only immutable types are: use tuple\[T, \.\.\.\]",
        ),
        ("list", r": list is not supported, as only immutable types are: use tuple"),
        ("Sequence[int]", r"Sequence\[int\] is not supported, as only immutable"),
        ("dict[str, int]", r"use Mapping\[K, V\]"),
        ("set[int]", r"use frozenset\[T\]"),
        ("tuple", r"tuple must be parameterised, e\.g\. tuple\[int, \.\.\.\]"),
        ("frozenset", r"e\.g\. frozenset\[int\]"),
        ("Mapping", r"e\.g\. Mapping\[str, int\]"),
        ("A | B", "cannot be told apart when serialised.*use DiscriminatedUnion"),
        ("str | Colour", "cannot be told apart"),
        ("dt.date | int", "cannot be told apart"),
        ("DiscriminatedUnion[A, B, None]", "cannot include None"),
        (
            "DiscriminatedUnion[A, tuple[int, ...]]",
            "must be scalars, enums or @native classes",
        ),
        ("DiscriminatedUnion[A, B] | int", "include all the members"),
        ("Shared[int]", "Shared applies only to @native classes"),
        ("Shared[tuple[A, ...]]", "Shared applies only to @native classes"),
        ("frozenset[A]", "must be scalars or enums"),
        ("Mapping[A, int]", "must be scalars or enums"),
        ("None", "None alone"),
        ("object", "unsupported type"),
        ("Undecorated", "must be decorated with @native"),
        ("Flags", "Flag enums are not supported"),
        ("Missing", "cannot resolve its type annotations"),
    ],
)
def test_unsupported_types(make_module, annotation, message):
    module = make_module(
        _PREAMBLE
        + f"""
@dataclass(frozen=True)
class Undecorated:
    value: int

class Flags(Flag):
    X = 1

@native
@dataclass(frozen=True)
class Bad:
    bad: "{annotation}"
"""
    )
    with pytest.raises(SchemaError, match=message):
        build_schema([module])


def test_allowed_unions(make_module):
    module = make_module(
        _PREAMBLE
        + """
@native
@dataclass(frozen=True)
class Good:
    numbers: int | float | bool
    shared: Shared[A | B]
    mixed: Shared[A] | B | None
    tagged: DiscriminatedUnion[A, B, int, Colour]
"""
    )
    good = next(
        st
        for st in build_schema([module]).modules[module.__name__].structs
        if st.name == "Good"
    )
    fields = _fields(good)
    a = s.StructRef(module.__name__, "A", module.A)
    b = s.StructRef(module.__name__, "B", module.B)
    assert fields["mixed"].type == s.Optional(s.Union((s.Shared(a), b)))
    tagged = fields["tagged"].type
    assert isinstance(tagged, s.Union) and tagged.discriminated


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ("bad: Int8 = 300", "default 300 does not conform"),
        ("bad: dt.datetime = dt.datetime(2026, 1, 1)", "does not conform"),
        (
            "bad: tuple[int, ...] = field(default_factory=lambda: (1,))",
            "default_factory .* is not supported",
        ),
        ("bad: A = A(value='x')", "does not conform"),
    ],
)
def test_bad_defaults(make_module, declaration, message):
    module = make_module(
        _PREAMBLE
        + f"""
@native
@dataclass(frozen=True)
class Bad:
    {declaration}
"""
    )
    with pytest.raises(SchemaError, match=message):
        build_schema([module])


def test_default_factory_of_native_class(make_module):
    module = make_module(
        _PREAMBLE
        + """
@native
@dataclass(frozen=True)
class Good:
    a: A = field(default_factory=A)
    names: tuple[str, ...] = field(default_factory=tuple)
"""
    )
    good = next(
        st
        for st in build_schema([module]).modules[module.__name__].structs
        if st.name == "Good"
    )
    assert _fields(good)["a"].default == module.A()


def test_cycle_across_modules(make_module):
    one = make_module(
        """
        from dataclasses import dataclass
        from dataclass_native import native

        @native
        @dataclass(frozen=True)
        class One:
            two: "cycle_two.Two | None" = None
        """,
        name="cycle_one",
    )
    two = make_module(
        """
        from dataclasses import dataclass
        from dataclass_native import native
        import cycle_one

        @native
        @dataclass(frozen=True)
        class Two:
            one: "cycle_one.One"
        """,
        name="cycle_two",
    )
    one.cycle_two = two
    with pytest.raises(
        SchemaError, match="Types in a cycle must be in the same module"
    ):
        build_schema([two])


def test_modules_using_each_other(make_module):
    one = make_module(
        """
        from dataclasses import dataclass
        from dataclass_native import native

        @native
        @dataclass(frozen=True)
        class X:
            value: int

        @native
        @dataclass(frozen=True)
        class W:
            y: "uses_two.Y"
        """,
        name="uses_one",
    )
    two = make_module(
        """
        from dataclasses import dataclass
        from dataclass_native import native
        import uses_one

        @native
        @dataclass(frozen=True)
        class Y:
            value: int

        @native
        @dataclass(frozen=True)
        class Z:
            x: "uses_one.X"
        """,
        name="uses_two",
    )
    one.uses_two = two
    with pytest.raises(SchemaError, match="Modules cannot use each other's types"):
        build_schema([two])


def test_duplicate_tags(make_module):
    module = make_module(
        """
        from dataclasses import dataclass
        from dataclass_native import native

        @native(tag="same")
        @dataclass(frozen=True)
        class P:
            value: int

        @native(tag="same")
        @dataclass(frozen=True)
        class Q:
            value: int
        """
    )
    with pytest.raises(SchemaError, match="have the same tag 'same'"):
        build_schema([module])


def test_nested_class(make_module):
    module = make_module(
        """
        from dataclasses import dataclass
        from dataclass_native import native

        class Outer:
            @native
            @dataclass(frozen=True)
            class Inner:
                value: int
        """
    )
    with pytest.raises(SchemaError, match="nested and local classes are not supported"):
        build_schema([module])
