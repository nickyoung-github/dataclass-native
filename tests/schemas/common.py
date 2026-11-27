from dataclasses import dataclass
from enum import Enum, auto

from dataclass_native import Int32, native


class Currency(Enum):
    USD = auto()
    EUR = auto()
    GBP = auto()


class Side(Enum):
    # Not int-valued, so numbered by position
    BUY = "buy"
    SELL = "sell"


@native
@dataclass(frozen=True)
class Money:
    amount: float
    currency: Currency = Currency.USD


@native(tag="party.v1")
@dataclass(frozen=True)
class Party:
    name: str
    lei: str | None = None
    rating: Int32 = 0


@native
@dataclass(frozen=True)
class Fund(Party):
    manager: str = ""
