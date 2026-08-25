"""
Every function here is a tool the model can call. Two hard rules govern this
file, both enforced in code rather than by prompting:

1. No tool ever returns another customer's data. get_order requires the
   session's authenticated customer_id and raises rather than substituting a
   near-match.
2. No tool ever invents policy. search_policy only returns real clause text
   pulled from trendly_policy.md; if nothing matches, it says so.

Every tool returns a plain JSON-serializable dict so it can go straight into a
Groq tool-result message.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import asdict
from typing import Optional

from app.data_store import data_store, CustomerMismatchError, OrderNotFoundError
from app.policy_store import policy_store
from app import rules

_escalation_id_counter = itertools.count(1)
_ESCALATIONS: dict = {}  # in-memory "human queue" for this process


def identify_customer(email: Optional[str] = None, phone: Optional[str] = None) -> dict:
    """Resolve a customer_id from an email or phone number the customer
    provides themselves. This is the ONLY way a session becomes authenticated;
    every other tool requires the resulting customer_id to be bound server-side
    first (see agent.py dispatch) rather than trusting a customer_id the model
    might otherwise pass as a plain argument."""
    needle = (email or phone or "").strip().lower()
    if not needle:
        return {"ok": False, "error": "missing_identifier"}
    for customer in data_store.customers.values():
        if customer["email"].lower() == needle or customer["phone"].lower() == needle:
            return {"ok": True, "customer_id": customer["customer_id"], "name": customer["name"]}
    return {"ok": False, "error": "not_found",
            "message": "No account matches that email or phone number."}


def get_order(customer_id: str, order_id: str) -> dict:
    """Look up one order belonging to the authenticated customer."""
    try:
        order = data_store.get_order_for_customer(order_id, customer_id)
    except (OrderNotFoundError, CustomerMismatchError):
        return {"ok": False, "error": "not_found",
                "message": f"No order {order_id} was found on this account."}

    delay = rules.evaluate_delay(order)
    return {
        "ok": True,
        "order": order,
        "delay_status": asdict(delay) if delay else None,
        "simulated_today": rules.simulated_today().isoformat(),
    }


def list_orders(customer_id: str) -> dict:
    """List all orders for the authenticated customer (compact summary)."""
    orders = data_store.orders_for_customer(customer_id)
    return {
        "ok": True,
        "orders": [
            {"order_id": o["order_id"], "status": o["status"],
             "placed_at": o["placed_at"], "total": o["total"]}
            for o in orders
        ],
    }


def search_policy(query: str) -> dict:
    """Retrieve the most relevant policy clause(s) for a question. Returns
    found=False if nothing matches - callers must treat that as 'not covered
    by policy', never as license to guess."""
    hits = policy_store.search(query)
    if not hits:
        return {"ok": True, "found": False, "clauses": []}
    return {
        "ok": True,
        "found": True,
        "clauses": [
            {"number": c.number, "title": c.title, "section": c.section_title, "text": c.text}
            for c in hits
        ],
    }


def get_policy_clause(number: str) -> dict:
    """Retrieve one specific clause by number (e.g. '2.3') when already known."""
    clause = policy_store.get(number)
    if not clause:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "number": clause.number, "title": clause.title, "text": clause.text}


def check_return_eligibility(customer_id: str, order_id: str) -> dict:
    """Determine, per item, whether a return/exchange can be raised on this
    order. Deterministic policy computation, not a model guess - see rules.py."""
    try:
        order = data_store.get_order_for_customer(order_id, customer_id)
    except (OrderNotFoundError, CustomerMismatchError):
        return {"ok": False, "error": "not_found",
                "message": f"No order {order_id} was found on this account."}

    result = rules.evaluate_order_eligibility(order)
    return {
        "ok": True,
        "order_id": result.order_id,
        "order_level_block": result.order_level_block,
        "order_level_reason": result.order_level_reason,
        "policy_clause": result.policy_clause,
        "requires_human_escalation": result.is_escalation,
        "items": [asdict(i) for i in result.items],
    }


