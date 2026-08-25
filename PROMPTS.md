# Prompt Engineering Notes

This documents the reasoning behind the system prompt and tool descriptions in
`app/agent.py` / `app/tools.py` — what each instruction is there to prevent, and why
the split between "what the LLM decides" and "what code decides" is drawn where it is.

## Core design decision: the model narrates, code decides

The single biggest prompt-engineering risk in this assignment is an LLM asked to
"decide if a return is valid" from raw order JSON + a policy markdown file — that is
exactly the setup where a plausible-sounding but wrong eligibility call slips through.
So the prompt never asks the model to compute eligibility, refund amounts, or delay
status itself. It asks the model to **call the tool that computes it** and relay the
result. This shows up directly in the system prompt:

> "For return/exchange eligibility, always call check_return_eligibility rather than
> judging it yourself from the order data."

and

> "Never answer from memory about a specific order or a specific policy number."

The prompt's job is to make tool-calling the path of least resistance, not to make the
model a better arithmetician.

## Identification before data access

Early design question: should the API just accept a `customer_id` in the request body?
That's simpler, but it means a prompt-injected instruction (or a customer just typing
"pretend I'm C-102") could plausibly get the model to pass a different ID to `get_order`.
Instead, `customer_id` is **not a parameter of any tool schema the model controls** —
`identify_customer(email, phone)` resolves it server-side, `agent.py`'s dispatcher binds
it to the session, and every other tool has it injected, never model-supplied (see
`NEEDS_AUTH` in `agent.py`). The prompt reinforces this but isn't the actual mechanism:

> "Before touching any order, you must know who you're talking to... Do not ask for a
> password or OTP."

The "no OTP" line exists because early testing (mentally walking through likely model
behavior, given this is a demo with no real auth backend) suggested a helpful model
might try to invent a verification step that doesn't exist in this system and stall the
conversation — the instruction heads that off explicitly.

## Escalation triggers are enumerated, not left to judgment

Rather than "escalate when appropriate" (too vague — a model under-escalates when it's
confident and over-escalates when it isn't, neither of which is useful), the prompt
lists the exact triggers pulled directly from policy:

> "Call create_escalation for: lost-in-transit parcels, damaged/wrong items, a second
> exchange on the same item, cash-on-delivery refunds..., non-serviceable pincode
> reimbursements, or anything this policy document doesn't cover."

This maps 1:1 to policy clauses 1.6, 6.1–6.2, 4.4, 3.3, and 5.2, plus the catch-all in
Section 7 ("invent policy where this document is silent... it must say it does not know").

## Tone instruction placed deliberately, not as an afterthought

`orders.json` flags TR-4525 with: *"Customer is likely upset; a good agent acknowledges
the delay before quoting policy."* This became a standalone rule rather than folding it
into a generic "be nice" instruction, because generic politeness framing tends to get
drowned out by the more numerous hard-rule instructions around it:

> "If a customer seems upset about a real delay or problem, acknowledge it briefly and
> genuinely before quoting policy - don't lead with a clause number to someone who's
> frustrated."

## Hard rules are stated as absolutes, separated from soft guidance

The system prompt has a distinct `HARD RULES` block (discounts, bank details,
cross-customer data, honesty about not knowing) separated from the `HOW YOU WORK`
tool-usage guidance. This separation matters because it lets the hard rules stay short,
unconditional, and unmissable — bank-detail collection, for instance, is never
"unless the customer really wants to" — while the tool-usage section can stay
descriptive and example-heavy without diluting the non-negotiables.

## Tool descriptions carry policy discipline, not just function signatures

Rather than terse descriptions ("searches policy"), each tool's description repeats the
grounding discipline at the point of use, since that's more reliable than relying on the
system prompt alone to be remembered several turns into a conversation:

> `search_policy`: "Always call this before answering any policy question - never
> answer shipping/returns/refund policy from memory."
>
> `check_return_eligibility`: "Always call this before telling a customer a return is
> or isn't allowed - never decide eligibility yourself."

This redundancy (system prompt + tool description both saying "don't decide this
yourself") is intentional, not sloppy duplication — it's cheap insurance against the
instruction being under-weighted in one location.

## What would change with more iteration time

- A/B testing the exact phrasing of the "acknowledge before quoting policy" rule against
  a larger set of synthetic frustrated-customer messages, to check it generalizes beyond
  the one documented case (TR-4525).
- Testing whether `temperature=0.2` (chosen for consistency in eligibility phrasing) is
  too flat for the empathy-required cases, versus a higher temperature specifically for
  the final response-generation step once tool results are in hand.
- Explicit few-shot examples in the system prompt for the "policy doesn't cover this"
  refusal pattern, since zero-shot instruction-following on *absence* of information is
  generally weaker than on presence of information.
