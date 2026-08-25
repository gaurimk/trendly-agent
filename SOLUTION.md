# Solution Note

## Architecture

A single FastAPI process holds four layers, each with one job:

1. **Data layer** (`data_store.py`, `policy_store.py`) — read-only access to the fixed
   dataset and the policy document. `orders.json` is loaded once at startup and never
   mutated, per the assignment's instructions. The policy document is parsed into
   addressable clauses (`1.5`, `2.3`, etc.) by regex, not embeddings — the document is
   ~1,500 words and static, so keyword-overlap scoring over ~25 clauses is both accurate
   enough and trivially auditable (every retrieval can be traced to exact clause text).

2. **Rules layer** (`rules.py`) — deterministic Python functions that compute return
   eligibility, refund destination/timeline, and delay status directly from policy's
   stated thresholds (30-day window, non-returnable categories, ₹250 delay credit,
   etc.). This is the layer doing the actual "decide return/exchange eligibility by
   combining order data with policy rules" work the brief asks for — deliberately kept
   out of the LLM's hands (see PROMPTS.md for the reasoning).

3. **Tool layer** (`tools.py`) — the model-facing interface. Every tool is a plain
   function with a JSON schema; `identify_customer` is the only way a session acquires
   a `customer_id`, and no other tool schema exposes `customer_id` as a model-settable
   argument, so there is no cross-customer leakage path even under adversarial prompting.

4. **Orchestration layer** (`agent.py`) — a bounded ReAct loop (max 6 tool-call
   iterations per turn) against Groq's OpenAI-compatible function calling. In-memory
   session store carries conversation history and the resolved `customer_id` across
   turns. If the loop somehow exceeds its iteration budget, it fails closed into an
   automatic escalation rather than looping or guessing.

## Key trade-offs

- **Deterministic rules vs. LLM reasoning.** Chose deterministic Python for anything
  with a numeric or yes/no answer (dates, categories, amounts), leaving the LLM to
  handle language understanding, tool selection, and phrasing. This trades some
  flexibility (a genuinely novel policy question the rules engine wasn't written for
  falls through to `search_policy`'s "not found" path) for near-zero hallucination risk
  on the cases that matter most to get right.
- **No vector DB for policy retrieval.** Keyword-overlap over ~25 clauses is adequate
  here and has zero infra cost or embedding-API dependency (important under the
  free-tier-only constraint). This would need to change if Trendly's real policy corpus
  spans multiple documents or updates frequently.
- **In-memory session state, not Redis/DB.** Appropriate for this assignment's scope
  and single-process deployment; would not survive a process restart or multi-instance
  scaling in production.
- **Groq free tier for inference.** No cost, no card, full model access — but rate-
  limited (order of 30 requests/minute, ~1,000 requests/day per model as of mid-2026),
  which is fine for a demo and scripted evaluation but would need the paid Developer
  tier or a different provider for anything resembling Trendly's actual 2,000 chats/day.
- **Simulated clock.** `orders.json`'s own internal notes are only consistent at a
  specific fixed date (2026-07-29). Rather than let real wall-clock time silently
  desync the demo from the dataset's documented edge cases, "today" is an explicit,
  overridable simulated value. This is a demo-appropriate shortcut, not a production
  pattern (see discovery question #1).

## Known limitations

- **No real authentication.** `identify_customer` matches on email/phone alone, with no
  password, OTP, or session token — appropriate for a take-home demo, not for production.
- **Business-day math is naive.** `business_days_between` counts Mon–Fri with no public
  holiday calendar, so delay calculations near a holiday would be slightly off.
- **Escalation queue is in-memory and single-process.** Tickets vanish on restart; there
  is no actual human-agent-facing interface, just a debug JSON endpoint.
- **Free-tier LLM latency/availability isn't guaranteed** — no fallback provider is wired
  in if Groq rate-limits or errors mid-conversation; the loop will surface a 500 rather
  than degrade gracefully.
- **Policy retrieval is keyword-based**, so a policy question phrased with none of the
  document's vocabulary (e.g., a slang term for "returning" an item) could under-match.

## Five discovery questions for Trendly's ops team

1. What is the actual authoritative "current time" source in production, and how should
   date-dependent logic (return windows, delay thresholds) be recalculated if an order's
   timestamps are backdated or corrected after the fact?
2. What does real identity verification look like today (app login, OTP, order-lookup by
   phone at a call center), and what's the minimum viable version the agent should
   require before it can discuss order details?
3. Is there a public holiday calendar or region-specific business-day definition that
   should feed into delay/dispatch calculations, given Trendly ships to multiple cities?
4. What does the actual human-agent handoff look like operationally — a ticketing system
   (Zendesk, Freshdesk, an internal tool) the escalation payload needs to match, and what
   SLA or priority routing should different escalation reasons map to?
5. At real volume (2,000 chats/day), what's the expected peak concurrent load, and does
   Trendly have a preferred fallback LLM provider or budget for a paid tier, given the
   free-tier rate limits used in this build?
