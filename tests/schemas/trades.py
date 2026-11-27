import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field

from dataclass_native import DiscriminatedUnion, Float32, Shared, UInt8, native

from .common import Currency, Fund, Money, Party, Side


@native
@dataclass(frozen=True)
class Cash:
    money: Money


@native
@dataclass(frozen=True)
class Bond:
    isin: str
    coupon: Float32 = 0.05


@native
@dataclass(frozen=True)
class Trade:
    id: int
    side: Side
    instrument: DiscriminatedUnion[Cash, Bond]
    counterparty: Shared[Party | Fund]
    price: Money = Money(100.0, Currency.EUR)
    quantity: float = 1
    flags: UInt8 = 255
    tags: tuple[str, ...] = ("a\nb", 'q"uote', "café")
    labels: frozenset[str] = frozenset({"x", "y"})
    limits: Mapping[Currency, float] = field(default_factory=dict)
    pair: tuple[int, str] = (1, "one")
    payload: bytes = b"\x00\x01"
    note: str | int | None = "n/a"
    trade_date: dt.date = dt.date(2026, 11, 27)
    cut_off: dt.time = dt.time(16, 30)
    executed: dt.datetime = dt.datetime(2026, 11, 27, 12, 0, tzinfo=dt.UTC)
    settle_lag: dt.timedelta = dt.timedelta(days=2)
    broker: Shared[Party] | None = None
    parties: tuple[Shared[Party], ...] = ()
