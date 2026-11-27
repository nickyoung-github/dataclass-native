"""
How @native classes behave. Run in pure mode with the other tests, and again by test_bindings.py in the other modes,
with the schemas bound to their extensions: so most of these tests show that the modes agree.
"""

import copy
import datetime as dt
import pickle
from dataclasses import FrozenInstanceError, asdict, dataclass, fields, replace
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from schemas import functions
from schemas.common import Currency, Fund, Money, Party, Side
from schemas.kinds import Kinds
from schemas.sub.inner import Order
from schemas.trades import Bond, Cash, Trade
from schemas.tree import Expr, Leaf, Node, Wrapper
from schemas.validated import Account, Holder

from dataclass_native import Mode, NativeBindingError, mode, native

BOUND = mode() is not Mode.PURE
bound_only = pytest.mark.skipif(not BOUND, reason="Plain dataclasses in pure mode")

UTC = dt.UTC
PARTY = Party("Acme", lei="LEI1", rating=3)
FUND = Fund("Fund", manager="Ann")
MONEY = Money(1.5, Currency.EUR)


def _any(cls: Any) -> Any:
    """The class, for making an instance which a type checker would reject"""
    return cls


def _trade(**changes) -> Trade:
    return Trade(
        **{"id": 1, "side": Side.SELL, "instrument": Cash(MONEY), "counterparty": PARTY}
        | changes
    )


def test_schemas_are_bound():
    assert ("__native__" in vars(MONEY)) is BOUND


def test_fields():
    trade = _trade(
        quantity=2.5,
        flags=7,
        tags=("a", "é", ""),
        labels=frozenset({"x"}),
        limits={Currency.USD: 1.0, Currency.GBP: 2.0},
        pair=(-1, "minus"),
        payload=b"\x00\xff",
        note="note",
        trade_date=dt.date(2000, 2, 29),
        cut_off=dt.time(23, 59, 59, 999_999),
        executed=dt.datetime(1969, 12, 31, 23, 59, 59, 5, tzinfo=UTC),
        settle_lag=dt.timedelta(days=-1, microseconds=1),
        broker=PARTY,
        parties=(PARTY, PARTY),
    )

    assert (trade.id, trade.side, trade.quantity, trade.flags) == (1, Side.SELL, 2.5, 7)
    assert trade.instrument == Cash(MONEY)
    assert trade.price == Money(100.0, Currency.EUR)
    assert trade.tags == ("a", "é", "")
    assert trade.labels == frozenset({"x"})
    assert trade.limits == {Currency.USD: 1.0, Currency.GBP: 2.0}
    assert trade.pair == (-1, "minus")
    assert trade.payload == b"\x00\xff"
    assert trade.note == "note"
    assert trade.trade_date == dt.date(2000, 2, 29)
    assert trade.cut_off == dt.time(23, 59, 59, 999_999)
    assert trade.executed == dt.datetime(1969, 12, 31, 23, 59, 59, 5, tzinfo=UTC)
    assert trade.settle_lag == dt.timedelta(days=-1, microseconds=1)
    assert trade.broker == PARTY
    assert trade.parties[0] == PARTY


def test_types_of_values():
    trade = _trade(note=5, quantity=2)
    assert type(trade.tags) is tuple
    assert type(trade.labels) is frozenset
    assert type(trade.pair) is tuple
    assert type(trade.payload) is bytes
    assert type(trade.note) is int
    assert type(trade.instrument) is Cash
    assert type(trade.side) is Side
    assert type(next(iter(_trade(limits={Currency.USD: 1.0}).limits))) is Currency
    assert _trade(note=None).note is None


def test_defaults():
    trade = _trade()
    assert trade.tags == ("a\nb", 'q"uote', "café")
    assert trade.limits == {}
    assert trade.note == "n/a"
    assert trade.broker is None
    assert trade.parties == ()
    assert Money.currency is Currency.USD
    with pytest.raises(AttributeError):
        _ = Money.amount  # pyright: ignore[reportAttributeAccessIssue]
    assert Order() == Order(default=0, new=False, total=Money(0.0))


def test_datetime_is_an_instant():
    east = dt.timezone(dt.timedelta(hours=5, minutes=30))
    executed = dt.datetime(2026, 1, 1, 0, 0, tzinfo=east)
    trade = _trade(executed=executed)
    assert trade.executed == executed
    # Natively an instant, so given back as UTC
    assert trade.executed.tzinfo == (UTC if BOUND else east)


