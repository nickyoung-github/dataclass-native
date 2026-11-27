import datetime as dt

from dataclass_native import mode
from shop.model import Address, Customer, Line, Order
from shop.pricing import ship, total

alice = Customer(1, "Alice", Address("1 High St", "Leeds", "LS1 1AA"))
order = Order(
    id=100,
    customer=alice,
    lines=(Line("widget", 2, 9.99), Line("gadget", 1, 24.50)),
    placed=dt.date(2026, 11, 27),
)

print(f"Mode: {mode()}")
print(f"Total: {total(order):.2f}")

shipped = ship(order)
print(f"Shipped to: {shipped.ship_to}")
print(f"Still sharing the customer: {shipped.customer is alice}")
print(f"Or to: {ship(order, Address('2 Low Rd', 'York', 'YO1 2BB')).ship_to}")
print(f"The order itself is unchanged: {order.status}")
