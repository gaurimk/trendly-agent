# Trendly Support Assistant

A tool-calling agent that handles Trendly's repetitive support volume — order status,
returns/exchange eligibility, refunds, and shipping/policy questions — grounded strictly
in `data/trendly_policy.md` and `data/orders.json`, and escalates cleanly to a human when
it should.

Built for the Yellow.ai FDE (Intern) screening assignment.

---

## Quickstart

```bash
git clone <this-repo-url> trendly-agent
cd trendly-agent
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set GROQ_API_KEY (free, no card, from https://console.groq.com)

uvicorn app.main:app --reload
# open http://localhost:8000
```

No frontend build step — `static/index.html` is a single vanilla-JS file served
directly by FastAPI.

### Try it

- "Where's my order TR-4525?" (delayed order — the agent should acknowledge the delay
  before quoting policy, and mention the ₹250 delay credit)
- "Can I return TR-4527?" (jewellery — refused on category grounds, not date grounds)
- "I want to return TR-4526" (lost in transit — escalates to a human, doesn't process
  as a return)
- Give an email that isn't on the account, or try to ask about someone else's order —
  the agent should refuse without confirming whether the order exists at all.

Every reply also has a debug trace of tool calls; hit `GET /debug/escalations` to see
tickets the agent has raised in-session.

---

## Architecture

```
app/
  data_store.py   Loads orders.json as-is (never mutated). Every lookup requires
                   a customer_id and raises rather than returning a near-match —
                   this is the actual anti-leakage boundary, not a prompt instruction.
  policy_store.py  Parses trendly_policy.md into addressable clauses (e.g. "2.3")
                   with simple keyword-overlap search. No vector DB — the source
                   document is ~1,500 words and static, so that would be over-
                   engineering (see SOLUTION.md).
  rules.py         Deterministic policy application: eligibility, refund estimate,
                   delay status. Plain Python, not an LLM judgment call — the model's
                   job is to phrase these outputs, not compute them.
  tools.py         The functions + JSON schemas the model can call. Wraps data_store/
                   policy_store/rules behind a tool-call interface.
  agent.py         The ReAct-style tool-calling loop against Groq's chat-completions
                   API (OpenAI-compatible function calling), plus in-memory session
                   state so multi-turn context (e.g. "who is this customer") persists.
  main.py          FastAPI app: POST /chat, GET /debug/escalations, GET /health,
                   and the static chat UI.
static/index.html  Minimal chat widget (no framework, no build step).
tests/test_tools.py Offline unit tests against the deterministic core — zero
                    dependency on groq/fastapi, so they run without an API key.
```

**Model:** `openai/gpt-oss-120b` on Groq's free API tier (no card required, rate-
limited but sufficient for a demo/evaluation — see SOLUTION.md for the production
caveat). Configurable via `GROQ_MODEL`.

**Simulated clock:** `orders.json` is a static snapshot whose own internal notes (e.g.
"14 days past expected delivery," "well outside the 30-day window") are only mutually
consistent as of **2026-07-29**. Rather than let return-window/delay math silently drift
out of sync with the fixed dataset as real time passes, the app treats "today" as
`SIMULATED_TODAY` (default `2026-07-29`, overridable via env var).

---

## Testing

```bash
python -m unittest tests.test_tools -v
```

29 tests, no API key required — covers all 10 fixed orders against their documented
edge cases (delayed, lost-in-transit, jewellery, final-sale, cancelled, partial
shipment, clean happy path), the cross-customer authorization boundary, the
policy-grounding contract, and the multi-turn session-state layer itself
(`app.agent.Session` / `_dispatch_tool_call`): identify-once-then-reuse across
calls, fail-closed before identification, and — the one that matters most —
a forged `customer_id` in a tool-call argument being silently overridden by
the session's real identity rather than trusted. That last guarantee is
tested directly here rather than only asserted in the system prompt.
See `tests/test_tools.py` for the full list; see `SOLUTION.md` for what this
suite does *not* cover (a live multi-turn conversation against the actual
Groq model, which needs a live key and isn't practical to assert
deterministically against live LLM wording).

---

## Deployment

- **Render:** `render.yaml` is included — connect the repo, set `GROQ_API_KEY` as a
  secret env var, deploy. Free tier.
- **Any host with one command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

Note: free-tier Render/Railway instances sleep after inactivity; the first request
after a cold start can take ~30 seconds. Documented as a known limitation, not hidden.

---

## AI-usage note

This project was built with heavy use of Claude (Anthropic) as a pair-programmer:
the tool schemas, orchestration loop, rules engine, and test suite were drafted
through natural-language iteration with Claude, then reviewed, run, and corrected
line-by-line — every module was read in full, the deterministic core was exercised
against all 10 fixed orders via the test suite above, and the design decisions
(auth-boundary-in-code rather than in-prompt, deterministic rules engine rather than
LLM-computed eligibility, section/clause-level policy retrieval) were deliberately
chosen and can be explained and modified live. `PROMPTS.md` documents the reasoning
behind the system prompt and tool descriptions specifically.