def test_kinds():
    kinds = Kinds(
        octets=(0, 255),
        number=1.5,
        nested=((MONEY,), (), (MONEY, Money(2.0))),
        by_name={"a": (Side.BUY, Side.SELL), "b": ()},
        sides=frozenset({Side.BUY}),
        pair=(1, MONEY),
        container={"m": MONEY},
        tagged=Side.SELL,
        blobs=(b"a", b""),
    )

    assert kinds.octets == (0, 255)
    assert kinds.number == 1.5
    assert kinds.nested == ((MONEY,), (), (MONEY, Money(2.0)))
    assert kinds.by_name == {"a": (Side.BUY, Side.SELL), "b": ()}
    assert kinds.sides == frozenset({Side.BUY})
    assert kinds.pair == (1, MONEY)
    assert kinds.container == {"m": MONEY}
    assert kinds.tagged is Side.SELL
    assert kinds.blobs == (b"a", b"")
    assert kinds == pickle.loads(pickle.dumps(kinds))

    assert Kinds() == Kinds(number=0, tagged=0)
    assert type(Kinds(number="s").number) is str
    assert type(Kinds(number=2).number) is int
    assert Kinds(container=(1, 2)).container == (1, 2)
    assert Kinds(tagged=MONEY).tagged == MONEY
    assert Kinds(tagged=3).tagged == 3


def test_equality_hash_and_repr():
    assert Money(1.5, Currency.EUR) == MONEY
    assert Money(1.5) != MONEY
    assert hash(Money(1.5, Currency.EUR)) == hash(MONEY)
    assert repr(MONEY) == "Money(amount=1.5, currency=<Currency.EUR: 2>)"
    assert (
        repr(Order(new=True))
        == "Order(default=0, new=True, total=Money(amount=0.0, currency=<Currency.USD: 1>))"
    )


def test_dataclass_functions():
    assert [f.name for f in fields(MONEY)] == ["amount", "currency"]
    assert asdict(Cash(MONEY)) == {"money": {"amount": 1.5, "currency": Currency.EUR}}
    assert replace(MONEY, amount=2.0) == Money(2.0, Currency.EUR)
    assert replace(_trade(), id=2).counterparty == PARTY


def test_frozen():
    with pytest.raises(FrozenInstanceError):
        MONEY.amount = 2.0  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(FrozenInstanceError):
        del MONEY.amount  # pyright: ignore[reportAttributeAccessIssue]


def test_shared_values_are_the_objects_given():
    trade = _trade(counterparty=FUND, broker=PARTY, parties=(PARTY, PARTY))
    assert trade.counterparty is FUND
    assert trade.broker is PARTY
    assert trade.parties[0] is PARTY and trade.parties[1] is PARTY
    assert replace(trade, id=2).broker is PARTY


def test_recursive_types():
    node = Node(1, Node(2), (Node(3), Node(4, children=(Node(5),))))
    assert node.next == Node(2)
    assert node.next is not None and node.next.next is None
    assert [child.value for child in node.children] == [3, 4]
    assert node.children[1].children == (Node(5),)

    expr = Expr("+", Leaf(1.0), Expr("-", Leaf(2.0, Wrapper(Expr("x", Leaf(0.0))))))
    assert type(expr.left) is Leaf and type(expr.right) is Expr
    assert expr.right.left.parent.expr.op == "x"  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess]
    assert expr == copy.deepcopy(expr)


def test_subclass():
    assert isinstance(FUND, Party)
    assert (FUND.name, FUND.lei, FUND.rating, FUND.manager) == ("Fund", None, 0, "Ann")
    assert FUND != Party("Fund")


def test_copy_and_pickle():
    trade = _trade(broker=PARTY, limits={Currency.USD: 1.0})
    for copied in (
        copy.copy(trade),
        copy.deepcopy(trade),
        pickle.loads(pickle.dumps(trade)),
    ):
        assert copied == trade
        assert copied is not trade

    # As a plain dataclass pickles, so a pickle made in one mode loads in any
    _, args, state = MONEY.__reduce_ex__(pickle.HIGHEST_PROTOCOL)[:3]
    assert args == (Money,)
    assert state == {"amount": 1.5, "currency": Currency.EUR}


def test_undecorated_subclass():
    @dataclass(frozen=True)
    class Undecorated(Party):
        extra: int = 0

    if BOUND:
        with pytest.raises(TypeError, match="must be decorated with @native"):
            Undecorated("x")
    else:
        assert Undecorated("x").extra == 0


