"""pydantic dataclasses, which validate what they are made from"""

import datetime as dt
from typing import Annotated

from pydantic import Field
from pydantic.dataclasses import dataclass

from dataclass_native import Shared, UInt8, native

from .common import Money, Party, Side


@native
@dataclass(frozen=True)
class Holder:
    name: str
    party: Shared[Party] | None = None
    rank: UInt8 = 0


@native
@dataclass(frozen=True)
class Account:
    holder: Holder
    balance: Money
    limit: Annotated[int, Field(ge=0)] = 0
    ratio: float = Field(default=0.5, ge=0, le=1)
    tags: tuple[str, ...] = ()
    side: Side = Side.BUY
    opened: dt.date = dt.date(2026, 1, 1)
    parent: "Account | None" = None
