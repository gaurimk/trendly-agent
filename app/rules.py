"""
Deterministic policy application.

Design decision (documented in SOLUTION.md): eligibility/refund/delay math is
computed in plain Python against trendly_policy.md's stated rules, and only the
*explanation* of the outcome is left to the LLM. This is deliberate - an LLM
asked to "decide if this is a valid return" from raw JSON + a markdown file is
exactly where hallucinated eligibility creeps in. Numbers and yes/no decisions
come from code; the model's job is to phrase the code's decision helpfully and
cite the clause number the code used.

SIMULATED "TODAY":
orders.json is a static, non-relative snapshot (e.g. TR-4525's own note says it
is "14 days past expected delivery" against expected_delivery=2026-07-15, which
is only true if "today" is 2026-07-29; TR-4523's "well outside the 30-day
window" and TR-4527/TR-4528's "within window" notes are likewise only mutually
consistent at that date). Rather than silently drift out of sync with the fixed
dataset as real wall-clock time passes, the agent treats "today" as a
configurable simulated clock, defaulted to 2026-07-29 to match the dataset's
own internal notes. See SOLUTION.md discovery question #1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

NON_RETURNABLE_CATEGORIES = {"innerwear", "jewellery", "beauty", "fragrance", "face_masks", "gift_cards"}

REFUND_DESTINATION = {
    "credit_card": ("original card", "5-7 business days"),
    "prepaid_card": ("original card", "5-7 business days"),
    "upi": ("original UPI ID", "3-5 business days"),
    "cash_on_delivery": ("bank transfer or store credit (collected by a human agent over a secure link)", "7-10 business days"),
    "store_credit": ("store credit", "immediate"),
}

RETURN_WINDOW_DAYS = 30
DELAY_THRESHOLD_BUSINESS_DAYS = 3
DELAY_STORE_CREDIT_INR = 250
FOOTWEAR_NO_BOX_DEDUCTION_INR = 300
FREE_SHIPPING_THRESHOLD_INR = 1499
STANDARD_SHIPPING_FEE_INR = 99


def simulated_today() -> date:
    override = os.environ.get("SIMULATED_TODAY")
    if override:
        return datetime.strptime(override, "%Y-%m-%d").date()
    return date(2026, 7, 29)  # see module docstring


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def business_days_between(start: date, end: date) -> int:
    """Simple Mon-Fri business-day count, no holiday calendar. Documented
    limitation - see SOLUTION.md discovery question #3."""
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


@dataclass
class ItemEligibility:
    sku: str
    name: str
    category: str
    eligible_for_return: bool
    eligible_for_size_exchange: bool
    reason: str
    policy_clause: str
    notes: list = field(default_factory=list)


@dataclass
class OrderEligibility:
    order_id: str
    order_level_block: Optional[str]
    order_level_reason: Optional[str]
    policy_clause: Optional[str]
    items: list
    is_escalation: bool = False


