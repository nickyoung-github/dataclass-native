from dataclasses import dataclass

from dataclass_native import native

from ..common import Money


@native
@dataclass(frozen=True)
class Order:
    # C++ keywords, which get a trailing underscore
    default: int = 0
    new: bool = False
    total: Money = Money(0.0)
