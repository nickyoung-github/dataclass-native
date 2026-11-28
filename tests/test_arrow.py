"""
The Arrow tables and Parquet files of ARROW_FORMAT.md, as written and read by the reference implementation. Run in
every mode, as test_runtime.py is.
"""

import datetime as dt
from dataclasses import replace
from typing import Any
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from schemas.common import Currency, Fund, Money, Party, Side
from schemas.kinds import Graded, Kinds, Level, Link
from schemas.sub.inner import Order
from schemas.trades import Bond, Cash, Trade
from schemas.tree import Node
from schemas.validated import Account, Holder

from dataclass_native import (
    ArrowTables,
    Mode,
    _arrow,
    from_arrow,
    from_parquet,
    mode,
    to_arrow,
    to_parquet,
)
from dataclass_native._arrow import read, write
from dataclass_native.schema import SchemaError

BOUND = mode() is not Mode.PURE

UTC = dt.UTC
ENUM = pa.dictionary(pa.int32(), pa.string())
MONEY = pa.struct([("amount", pa.float64()), ("currency", ENUM)])
PARTY, FUND = Party("Acme", lei="LEI1", rating=3), Fund("Fund", manager="Ann")


def _trades() -> tuple[Trade, ...]:
    first = Trade(
        id=1,
        side=Side.SELL,
        instrument=Bond("XS1", coupon=0.5),
        counterparty=FUND,
        quantity=2.5,
        limits={Currency.GBP: 2.0, Currency.USD: 1.0},
        note=7,
        executed=dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=1))),
        broker=PARTY,
        # The same party, twice, and one equal to it
        parties=(PARTY, PARTY, replace(PARTY)),
    )
    second = replace(
        first,
        id=2,
        instrument=Cash(Money(1.0)),
        counterparty=PARTY,
        labels=frozenset(),
        note=None,
        broker=None,
        parties=(),
    )
    return first, second


def _same(table: Any, reference: Any, metadata: bool = True):
    """A table holds what a reference does: but for which names an enum's dictionary has, which may differ"""
    table.validate(full=True)
    assert table.schema.equals(reference.schema, check_metadata=metadata)
    assert table.to_pylist() == reference.to_pylist()


def _round_trip(values: tuple[Any, ...]) -> tuple[Any, ...]:
    cls = type(values[0])
    tables = to_arrow(values)
    # The implementation in Python, which that used may not be, holds the same
    reference = write(values)
    _same(tables.root, reference.root)
    assert list(tables.shared) == list(reference.shared)
    for tag, table in tables.shared.items():
        _same(table, reference.shared[tag])

    result = from_arrow(cls, tables)
    assert result == values
    # And each reads what the other holds
    assert read(cls, tables) == values
    assert from_arrow(cls, reference) == values
    return result


def test_bound_objects_are_held_natively():
    trades = _trades()
    tables = to_arrow(trades)
    with (
        mock.patch.object(_arrow, "write", side_effect=NotImplementedError),
        mock.patch.object(_arrow, "read", side_effect=NotImplementedError),
    ):
        if BOUND:
            assert from_arrow(Trade, to_arrow(trades)) == trades
        else:
            with pytest.raises(NotImplementedError):
                to_arrow(trades)
            with pytest.raises(NotImplementedError):
                from_arrow(Trade, tables)


def test_tables_in_parts():
    trades = _trades()
    tables = to_arrow(trades)
    # A table which starts within its arrays, as do those it holds
    assert (
        from_arrow(Trade, ArrowTables(tables.root.slice(1), tables.shared))
        == trades[1:]
    )
    party = tables.shared["party.v1"]
    padded = pa.concat_tables([party.slice(1), party]).slice(1)
    assert (
        from_arrow(
            Trade, ArrowTables(tables.root, tables.shared | {"party.v1": padded})
        )
        == trades
    )
    kinds = (
        Kinds(),
        Kinds(
            nested=((Money(1.0),), ()), by_name={"a": (Side.BUY,)}, pair=(1, Money(2.0))
        ),
    )
    assert (
        from_arrow(Kinds, to_arrow(kinds * 3).root.slice(3, 2)) == kinds[1:] + kinds[:1]
    )
    # And one of several arrays
    assert (
        from_arrow(
            Trade, ArrowTables(pa.concat_tables([tables.root] * 3), tables.shared)
        )
        == trades * 3
    )


