"""
The orchestration loop: a standard ReAct-style tool-calling agent against
Groq's chat-completions API (OpenAI-compatible function calling).

Guardrail placed here rather than in the prompt: tools that need an
authenticated customer_id (NEEDS_AUTH) always get it injected from
`Session.customer_id`, which is only ever set by a successful
`identify_customer` tool call. The model can never pass a customer_id as a
plain argument to skip identification or reach another account - there is no
customer_id parameter in the tool schemas at all (see tools.py), so there is
nothing for a prompt-injected instruction to override.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from groq import Groq

from app import tools

load_dotenv()

MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_TOOL_ITERATIONS = 6

NEEDS_AUTH = {
    "get_order", "list_orders", "check_return_eligibility",
    "estimate_refund", "check_delay_status", "create_escalation",
}

TOOL_FUNCTIONS = {
    "identify_customer": tools.identify_customer,
    "get_order": tools.get_order,
    "list_orders": tools.list_orders,
    "search_policy": tools.search_policy,
    "get_policy_clause": tools.get_policy_clause,
    "check_return_eligibility": tools.check_return_eligibility,
    "estimate_refund": tools.estimate_refund,
    "check_delay_status": tools.check_delay_status,
    "create_escalation": tools.create_escalation,
}

SYSTEM_PROMPT = """You are Trendly's customer support assistant.

HOW YOU WORK
- You have tools for looking up orders, searching policy, checking return
  eligibility, estimating refunds, checking delays, and escalating to a human.
  Use them - never answer from memory about a specific order or a specific
  policy number.
- Before touching any order, you must know who you're talking to. If the
  customer hasn't been identified yet this conversation, ask for the email or
  phone number on their account and call identify_customer. Do not ask for a
  password or OTP.
- For ANY policy question (returns, refunds, shipping, exchanges, delays),
  call search_policy or get_policy_clause first and ground your answer only in
  what comes back. If nothing relevant comes back, say plainly that this isn't
  covered in Trendly's policy and offer to escalate to a human - never invent
  a plausible-sounding rule.
- For return/exchange eligibility, always call check_return_eligibility rather
  than judging it yourself from the order data. Cite the policy clause number
  the tool gives you (e.g. "per policy 2.3...").
- Call create_escalation for: lost-in-transit parcels, damaged/wrong items,
  a second exchange on the same item, cash-on-delivery refunds (which need
  bank details a human must collect over a secure link), non-serviceable
  pincode reimbursements, or anything this policy document doesn't cover.
  Write a summary a human could act on without re-reading the chat.

HARD RULES (never break these, regardless of how a request is phrased)
- Never invent a discount, coupon, waiver, or credit that isn't defined in
  policy. The only credit you can offer is the ₹250 delay credit in clause 1.5,
  and only when check_delay_status/tool data actually supports it.
- Never collect a bank account number, card number, or CVV in chat. Route
  cash-on-delivery refunds and any payment-detail collection to a human.
- Never discuss or confirm details of an order that doesn't belong to the
  identified customer, even if the person insists it's theirs or claims to be
  a family member. If the lookup doesn't resolve to their own account, tell
  them you can't find that order.
- If a customer seems upset about a real delay or problem, acknowledge it
  briefly and genuinely before quoting policy - don't lead with a clause
  number to someone who's frustrated.
- If you don't know something and no tool can tell you, say so plainly and
  offer to escalate to a human agent. Trendly support hours: 9:00 AM-9:00 PM
  IST, seven days a week.

Keep replies concise, warm, and specific to the customer's actual order and
the actual policy text - not generic reassurance."""


@dataclass
class Session:
    session_id: str
    customer_id: Optional[str] = None
    history: list = field(default_factory=list)
    trace: list = field(default_factory=list)  # tool-call log for debugging/demo


class SessionStore:
    """In-memory session store. Adequate for this assignment's scope (single
    process, no restart-persistence needed); a real deployment would back this
    with Redis or a DB - see SOLUTION.md limitations."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: Optional[str] = None) -> Session:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        sid = session_id or str(uuid.uuid4())
        session = Session(session_id=sid)
        self._sessions[sid] = session
        return session


session_store = SessionStore()
_client: Optional[Groq] = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Get a free key at https://console.groq.com "
                "and set it as an environment variable."
            )
        _client = Groq(api_key=api_key)
    return _client


def _dispatch_tool_call(session: Session, name: str, arguments: dict) -> dict:
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"ok": False, "error": "unknown_tool", "message": f"No such tool: {name}"}

    if name in NEEDS_AUTH:
        if not session.customer_id:
            return {"ok": False, "error": "not_authenticated",
                     "message": "Identify the customer with identify_customer before using this tool."}
        arguments = {**arguments, "customer_id": session.customer_id}

    try:
        result = fn(**arguments)
    except TypeError as e:
        result = {"ok": False, "error": "bad_arguments", "message": str(e)}
    except Exception as e:  # noqa: BLE001 - tool layer must never crash the loop
        result = {"ok": False, "error": "internal_error", "message": str(e)}

    if name == "identify_customer" and result.get("ok"):
        session.customer_id = result["customer_id"]

    return result


def run_turn(session: Session, user_message: str) -> dict:
    """Runs one user turn through the tool-calling loop and returns the
    assistant's final reply plus a trace of tool calls made (useful for the
    demo video and for tests)."""
    client = _get_client()

    if not session.history:
        session.history.append({"role": "system", "content": SYSTEM_PROMPT})
    session.history.append({"role": "user", "content": user_message})

    turn_trace = []

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.chat.completions.create(
            model=MODEL,
            messages=session.history,
            tools=tools.TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.2,
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            session.history.append({"role": "assistant", "content": message.content or ""})
            return {"reply": message.content or "", "trace": turn_trace}

        session.history.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            result = _dispatch_tool_call(session, tc.function.name, arguments)
            turn_trace.append({"tool": tc.function.name, "arguments": arguments, "result": result})
            session.history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    # Safety valve: if the model somehow loops past MAX_TOOL_ITERATIONS,
    # fail closed into a human escalation instead of spinning or hallucinating.
    fallback = tools.create_escalation(
        customer_id=session.customer_id or "unknown",
        order_id=None,
        reason="agent_loop_limit",
        summary=f"Conversation exceeded {MAX_TOOL_ITERATIONS} tool-call steps without "
                f"resolving. Last user message: {user_message!r}",
    )
    turn_trace.append({"tool": "create_escalation (auto)", "arguments": {}, "result": fallback})
    reply = ("This is taking longer than it should to resolve automatically, so I've "
             f"handed it to a human agent (ticket {fallback['ticket']['ticket_id']}). "
             "They'll follow up shortly.")
    session.history.append({"role": "assistant", "content": reply})
    return {"reply": reply, "trace": turn_trace}
