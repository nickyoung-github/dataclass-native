import datetime as dt
from dataclasses import replace

from dataclass_native import mode
from shop.model import Address, Customer, Line, Order, Status

alice = Customer(1, "Alice", Address("1 High St", "Leeds", "LS1 1AA"))
order = Order(
    id=100,
    customer=alice,
    lines=(Line("widget", 2, 9.99), Line("gadget", 1, 24.50)),
    placed=dt.date(2026, 11, 27),
)

print(f"Mode: {mode()}")
print(order)
print(f"Stored in: {list(vars(order))}")
print(f"The customer is shared, not copied: {order.customer is alice}")
print(f"Shipped: {replace(order, status=Status.SHIPPED).status}")

try:
    Line("widget", "two", 9.99)  # pyright: ignore[reportArgumentType]
    print("A plain dataclass does not check its fields")
except TypeError as e:
    print(f"Rejected: {e}")