def evaluate_order_eligibility(order: dict) -> OrderEligibility:
    status = order["status"]

    if status == "cancelled":
        return OrderEligibility(
            order_id=order["order_id"],
            order_level_block="cancelled",
            order_level_reason=(
                "This order was already cancelled and refunded on "
                f"{order.get('cancelled_at', 'an earlier date')}. A return cannot be "
                "raised against a cancelled order."
            ),
            policy_clause="2.6",
            items=[],
        )

    if status == "lost_in_transit":
        return OrderEligibility(
            order_id=order["order_id"],
            order_level_block="lost_in_transit",
            order_level_reason=(
                "The carrier has marked this parcel lost. This is handled as a "
                "lost-parcel claim, not a return, and must go to a human agent."
            ),
            policy_clause="1.6",
            items=[],
            is_escalation=True,
        )

    if order.get("delivered_at") is None:
        return OrderEligibility(
            order_id=order["order_id"],
            order_level_block="not_delivered",
            order_level_reason=(
                "This order has not been delivered yet, so a return can't be raised "
                "on it. Once it arrives, it can be reported as damaged/wrong within "
                "48 hours of delivery."
            ),
            policy_clause="2.1",
            items=[],
        )

    delivered = _parse_date(order["delivered_at"])
    today = simulated_today()
    days_since_delivery = (today - delivered).days
    within_window = 0 <= days_since_delivery <= RETURN_WINDOW_DAYS

    items = []
    for item in order["items"]:
        category = item["category"]
        final_sale = item.get("final_sale", False)

        if category in NON_RETURNABLE_CATEGORIES:
            items.append(ItemEligibility(
                sku=item["sku"], name=item["name"], category=category,
                eligible_for_return=False, eligible_for_size_exchange=False,
                reason=f"'{item['name']}' is in a non-returnable category ({category}) "
                       "for hygiene/safety reasons. This applies regardless of the "
                       "return window.",
                policy_clause="2.3",
            ))
            continue

        if final_sale:
            items.append(ItemEligibility(
                sku=item["sku"], name=item["name"], category=category,
                eligible_for_return=within_window, eligible_for_size_exchange=within_window,
                reason=(
                    f"'{item['name']}' is marked final sale: size exchange only, "
                    "no refund or store credit."
                    if within_window else
                    f"'{item['name']}' is marked final sale and the 30-day window "
                    f"has passed ({days_since_delivery} days since delivery)."
                ),
                policy_clause="2.4",
                notes=["final_sale_exchange_only"] if within_window else [],
            ))
            continue

        if not within_window:
            items.append(ItemEligibility(
                sku=item["sku"], name=item["name"], category=category,
                eligible_for_return=False, eligible_for_size_exchange=False,
                reason=(
                    f"'{item['name']}' was delivered {days_since_delivery} days ago, "
                    f"which is past the {RETURN_WINDOW_DAYS}-day return window."
                ),
                policy_clause="2.1",
            ))
            continue

        notes = []
        if category == "footwear":
            notes.append("footwear_needs_original_box")

        items.append(ItemEligibility(
            sku=item["sku"], name=item["name"], category=category,
            eligible_for_return=True, eligible_for_size_exchange=True,
            reason=f"'{item['name']}' is within the {RETURN_WINDOW_DAYS}-day window "
                   "and in a returnable category.",
            policy_clause="2.1" if category != "footwear" else "2.5",
            notes=notes,
        ))

    return OrderEligibility(
        order_id=order["order_id"],
        order_level_block=None,
        order_level_reason=None,
        policy_clause=None,
        items=items,
    )


@dataclass
class RefundEstimate:
    item_total_inr: int
    shipping_refund_inr: int
    destination: str
    timeline: str
    notes: list = field(default_factory=list)


def estimate_refund(order: dict, skus: Optional[list] = None) -> RefundEstimate:
    payment_method = order["payment_method"]
    destination, timeline = REFUND_DESTINATION.get(
        payment_method, ("store credit", "7-10 business days")
    )
    items = order["items"]
    if skus:
        items = [i for i in items if i["sku"] in skus]

    item_total = sum(i["price"] * i["qty"] for i in items)

    notes = []
    is_full_order_return = skus is None or len(items) == len(order["items"])
    original_shipping_fee = 0 if order["total"] >= FREE_SHIPPING_THRESHOLD_INR else STANDARD_SHIPPING_FEE_INR
    shipping_refund = 0
    if original_shipping_fee and is_full_order_return:
        notes.append(
            f"The original ₹{original_shipping_fee} shipping fee is refunded only if this "
            "return is due to a Trendly error (wrong/damaged/defective item), not for a "
            "change-of-mind return."
        )
    if not is_full_order_return:
        notes.append("Only the returned item(s) are refunded; free-shipping eligibility on "
                     "the rest of the order is not recalculated.")
    if payment_method == "cash_on_delivery":
        notes.append("Refund requires bank details, which a human agent collects over a "
                      "secure link - never in chat.")

    return RefundEstimate(
        item_total_inr=item_total,
        shipping_refund_inr=shipping_refund,
        destination=destination,
        timeline=timeline,
        notes=notes,
    )


@dataclass
class DelayStatus:
    is_delayed: bool
    business_days_late: int
    qualifies_for_credit: bool
    credit_amount_inr: int


def evaluate_delay(order: dict) -> Optional[DelayStatus]:
    if order["status"] == "cancelled" or not order.get("expected_delivery"):
        return None
    expected = _parse_date(order["expected_delivery"])
    today = simulated_today()
    delivered = _parse_date(order.get("delivered_at"))
    reference_end = delivered or today
    late_business_days = business_days_between(expected, reference_end) if reference_end > expected else 0
    is_delayed = late_business_days > DELAY_THRESHOLD_BUSINESS_DAYS or order["status"] == "delayed"
    return DelayStatus(
        is_delayed=is_delayed,
        business_days_late=late_business_days,
        qualifies_for_credit=is_delayed,
        credit_amount_inr=DELAY_STORE_CREDIT_INR if is_delayed else 0,
    )
