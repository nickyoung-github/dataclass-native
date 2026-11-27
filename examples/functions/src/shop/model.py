import datetime as dt
from dataclasses import dataclass
from enum import Enum, auto

from dataclass_native import Int32, Shared, native


class Status(Enum):
    PENDING = auto()
    SHIPPED = auto()
    DELIVERED = auto()


@native
@dataclass(frozen=True)
class Address:
    street: str
    city: str
    postcode: str
    country: str = "GB"


@native
@dataclass(frozen=True)
class Customer:
    id: int
    name: str
    address: Address
    email: str | None = None


@native
@dataclass(frozen=True)
class Line:
    sku: str
    quantity: Int32
    unit_price: float


@native
@dataclass(frozen=True)
class Order:
    id: int
    customer: Shared[Customer]  # Shared by a customer's orders, not copied into each
    lines: tuple[Line, ...]
    placed: dt.date
    status: Status = Status.PENDING
    ship_to: Address | None = None  # The customer's address, if None