def test_table():
    table = to_arrow((Money(1.5, Currency.EUR), Money(2.0))).root
    assert table.schema.equals(
        pa.schema([("amount", pa.float64()), ("currency", ENUM)]), check_metadata=False
    )
    # The tag of the class its rows are of
    assert table.schema.metadata == {b"dataclass_native:tag": b"schemas.common.Money"}
    assert table.to_pylist() == [
        {"amount": 1.5, "currency": "EUR"},
        {"amount": 2.0, "currency": "USD"},
    ]
    # Of a class which shares nothing, the table alone is read
    assert from_arrow(Money, table) == (Money(1.5, Currency.EUR), Money(2.0))


def test_types():
    schema = to_arrow((), Trade).root.schema
    expected = {
        "id": pa.int64(),
        "side": ENUM,
        # A union: a struct of its members, named by their tags, of which one is not null
        "instrument": pa.struct(
            [
                ("schemas.trades.Cash", pa.struct([("money", MONEY)])),
                (
                    "schemas.trades.Bond",
                    pa.struct([("isin", pa.string()), ("coupon", pa.float32())]),
                ),
            ]
        ),
        # Shared objects: their rows in the tables of their classes
        "counterparty": pa.struct(
            [("party.v1", pa.uint32()), ("schemas.common.Fund", pa.uint32())]
        ),
        "price": MONEY,
        "quantity": pa.float64(),
        "flags": pa.uint8(),
        "tags": pa.list_(pa.string()),
        "labels": pa.list_(pa.string()),
        "limits": pa.map_(ENUM, pa.float64(), keys_sorted=True),
        # A tuple: a struct, of fields named by position
        "pair": pa.struct([("0", pa.int64()), ("1", pa.string())]),
        "payload": pa.binary(),
        "note": pa.struct([("str", pa.string()), ("int", pa.int64())]),
        "trade_date": pa.date32(),
        "cut_off": pa.time64("us"),
        "executed": pa.timestamp("us", tz="UTC"),
        "settle_lag": pa.duration("us"),
        "broker": pa.uint32(),
        "parties": pa.list_(pa.uint32()),
    }
    assert schema.names == list(expected)
    for name, typ in expected.items():
        assert schema.field(name).type.equals(typ), name
    # What a reference is to
    assert schema.field("broker").metadata == {b"dataclass_native:shared": b"party.v1"}
    assert schema.field("parties").type.value_field.metadata == {
        b"dataclass_native:shared": b"party.v1"
    }

    kinds = to_arrow((), Kinds).root.schema
    assert kinds.field("octets").type.equals(pa.binary())
    assert kinds.field("container").type.equals(
        pa.struct(
            [
                ("array", pa.list_(pa.int64())),
                ("map", pa.map_(pa.string(), MONEY, keys_sorted=True)),
            ]
        )
    )
    assert kinds.field("tagged").type.equals(
        pa.struct(
            [
                ("schemas.common.Money", MONEY),
                ("schemas.common.Side", ENUM),
                ("int", pa.int64()),
            ]
        )
    )