@bound_only
def test_fields_are_stored_natively():
    assert list(vars(MONEY)) == ["__native__"]
    handle = vars(MONEY)["__native__"]
    assert type(handle).__module__ == "schemas._common_native"

    # Without copying it
    same = _any(Money)._from_native(handle)
    assert same == MONEY and vars(same)["__native__"] is handle
    with pytest.raises(TypeError, match="is not the native object of a Party"):
        _any(Party)._from_native(handle)


@bound_only
@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: _any(Money)("x"), "Money.amount cannot be 'x'"),
        (lambda: _any(Money)(1.0, 1), "Money.currency cannot be 1"),
        (lambda: _any(Money)(1.0, Side.BUY), "Money.currency cannot be <Side.BUY"),
        (lambda: _any(Party)("p", rating=2**31), "Party.rating cannot be 2147483648"),
        (lambda: _any(Party)(None), "Party.name cannot be None"),
        (lambda: _trade(flags=-1), "Trade.flags cannot be -1"),
        (lambda: _trade(id=1.5), "Trade.id cannot be 1.5"),
        (lambda: _trade(instrument=MONEY), "Trade.instrument cannot be Money"),
        (lambda: _trade(price=FUND), "Trade.price cannot be Fund"),
        (lambda: _trade(counterparty=None), "Trade.counterparty cannot be None"),
        (lambda: _trade(broker=FUND), "Trade.broker cannot be Fund"),
        (lambda: _trade(tags=["a"]), "Trade.tags cannot be \\['a'\\]"),
        (lambda: _trade(tags=("a", 1)), "Trade.tags cannot be"),
        (lambda: _trade(labels={"a"}), "Trade.labels cannot be {'a'}"),
        (lambda: _trade(limits=[]), "Trade.limits cannot be \\[\\]"),
        (lambda: _trade(limits={"USD": 1.0}), "Trade.limits cannot be"),
        (lambda: _trade(pair=(1,)), "Trade.pair cannot be \\(1,\\)"),
        (lambda: _trade(payload="text"), "Trade.payload cannot be 'text'"),
        (lambda: _trade(note=1.5), "Trade.note cannot be 1.5"),
        (
            lambda: _trade(trade_date=dt.datetime(2026, 1, 1, tzinfo=UTC)),
            "Trade.trade_date cannot be",
        ),
        (lambda: _trade(cut_off=dt.time(1, tzinfo=UTC)), "Trade.cut_off cannot be"),
        (lambda: _trade(executed=dt.datetime(2026, 1, 1)), "Trade.executed cannot be"),
        (lambda: _trade(settle_lag=1), "Trade.settle_lag cannot be 1"),
        (lambda: _trade(settle_lag=dt.timedelta.max), "Trade.settle_lag cannot be"),
        (lambda: _any(Kinds)(octets=(256,)), "Kinds.octets cannot be"),
        (lambda: _any(Kinds)(number=None), "Kinds.number cannot be None"),
        (lambda: _any(Kinds)(sides=frozenset({1})), "Kinds.sides cannot be"),
        (lambda: _any(Kinds)(container=frozenset()), "Kinds.container cannot be"),
        (lambda: _any(Node)(1, next=Leaf(1.0)), "Node.next cannot be Leaf"),
    ],
)
def test_invalid_values(make, message):
    with pytest.raises(TypeError, match=message):
        make()


def test_class_without_extension(make_module):
    module = make_module(
        """
        from dataclasses import dataclass
        from schemas.common import Money
        from dataclass_native import native

        @native
        @dataclass(frozen=True)
        class Unbuilt:
            money: Money
        """
    )

    if mode() is Mode.STRICT:
        with pytest.raises(
            NativeBindingError,
            match=f"there is no extension module _{module.__name__}_native",
        ):
            module.Unbuilt(MONEY)
    else:
        # A plain dataclass, though its field is bound
        unbuilt = module.Unbuilt(MONEY)
        assert vars(unbuilt) == {"money": MONEY}
        assert unbuilt == pickle.loads(pickle.dumps(unbuilt))


def test_local_class():
    @native
    @dataclass(frozen=True)
    class Local:
        x: int = 0

    if mode() is Mode.STRICT:
        with pytest.raises(
            NativeBindingError, match="only the classes at the top level"
        ):
            Local()
    else:
        assert vars(Local(1)) == {"x": 1}


def test_bond():
    assert Bond("XS1").coupon == pytest.approx(0.05)


