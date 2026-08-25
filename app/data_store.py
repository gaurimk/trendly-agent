"""
Loads orders.json exactly as provided (never mutated, never re-ordered) and
exposes lookups that are safe by construction: a lookup for order X always
requires the caller's customer_id and refuses silently-wrong matches rather
than trusting the model to remember not to leak another customer's order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ORDERS_PATH = Path(__file__).resolve().parent.parent / "data" / "orders.json"


class OrderNotFoundError(Exception):
    pass


class CustomerMismatchError(Exception):
    """Raised when an order exists but does not belong to the requesting customer.
    Callers must treat this identically to 'not found' in anything shown to the
    user - the distinction exists only for internal logging/escalation, never
    for confirming to a user that a *different* customer's order ID is valid."""


@dataclass(frozen=True)
class DataStore:
    customers: dict
    orders: dict

    @classmethod
    def load(cls, path: Path = ORDERS_PATH) -> "DataStore":
        raw = json.loads(path.read_text(encoding="utf-8"))
        customers = {c["customer_id"]: c for c in raw["customers"]}
        orders = {o["order_id"]: o for o in raw["orders"]}
        return cls(customers=customers, orders=orders)

    def get_customer(self, customer_id: str) -> Optional[dict]:
        return self.customers.get(customer_id)

    def get_order_for_customer(self, order_id: str, customer_id: str) -> dict:
        """The only sanctioned way to read an order. Raises rather than returning
        None/partial data so callers can't accidentally forward a leaked record."""
        order = self.orders.get(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        if order["customer_id"] != customer_id:
            raise CustomerMismatchError(order_id)
        return order

    def orders_for_customer(self, customer_id: str) -> list[dict]:
        return [o for o in self.orders.values() if o["customer_id"] == customer_id]


data_store = DataStore.load()
