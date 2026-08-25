# Design Reference: Tools & Edge-Case Matrix

## Tool contracts

| Tool | Auth required | Purpose | Never does |
|---|---|---|---|
| `identify_customer(email?, phone?)` | — (this *is* auth) | Resolve a `customer_id` from a self-provided contact detail | Ask for password/OTP |
| `get_order(order_id)` | yes | Full details of one order owned by the session's customer | Return another customer's order |
| `list_orders()` | yes | Summary of all orders on the account | — |
| `search_policy(query)` | no | Keyword-matched policy clause(s) | Invent clause text |
| `get_policy_clause(number)` | no | Exact clause by number | — |
| `check_return_eligibility(order_id)` | yes | Deterministic per-item return/exchange verdict | Let the model override the verdict |
| `estimate_refund(order_id, skus?)` | yes | Refund amount, destination, timeline | Collect bank details |
| `check_delay_status(order_id)` | yes | Delay flag + ₹250 credit eligibility | — |
| `create_escalation(order_id?, reason, summary)` | yes | Human-actionable handoff ticket | Resolve the issue itself |

## Edge-case matrix (from `orders.json` `_note_for_designers` fields)

| Order | Scenario | Expected outcome | Policy clause | Covered by test |
|---|---|---|---|---|
| TR-4521 | In transit | Not eligible — not yet delivered | 2.1 | ✅ |
| TR-4522 | Mixed cart (apparel + socks) | Tee returnable, socks refused | 2.1 / 2.3 | ✅ |
| TR-4523 | Delivered 54+ days ago | Refused — window expired | 2.1 | ✅ |
| TR-4524 | Partially shipped | Not eligible — not yet delivered | 2.1 | ✅ |
| TR-4525 | 14 days delayed | Acknowledge delay, offer ₹250 credit | 1.5 | ✅ |
| TR-4526 | Lost in transit | Escalate — NOT a return | 1.6 | ✅ |
| TR-4527 | Jewellery, within window | Refused on category, not date | 2.3 | ✅ |
| TR-4528 | Final sale, within window | Exchange only, no refund | 2.4 | ✅ |
| TR-4529 | Already cancelled | Return request is nonsensical | 2.6 | ✅ |
| TR-4530 | Clean happy path | Standard return eligible | 2.1 | ✅ |

## Adversarial / safety cases

| Case | Expected behavior | Covered by test |
|---|---|---|
| Customer asks about another customer's order | Denied, indistinguishable from "not found" | ✅ |
| Unknown order ID | Denied | ✅ |
| Unknown email/phone at identify step | Fails closed, no account created | ✅ |
| Policy question with no matching clause | Reports not-found, never fabricates | ✅ |
| Cash-on-delivery refund | Flags human-agent requirement, never asks for bank details in chat | ✅ |
| Agent loop runs past iteration budget | Fails closed into automatic escalation | design-level (agent.py) |

All ten fixed orders and the listed adversarial cases are exercised in
`tests/test_tools.py` against the deterministic layer. Full multi-turn conversational
behavior (tone, escalation phrasing, actually calling the right tool in sequence)
requires a live Groq key and is validated manually per the demo video — see
`SOLUTION.md` for why this split exists.