def test_shared():
    trades = _trades()
    tables = to_arrow(trades)
    # A table of each class which may be shared, in which each object shared is once
    party = {"name": "Acme", "lei": "LEI1", "rating": 3}
    assert {tag: table.to_pylist() for tag, table in tables.shared.items()} == {
        "party.v1": [party, party],
        "schemas.common.Fund": [
            {"name": "Fund", "lei": None, "rating": 0, "manager": "Ann"}
        ],
    }
    rows = tables.root.select(
        [
            "counterparty",
            "broker",
            "parties",
            "instrument",
            "note",
            "limits",
            "executed",
        ]
    ).to_pylist()
    assert rows[0] == {
        "counterparty": {"party.v1": None, "schemas.common.Fund": 0},
        "broker": 0,
        "parties": [0, 0, 1],
        "instrument": {
            "schemas.trades.Cash": None,
            "schemas.trades.Bond": {"isin": "XS1", "coupon": 0.5},
        },
        "note": {"str": None, "int": 7},
        # In the order of the enum's values, not of its names
        "limits": [("USD", 1.0), ("GBP", 2.0)],
        "executed": dt.datetime(2026, 1, 1, 11, tzinfo=UTC),
    }
    assert rows[1]["counterparty"] == {"party.v1": 0, "schemas.common.Fund": None}
    assert rows[1]["broker"] is None and rows[1]["note"] is None

    read = _round_trip(trades)
    # What was shared still is, across rows too, and what was not is not
    assert (
        read[0].broker
        is read[0].parties[0]
        is read[0].parties[1]
        is read[1].counterparty
    )
    assert read[0].parties[2] is not read[0].broker
    assert type(read[0].counterparty) is Fund

    # A table of each class which may be shared, though none is
    empty = to_arrow((), Trade)
    assert empty.root.num_rows == 0 and from_arrow(Trade, empty) == ()
    assert {tag: table.num_rows for tag, table in empty.shared.items()} == {
        "party.v1": 0,
        "schemas.common.Fund": 0,
    }


def test_all_kinds():
    _round_trip(
        (
            Kinds(),
            Kinds(
                octets=(0, 255),
                number=1.5,
                nested=((Money(1.0),), (), (Money(2.0), Money(3.0, Currency.GBP))),
                by_name={"b": (Side.SELL,), "a": (), "é": (Side.BUY, Side.BUY)},
                sides=frozenset({Side.SELL, Side.BUY}),
                pair=(-(2**63), Money(4.0)),
                container={"m": Money(1.0)},
                tagged=Side.SELL,
                blobs=(b"", b"\x00"),
            ),
            Kinds(number="x", container=(1, 2), tagged=Money(5.0)),
        )
    )
    _round_trip((Order(default=-33, new=True, total=Money(65536.5)),))


def test_shared_objects_of_their_own_class():
    last = Link("c")
    first = Link("a", Link("b", last))
    tables = to_arrow((first, last))
    # Its own class's table holds what a Link shares, not the Links which are written
    assert tables.root.to_pylist() == [
        {"name": "a", "next": 0},
        {"name": "c", "next": None},
    ]
    assert tables.shared["schemas.kinds.Link"].to_pylist() == [
        {"name": "b", "next": 1},
        {"name": "c", "next": None},
    ]
    read = _round_trip((first, last))
    assert read[0].next.next == read[1]


def test_class_which_holds_itself():
    with pytest.raises(
        SchemaError, match="Node cannot be written as Arrow, as Node holds itself"
    ):
        to_arrow((Node(1),))
    with pytest.raises(SchemaError, match="Account cannot be written as Arrow"):
        to_arrow((), Account)


def test_pydantic_dataclass():
    holders = (Holder("h", PARTY, rank=2), Holder("i", PARTY), Holder("j"))
    read = _round_trip(holders)
    assert read[0].party is read[1].party


def test_parquet(tmp_path):
    trades = _trades()
    to_parquet(trades, tmp_path / "trades")
    assert sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.parquet")
    ) == [
        "trades/root.parquet",
        "trades/shared/party.v1.parquet",
        "trades/shared/schemas.common.Fund.parquet",
    ]
    read = from_parquet(Trade, tmp_path / "trades")
    assert read == trades
    assert read[0].broker is read[1].counterparty
    # The files are of the tables
    # But for the metadata of the fields within its columns, which Parquet does not keep
    root = pq.read_table(tmp_path / "trades" / "root.parquet")
    _same(root, to_arrow(trades).root, metadata=False)
    assert root.schema.metadata == {b"dataclass_native:tag": b"schemas.trades.Trade"}

    # Of a class which shares nothing, the one file
    to_parquet((Money(1.0),), tmp_path / "money")
    assert [p.name for p in (tmp_path / "money").iterdir()] == ["root.parquet"]
    assert from_parquet(Money, tmp_path / "money") == (Money(1.0),)
    assert from_arrow(Money, pq.read_table(tmp_path / "money" / "root.parquet")) == (
        Money(1.0),
    )


