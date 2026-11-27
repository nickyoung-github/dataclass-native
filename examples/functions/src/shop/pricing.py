"""Functions of orders, implemented in C++ by cpp/pricing.hpp: their bodies are what runs in pure mode"""

from dataclasses import replace

from dataclass_native import native_function

from .model import Address, Order, Status


@native_function("._pricing")
def total(order: Order) -> float:
    return sum(line.quantity * line.unit_price for line in order.lines)


@native_function("._pricing")
def ship(order: Order, to: Address | None = None) -> Order:
    """The order, shipped to the address given, or else to its customer's"""
    return replace(order, status=Status.SHIPPED, ship_to=to or order.customer.address)