def estimate_refund(customer_id: str, order_id: str, skus: Optional[list] = None) -> dict:
    """Estimate refund amount, destination, and timeline. Call
    check_return_eligibility first - this tool only does the arithmetic."""
    try:
        order = data_store.get_order_for_customer(order_id, customer_id)
    except (OrderNotFoundError, CustomerMismatchError):
        return {"ok": False, "error": "not_found",
                "message": f"No order {order_id} was found on this account."}
    est = rules.estimate_refund(order, skus)
    return {"ok": True, **asdict(est)}


def check_delay_status(customer_id: str, order_id: str) -> dict:
    """Check whether an order is officially 'delayed' under policy 1.5 and
    whether it qualifies for the ₹250 store credit."""
    try:
        order = data_store.get_order_for_customer(order_id, customer_id)
    except (OrderNotFoundError, CustomerMismatchError):
        return {"ok": False, "error": "not_found",
                "message": f"No order {order_id} was found on this account."}
    status = rules.evaluate_delay(order)
    if status is None:
        return {"ok": True, "applicable": False}
    return {"ok": True, "applicable": True, **asdict(status)}


def create_escalation(customer_id: str, order_id: Optional[str], reason: str, summary: str) -> dict:
    """Hand off to a human agent. summary must be something a person can act
    on immediately: what happened, what the customer wants, and what policy
    clause (if any) governs it."""
    customer = data_store.get_customer(customer_id)
    ticket_id = f"ESC-{next(_escalation_id_counter):04d}-{uuid.uuid4().hex[:4].upper()}"
    ticket = {
        "ticket_id": ticket_id,
        "customer_id": customer_id,
        "customer_name": customer["name"] if customer else "unknown",
        "order_id": order_id,
        "reason": reason,
        "summary": summary,
    }
    _ESCALATIONS[ticket_id] = ticket
    return {"ok": True, "ticket": ticket,
            "message": "This has been escalated to a human agent (Trendly support hours: "
                       "9:00 AM-9:00 PM IST, 7 days a week)."}


def _get_escalations_for_testing() -> dict:
    """Test/debug helper only - never exposed to the model."""
    return dict(_ESCALATIONS)


# ---------------------------------------------------------------------------
# Tool schemas (Groq / OpenAI-style function calling)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "identify_customer",
            "description": "Verify who the customer is, using an email or phone number THEY provide in chat. Must succeed before any order-specific tool is used. Never ask for a password or OTP - this demo account model uses email/phone only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Look up a single order's full details for the authenticated customer.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "e.g. TR-4521"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_orders",
            "description": "List all orders on the authenticated customer's account, when they don't give a specific order ID.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_policy",
            "description": "Search trendly_policy.md for the clause(s) relevant to a policy question. Always call this before answering any policy question - never answer shipping/returns/refund policy from memory.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_policy_clause",
            "description": "Fetch one exact policy clause by number, e.g. '2.3'.",
            "parameters": {
                "type": "object",
                "properties": {"number": {"type": "string"}},
                "required": ["number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_return_eligibility",
            "description": "Decide whether a return/exchange is allowed on an order, per item, per policy. Always call this before telling a customer a return is or isn't allowed - never decide eligibility yourself.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_refund",
            "description": "Compute the refund amount, destination, and timeline for a return. Call check_return_eligibility first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "skus": {"type": "array", "items": {"type": "string"},
                             "description": "Specific item SKUs being returned; omit for a full-order return."},
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_delay_status",
            "description": "Check whether an order is delayed under policy and qualifies for the ₹250 delay store credit.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_escalation",
            "description": "Escalate to a human agent: lost parcels, damaged/wrong items, second exchange on the same item, cash-on-delivery bank-detail collection, non-serviceable-pincode reimbursement, or anything policy doesn't cover. Always include a summary a human could act on without re-reading the chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "reason": {"type": "string", "description": "Short machine-readable reason, e.g. 'lost_in_transit'"},
                    "summary": {"type": "string", "description": "Human-readable handoff summary."},
                },
                "required": ["reason", "summary"],
            },
        },
    },
]
