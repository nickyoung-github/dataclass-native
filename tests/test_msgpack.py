"""
The msgpack wire format of WIRE_FORMAT.md, as written and read by the reference implementation. What is written is
compared with what the msgpack package writes for the same plain values. Run in every mode, as test_runtime.py is.
"""

import datetime as dt
import struct
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import msgpack
import pytest
from schemas.common import Currency, Fund, Money, Party, Side
from schemas.kinds import Kinds
from schemas.trades import Bond, Cash, Trade
from schemas.tree import Expr, Leaf, Node, Wrapper
from schemas.validated import Account, Holder

from dataclass_native import (
    Float32,
    Int32,
    Shared,
    UInt8,
    UInt64,
    from_msgpack,
    to_msgpack,
)
from dataclass_native._msgpack import Ext, unpack

UTC = dt.UTC
PARIS = dt.timezone(dt.timedelta(hours=1))


def _packed(plain: Any, **options: Any) -> bytes:
    """A plain value as the msgpack package writes it"""
    packed = msgpack.packb(plain, use_bin_type=True, datetime=True, **options)
    assert packed is not None
    return packed


def _ref(table: int, row: int) -> msgpack.ExtType:
    return msgpack.ExtType(1, struct.pack(">II", table, row))


def _round_trip(value: Any, tp: Any = None) -> Any:
    data = to_msgpack(value, tp)
    read = from_msgpack(tp or type(value), data)
    assert read == value
    # What is written depends only on the value
    assert to_msgpack(read, tp) == data
    return read