def test_pydantic_dataclass():
    account = Account(Holder("h", PARTY), MONEY, limit=5, tags=("a",))
    assert ("__native__" in vars(account)) is BOUND
    assert (account.holder, account.balance, account.limit, account.ratio) == (
        Holder("h", PARTY),
        MONEY,
        5,
        0.5,
    )
    assert (account.tags, account.side, account.opened, account.parent) == (
        ("a",),
        Side.BUY,
        dt.date(2026, 1, 1),
        None,
    )
    assert account.holder.party is PARTY
    assert Account.ratio == 0.5

    # pydantic validates and converts what it is made from, making the objects it holds
    made: Any = _any(Account)(
        holder={"name": "h", "party": PARTY}, balance=MONEY, limit="5", tags=["a"]
    )
    assert made == account
    assert type(made.tags) is tuple and type(made.holder) is Holder
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Account(Holder("h"), MONEY, limit=-1)
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        Account(Holder("h"), MONEY, ratio=2)

    for copied in (
        replace(account, limit=1),
        copy.deepcopy(account),
        pickle.loads(pickle.dumps(account)),
    ):
        assert replace(copied, limit=5) == account
        assert ("__native__" in vars(copied)) is BOUND


def test_instance_made_by_pydantic():
    parent = {"holder": {"name": "x"}, "balance": {"amount": 2.0}}
    values = {
        "holder": {"name": "h", "party": {"name": "p"}},
        "balance": {"amount": 1.5},
        "parent": parent,
    }
    account = TypeAdapter(Account).validate_python(values)

    # pydantic sets its fields, without __init__: they are stored natively once one is read
    assert "__native__" not in vars(account)
    assert account == Account(
        Holder("h", Party("p")), Money(1.5), parent=Account(Holder("x"), Money(2.0))
    )
    assert ("__native__" in vars(account)) is BOUND
    assert (
        account.parent is not None and ("__native__" in vars(account.parent)) is BOUND
    )


@bound_only
def test_pydantic_dataclass_invalid_natively():
    # Valid to pydantic, which does not know the size of the int
    with pytest.raises(TypeError, match="Holder.rank cannot be 256"):
        Holder("h", rank=256)
    made = TypeAdapter(Holder).validate_python({"name": "h", "rank": 256})
    with pytest.raises(TypeError, match="Holder.rank cannot be 256"):
        _ = made.name
    assert vars(made) == {"name": "h", "party": None, "rank": 256}


def test_pydantic_model():
    class Model(BaseModel):
        x: int = 0

    with pytest.raises(
        TypeError,
        match="pydantic models are not supported: use pydantic.dataclasses.dataclass",
    ):
        _any(native)(Model)


def test_native_function():
    assert functions.implementation() == ("native" if BOUND else "python")

    amounts = (MONEY, Money(2.0, Currency.EUR), Money(4.0))
    assert functions.total(amounts) == Money(4.0)
    assert functions.total(amounts, Currency.EUR) == Money(3.5, Currency.EUR)
    assert functions.total(currency=Currency.GBP, amounts=amounts) == Money(
        0.0, Currency.GBP
    )
    assert functions.total.__doc__ == "The sum of the amounts in the currency"

    assert functions.renamed(PARTY) == replace(PARTY, name="?")
    assert functions.renamed(PARTY, "New").name == "New" and PARTY.name == "Acme"
    assert functions.same(PARTY) is PARTY
    assert functions.later(dt.date(2026, 12, 31), 1) == dt.date(2027, 1, 1)
    assert functions.nothing(1) is None


@pytest.mark.parametrize(
    ("function", "missing", "result"),
    [
        (functions.python_only, "schemas._functions has no python_only", 2),
        (functions.no_extension, "there is no extension module schemas._absent", 2),
    ],
)
def test_native_function_without_native_implementation(function, missing, result):
    if mode() is Mode.STRICT:
        name = f"schemas.functions.{function.__name__}"
        with pytest.raises(
            NativeBindingError,
            match=f"No native function is available for {name}: {missing}. Build",
        ):
            function(1)
    else:
        assert function(1) == result


@bound_only
def test_native_function_rejects():
    with pytest.raises(
        TypeError, match=r"schemas.functions.total: amounts cannot be \[\]"
    ):
        functions.total(_any([]))
    with pytest.raises(TypeError, match="total: currency cannot be 1"):
        functions.total((), _any(1))
    # Exactly a Party, as a field must be
    with pytest.raises(TypeError, match="renamed: party cannot be Fund"):
        functions.renamed(FUND)
    with pytest.raises(TypeError, match="too many positional arguments"):
        _any(functions.nothing)(1, 2)
    # By nanobind
    with pytest.raises(TypeError, match="incompatible function arguments"):
        functions.nothing(_any("1"))
    with pytest.raises(
        TypeError, match=r"schemas.functions.unannotated: value must be annotated"
    ):
        functions.unannotated(1)