def test_reader_allows_for_other_versions():
    # A field with no column takes its default, a column which is not a field is ignored, and columns are in any order
    table = pa.table(
        {"added": [1, 2], "rating": pa.array([1, 2], pa.int32()), "name": ["p", "q"]}
    )
    assert from_arrow(Party, table) == (Party("p", rating=1), Party("q", rating=2))
    # An enum's names need not be in a dictionary, nor a string a small one
    table = pa.table(
        {"amount": [1.0], "currency": pa.array(["GBP"], pa.large_string())}
    )
    assert from_arrow(Money, table) == (Money(1.0, Currency.GBP),)
    with pytest.raises(ValueError, match="Money cannot be read: .*amount"):
        from_arrow(Money, pa.table({"currency": ["USD"]}))


@pytest.mark.parametrize(
    ("cls", "table", "message"),
    [
        (Money, {"amount": ["1"]}, "Money.amount cannot be read from '1'"),
        (Money, {"amount": [1]}, "Money.amount cannot be read from 1"),
        # Not None, as the field's type is not
        (
            Money,
            {"amount": pa.array([None], pa.float64())},
            "Money.amount cannot be read from None",
        ),
        (
            Money,
            {"amount": [1.0], "currency": ["JPY"]},
            "Money.currency cannot be read from 'JPY'",
        ),
        (Party, {"name": ["p"], "rating": [2**31]}, "Party.rating cannot be read"),
        (Kinds, {"number": [{"int": 1, "str": "x"}]}, "Kinds.number cannot be read"),
        (
            Kinds,
            {"number": [{"int": None, "str": None}]},
            "Kinds.number cannot be read",
        ),
        (Kinds, {"pair": [{"0": 1}]}, "Kinds.pair cannot be read"),
        (Kinds, {"sides": [["HOLD"]]}, "Kinds.sides cannot be read"),
        # A reference to a table which there is not, and to no row of one
        (
            Holder,
            {"name": ["h"], "party": pa.array([0], pa.uint32())},
            "Holder.party cannot be read from 0",
        ),
    ],
)
def test_invalid_tables(cls, table, message):
    with pytest.raises(ValueError, match=message):
        from_arrow(cls, pa.table(table))


def test_object_sharing_itself():
    links = pa.table({"name": ["a", "b"], "next": pa.array([1, 0], pa.uint32())})
    tables = ArrowTables(links.slice(0, 1), {"schemas.kinds.Link": links})
    # Which no object written could
    with pytest.raises(ValueError, match="Link.next cannot be read"):
        from_arrow(Link, tables)


def test_enums_by_value():
    # A plain dataclass holds the values it is given, and a bound one their members: both are written as the members
    graded: Any = Graded
    by_value = graded(
        "high", "sell", (2, Currency.GBP), frozenset({"low"}), {"buy": "high"}
    )
    members = Graded(
        Level.HIGH,
        Side.SELL,
        (Currency.EUR, Currency.GBP),
        frozenset({Level.LOW}),
        {Side.BUY: Level.HIGH},
    )
    tables = to_arrow((by_value,))
    assert tables.root.to_pylist() == to_arrow((members,)).root.to_pylist()
    assert tables.root.column("by_side").to_pylist() == [[("BUY", "HIGH")]]
    assert from_arrow(Graded, tables) == (members,)

    with pytest.raises(TypeError, match="Graded.level cannot be 'HIGH'"):
        to_arrow((graded("HIGH"),))


def test_invalid_values():
    with pytest.raises(TypeError, match="is not a Money"):
        to_arrow((Money(1.0), PARTY))
    with pytest.raises(TypeError, match="The class of no objects must be given"):
        to_arrow(())
    with pytest.raises(TypeError, match="is not a @native class"):
        to_arrow((1,))
    # In a bound mode, it is making the object which raises: a bound class holds only what is of its fields' types
    invalid: Any = Money
    with pytest.raises(TypeError, match="Money.amount cannot be 'x'"):
        to_arrow((Cash(invalid("x")),))
    with pytest.raises(TypeError, match=r"Kinds.nested cannot be \(\(1,\),\)"):
        nested: Any = ((1,),)
        to_arrow((replace(Kinds(), nested=nested),))