@pytest.mark.parametrize(
    ("value", "tp", "plain"),
    [
        (True, bool, True),
        (0, int, 0),
        (-33, int, -33),
        (2**32, int, 2**32),
        (-(2**63), int, -(2**63)),
        (2**64 - 1, UInt64, 2**64 - 1),
        (1.5, float, 1.5),
        # A float, though given as an int
        (1, float, 1.0),
        ("café", str, "café"),
        (b"\x00\xff", bytes, b"\x00\xff"),
        # As bytes, which they are natively
        ((0, 255), tuple[UInt8, ...], b"\x00\xff"),
        (dt.date(1969, 12, 31), dt.date, -1),
        (dt.time(0, 0, 1, 5), dt.time, 1_000_005),
        (dt.timedelta(days=-1, microseconds=1), dt.timedelta, -86_399_999_999),
        # By name
        (Side.SELL, Side, "SELL"),
        (None, int | None, None),
        ((1, "a"), tuple[int, str], [1, "a"]),
        ((1, 2), tuple[int, ...], [1, 2]),
        # In 4, 8 and 12 bytes: the shortest which holds each
        (
            dt.datetime(2026, 1, 1, tzinfo=UTC),
            dt.datetime,
            dt.datetime(2026, 1, 1, tzinfo=UTC),
        ),
        (
            dt.datetime(2026, 1, 1, 0, 0, 0, 5, tzinfo=UTC),
            dt.datetime,
            dt.datetime(2026, 1, 1, 0, 0, 0, 5, tzinfo=UTC),
        ),
        (
            dt.datetime(1969, 12, 31, 23, 59, 59, 5, tzinfo=UTC),
            dt.datetime,
            dt.datetime(1969, 12, 31, 23, 59, 59, 5, tzinfo=UTC),
        ),
        # The instant, whatever its timezone
        (
            dt.datetime(2026, 1, 1, 1, tzinfo=PARIS),
            dt.datetime,
            dt.datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ],
)
def test_values(value, tp, plain):
    assert to_msgpack(value, tp) == _packed(plain)
    _round_trip(value, tp)


def test_float32():
    assert to_msgpack(1.5, Float32) == _packed(1.5, use_single_float=True)
    assert from_msgpack(Float32, to_msgpack(1.5, Float32)) == 1.5
    # A reader takes a float of either size
    assert from_msgpack(float, to_msgpack(1.5, Float32)) == 1.5
    assert from_msgpack(Float32, to_msgpack(1.5, float)) == 1.5


def test_struct():
    money = Money(1.5, Currency.EUR)
    assert to_msgpack(money) == _packed({"amount": 1.5, "currency": "EUR"})
    _round_trip(money)


def test_sets_and_maps_are_ordered():
    kinds = Kinds(
        sides=frozenset({Side.SELL, Side.BUY}),
        by_name={"b": (Side.SELL,), "a": (), "é": (Side.BUY, Side.BUY)},
    )
    written = unpack(to_msgpack(kinds))
    # Enums in the order of their values, not their names: as they are natively
    assert written["sides"] == ["BUY", "SELL"]
    assert list(written["by_name"].items()) == [
        ("a", []),
        ("b", ["SELL"]),
        ("é", ["BUY", "BUY"]),
    ]
    limits = {Currency.GBP: 3.0, Currency.USD: 1.0, Currency.EUR: 2.0}
    assert to_msgpack(limits, Mapping[Currency, float]) == _packed(
        {"USD": 1.0, "EUR": 2.0, "GBP": 3.0}
    )
    _round_trip(kinds)
    _round_trip(frozenset({3, -1, 2}), frozenset[int])


def test_unions():
    money = {"amount": 1.0, "currency": "USD"}
    for number in (0, 1.5, "x"):
        assert unpack(to_msgpack(Kinds(number=number)))["number"] == number
        # An int stays an int, though a float is also a member
        assert type(_round_trip(Kinds(number=number)).number) is type(number)

    for container, plain in (
        ((1, 2), [1, 2]),
        ({"m": Money(1.0)}, {"m": money}),
        (None, None),
    ):
        assert unpack(to_msgpack(Kinds(container=container)))["container"] == plain
        _round_trip(Kinds(container=container))

    for tagged, plain in (
        (Money(1.0), {"t": "schemas.common.Money", "v": money}),
        (Side.SELL, {"t": "schemas.common.Side", "v": "SELL"}),
        (7, {"t": "int", "v": 7}),
    ):
        assert unpack(to_msgpack(Kinds(tagged=tagged)))["tagged"] == plain
        _round_trip(Kinds(tagged=tagged))


def test_all_kinds():
    _round_trip(
        Kinds(
            octets=(0, 255),
            nested=((Money(1.0),), (), (Money(2.0), Money(3.0, Currency.GBP))),
            pair=(1, Money(4.0)),
            blobs=(b"", b"\x00"),
        )
    )


def test_bundle():
    party, fund = Party("Acme", lei="LEI1", rating=3), Fund("Fund", manager="Ann")
    trade = Trade(
        id=1,
        side=Side.SELL,
        instrument=Bond("XS1", coupon=0.5),
        counterparty=fund,
        quantity=2.5,
        limits={Currency.GBP: 2.0, Currency.USD: 1.0},
        note=7,
        executed=dt.datetime(2026, 1, 1, 12, tzinfo=PARIS),
        broker=party,
        # The same party, twice, and one equal to it
        parties=(party, party, replace(party)),
    )
    party_row = {"name": "Acme", "lei": "LEI1", "rating": 3}
    assert to_msgpack(trade) == _packed(
        {
            "root": {
                "id": 1,
                "side": "SELL",
                "instrument": {
                    "t": "schemas.trades.Bond",
                    "v": {"isin": "XS1", "coupon": 0.5},
                },
                "counterparty": _ref(0, 0),
                "price": {"amount": 100.0, "currency": "EUR"},
                "quantity": 2.5,
                "flags": 255,
                "tags": ["a\nb", 'q"uote', "café"],
                "labels": ["x", "y"],
                "limits": {"USD": 1.0, "GBP": 2.0},
                "pair": [1, "one"],
                "payload": b"\x00\x01",
                "note": 7,
                "trade_date": 20784,
                "cut_off": 59_400_000_000,
                "executed": dt.datetime(2026, 1, 1, 11, tzinfo=UTC),
                "settle_lag": 172_800_000_000,
                "broker": _ref(1, 0),
                "parties": [_ref(1, 0), _ref(1, 0), _ref(1, 1)],
            },
            # A table of each type shared, in the order first met: here, by its tag
            "shared": [
                [
                    "schemas.common.Fund",
                    [{"name": "Fund", "lei": None, "rating": 0, "manager": "Ann"}],
                ],
                ["party.v1", [party_row, party_row]],
            ],
        }
    ).replace(
        # The one float of 32 bits
        _packed(0.5),
        _packed(0.5, use_single_float=True),
    )

    read = _round_trip(trade)
    # What was shared still is, and what was not is not
    assert read.broker is read.parties[0] is read.parties[1]
    assert read.parties[2] == read.broker and read.parties[2] is not read.broker
    assert type(read.counterparty) is Fund

    # A bundle though nothing is shared, as its type may share
    alone = Trade(1, Side.BUY, Cash(Money(1.0)), party)
    assert unpack(to_msgpack(alone))["shared"] == [["party.v1", [party_row]]]
    assert unpack(to_msgpack((), tuple[Trade, ...])) == {"root": [], "shared": []}


def test_shared_objects_sharing_others():
    party = Party("p")
    trade = Trade(1, Side.BUY, Cash(Money(1.0)), party, broker=party)
    read = _round_trip(
        (trade, trade, party), tuple[Shared[Trade], Shared[Trade], Shared[Party]]
    )
    assert read[0] is read[1]
    assert read[0].counterparty is read[0].broker is read[2]


def test_object_sharing_itself(make_module):
    module = make_module(
        """
        from dataclasses import dataclass
        from dataclass_native import Shared, native

        @native
        @dataclass(frozen=True)
        class Link:
            next: "Shared[Link] | None" = None
        """
    )
    link: Any = module.Link
    rows = [{"next": _ref(0, 1)}, {"next": _ref(0, 0)}]
    data = _packed({"root": _ref(0, 0), "shared": [[f"{module.__name__}.Link", rows]]})
    # Which no object written could
    with pytest.raises(ValueError, match="Link.next cannot be read"):
        from_msgpack(Shared[link], data)


def test_recursive():
    _round_trip(Node(1, next=Node(2), children=(Node(3, children=(Node(4),)), Node(5))))
    leaf = Leaf(1.0, parent=Wrapper(Expr("neg", Leaf(2.0))))
    expr = Expr("add", Expr("mul", leaf, Leaf(3.0)), leaf)
    assert unpack(to_msgpack(expr))["right"] == {
        "t": "schemas.tree.Leaf",
        "v": {
            "value": 1.0,
            "parent": {
                "expr": {
                    "op": "neg",
                    "left": {
                        "t": "schemas.tree.Leaf",
                        "v": {"value": 2.0, "parent": None},
                    },
                    "right": None,
                }
            },
        },
    }
    _round_trip(expr)


def test_reader_allows_for_other_versions():
    # A field which is not written takes its default, and one which is not a field is ignored
    assert from_msgpack(Money, _packed({"added": [1], "amount": 2.0})) == Money(2.0)
    assert from_msgpack(Party, _packed({"rating": 1, "name": "p"})) == Party(
        "p", rating=1
    )
    with pytest.raises(ValueError, match="Money cannot be read: .*amount"):
        from_msgpack(Money, _packed({"currency": "USD"}))


@pytest.mark.parametrize(
    ("tp", "data", "message"),
    [
        (int, b"", "msgpack data is truncated"),
        (int, b"\xcd\x00", "msgpack data is truncated"),
        (int, b"\x01\x02", "msgpack data has more than one value"),
        (int, b"\xc1", "msgpack data has the unused byte 0xc1"),
        (str, b"\xa1\xff", "msgpack data has a str which is not UTF-8"),
        (int, b"\x82\x01\x02\x01\x03", "msgpack data has a map with the key 1 twice"),
        (int, b"\x81\x90\x01", "msgpack data has a map keyed by an array or a map"),
        (int, _packed("1"), "The data is not a int, as written: '1'"),
        (int, _packed(True), "not a int"),
        (bool, _packed(1), "not a bool"),
        (float, _packed(1), "not a float"),
        (Int32, _packed(2**31), "The data is not a "),
        (dt.date, _packed(10**9), "not a date"),
        (dt.time, _packed(86_400_000_000), "not a time"),
        (dt.timedelta, _packed(1.5), "not a timedelta"),
        # Not a whole number of microseconds
        (dt.datetime, _packed(msgpack.Timestamp(0, 1)), "not a datetime"),
        (dt.datetime, b"\xd4\xff\x00", "not a datetime"),
        (Side, _packed("HOLD"), "not a Side"),
        (tuple[int, str], _packed([1]), "The data is not a tuple"),
        (tuple[UInt8, ...], _packed([1]), "The data is not a tuple"),
        (Money, _packed([]), "The data is not a Money"),
        (Money, _packed({"amount": "1"}), "Money.amount cannot be read from '1'"),
        (Party, _packed({"name": "p", "rating": 2**31}), "Party.rating cannot be read"),
        (int | str, _packed(1.5), "The data is not a int | str"),
        (
            Kinds,
            _packed({"tagged": {"t": "str", "v": "x"}}),
            "Kinds.tagged cannot be read",
        ),
        (Kinds, _packed({"tagged": {"t": "int"}}), "Kinds.tagged cannot be read"),
        # Not a bundle
        (Shared[Party], _packed({"name": "p"}), "The data is not a "),
        (
            Shared[Party],
            _packed({"root": _ref(0, 0), "shared": {}}),
            "The data is not a ",
        ),
        # A reference to no table, to no row, and to an object of another type
        (
            Shared[Party],
            _packed({"root": _ref(0, 0), "shared": []}),
            "The data is not a ",
        ),
        (
            Shared[Party],
            _packed({"root": _ref(0, 1), "shared": [["party.v1", [{}]]]}),
            "not a ",
        ),
        (
            Shared[Party],
            _packed({"root": _ref(0, 0), "shared": [["other", [{}]]]}),
            "not a ",
        ),
        (
            Shared[Party],
            _packed({"root": {"name": "p"}, "shared": []}),
            "The data is not a ",
        ),
        (
            Shared[Party | Fund],
            _packed({"root": _ref(0, 0), "shared": [["other", [{}]]]}),
            "The data is not a ",
        ),
    ],
)
def test_invalid_data(tp, data, message):
    with pytest.raises(ValueError, match=message):
        from_msgpack(tp, data)


@pytest.mark.parametrize(
    ("value", "tp"),
    [
        ("1", int),
        (True, int),
        (1.5, int),
        (2**63, int),
        (256, UInt8),
        (1, bool),
        (1, str),
        ("\ud800", str),
        (1e300, Float32),
        (bytearray(b"x"), bytes),
        ([1], tuple[int, ...]),
        ((1, 2), tuple[int, str]),
        ((256,), tuple[UInt8, ...]),
        ({1}, frozenset[int]),
        (frozenset({1, "a"}), frozenset[int]),
        ([("a", 1)], Mapping[str, int]),
        ("BUY", Side),
        (dt.datetime(2026, 1, 1), dt.datetime),
        (dt.datetime(2026, 1, 1, tzinfo=UTC), dt.date),
        (dt.time(1, tzinfo=UTC), dt.time),
        (Fund("f"), Party),
        (Fund("f"), Shared[Party]),
        (1.5, int | str),
        ([], Kinds),
    ],
)
def test_invalid_values(value, tp):
    with pytest.raises(TypeError, match="cannot be written as "):
        to_msgpack(value, tp)


def test_invalid_field():
    # In a bound mode, it is making the object which raises: a bound class holds only what is of its fields' types
    invalid: Any = Money
    with pytest.raises(TypeError, match="Money.amount cannot be 'x'"):
        to_msgpack(Cash(invalid("x")))
    nested: Any = ((1,),)
    with pytest.raises(TypeError, match=r"Kinds.nested cannot be \(\(1,\),\)"):
        to_msgpack(replace(Kinds(), nested=nested))


def test_pydantic_dataclass():
    account = Account(Holder("h", Party("p"), rank=2), Money(1.5), limit=3, tags=("a",))
    assert _round_trip(account).holder.party == Party("p")
    # Validated as it is read, as when it is made
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        from_msgpack(
            Account,
            to_msgpack(account).replace(
                _packed("limit") + _packed(3), _packed("limit") + _packed(-1)
            ),
        )


@pytest.mark.parametrize("length", [0, 15, 16, 31, 32, 255, 256, 65535, 65536])
def test_lengths(length):
    """Each length is written in the shortest form which holds it, and read from it"""
    table = {str(i): i for i in range(length)}
    cases: list[tuple[Any, Any, Any]] = [
        ("x" * length, str, "x" * length),
        (b"x" * length, bytes, b"x" * length),
        (tuple(range(length)), tuple[int, ...], list(range(length))),
        (table, Mapping[str, int], dict(sorted(table.items()))),
    ]
    for value, tp, plain in cases:
        data = to_msgpack(value, tp)
        assert data == _packed(plain)
        assert from_msgpack(tp, data) == value


@pytest.mark.parametrize("length", [0, 1, 2, 3, 4, 8, 16, 17, 255, 256, 65536])
def test_extensions_are_read(length):
    data = b"x" * length
    assert unpack(_packed(msgpack.ExtType(5, data))) == Ext(5, data)
    # msgpack's own types have negative codes
    assert unpack(b"\xd4\x80x") == Ext(-128, b"x")
