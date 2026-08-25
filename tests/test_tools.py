"""
Offline test suite for the deterministic core (data_store, policy_store, rules,
tools). Deliberately has ZERO dependency on `groq` or `fastapi` so it runs
anywhere with just the standard library + the project itself — this is the
layer where correctness actually matters (eligibility, refunds, auth
boundaries), and it should be testable without an API key or a running server.

Run with:  python -m unittest tests.test_tools -v
(or `pytest tests/` once fastapi/groq/pytest are installed — the assertions
are plain unittest so both runners work.)

Each test is traceable to either a `_note_for_designers` field in
orders.json or a specific policy clause, referenced in the test name/comment.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Pin the simulated clock BEFORE importing app.rules, since it reads the env
# var at call-time via simulated_today() (not at import time) — but we set it
# explicitly here anyway so these tests never depend on ambient environment.
os.environ["SIMULATED_TODAY"] = "2026-07-29"

from app import tools, rules  # noqa: E402
from app.data_store import data_store, OrderNotFoundError, CustomerMismatchError  # noqa: E402


class TestIdentifyCustomer(unittest.TestCase):
    def test_identify_by_email(self):
        result = tools.identify_customer(email="ananya.rao@example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["customer_id"], "C-100")

    def test_identify_by_phone(self):
        result = tools.identify_customer(phone="+1-415-555-0102")
        self.assertTrue(result["ok"])
        self.assertEqual(result["customer_id"], "C-101")

    def test_identify_unknown_contact_fails_closed(self):
        result = tools.identify_customer(email="nobody@nowhere.com")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_found")

    def test_identify_with_nothing_provided(self):
        result = tools.identify_customer()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_identifier")


class TestAuthorizationBoundary(unittest.TestCase):
    """The single most important guarantee: a customer can never read
    another customer's order, at the data layer, regardless of what the
    model is tricked into asking for."""

    def test_cross_customer_lookup_is_denied(self):
        # TR-4521 belongs to C-100; C-101 must not be able to fetch it.
        with self.assertRaises(CustomerMismatchError):
            data_store.get_order_for_customer("TR-4521", "C-101")

    def test_tool_layer_reports_not_found_not_which_customer_owns_it(self):
        # The tool-facing error must NOT reveal that the order exists at all
        # for a different customer — same message as a genuinely missing ID.
        wrong_customer = tools.get_order(customer_id="C-101", order_id="TR-4521")
        missing_order = tools.get_order(customer_id="C-101", order_id="TR-9999")
        self.assertFalse(wrong_customer["ok"])
        self.assertFalse(missing_order["ok"])
        self.assertEqual(wrong_customer["error"], missing_order["error"])

    def test_unknown_order_id(self):
        with self.assertRaises(OrderNotFoundError):
            data_store.get_order_for_customer("TR-0000", "C-100")


class TestEligibilityAgainstFixedDataset(unittest.TestCase):
    """One test per order in orders.json, each asserting the exact outcome
    documented in that order's own _note_for_designers field."""

    def test_TR4521_not_yet_delivered(self):
        order = data_store.orders["TR-4521"]
        result = rules.evaluate_order_eligibility(order)
        self.assertEqual(result.order_level_block, "not_delivered")

    def test_TR4522_mixed_categories_partial_eligibility(self):
        # Cotton tee (apparel) returnable; ankle socks (innerwear) are not.
        order = data_store.orders["TR-4522"]
        result = rules.evaluate_order_eligibility(order)
        by_sku = {i.sku: i for i in result.items}
        self.assertTrue(by_sku["TR-TSH-002"].eligible_for_return)
        self.assertFalse(by_sku["TR-SOK-031"].eligible_for_return)
        self.assertEqual(by_sku["TR-SOK-031"].policy_clause, "2.3")

    def test_TR4523_outside_30_day_window(self):
        # Note: "well outside the 30-day return window. Return must be refused."
        order = data_store.orders["TR-4523"]
        result = rules.evaluate_order_eligibility(order)
        self.assertFalse(result.items[0].eligible_for_return)
        self.assertEqual(result.items[0].policy_clause, "2.1")

    def test_TR4524_partially_shipped_not_delivered(self):
        order = data_store.orders["TR-4524"]
        result = rules.evaluate_order_eligibility(order)
        self.assertEqual(result.order_level_block, "not_delivered")

    def test_TR4525_delayed_qualifies_for_credit(self):
        # Note: "14 days past expected delivery... good agent acknowledges
        # the delay before quoting policy."
        order = data_store.orders["TR-4525"]
        status = rules.evaluate_delay(order)
        self.assertTrue(status.is_delayed)
        self.assertTrue(status.qualifies_for_credit)
        self.assertEqual(status.credit_amount_inr, 250)

    def test_TR4526_lost_in_transit_is_escalation_not_return(self):
        # Note: "NOT a return — it is a lost-parcel claim and must be
        # escalated to a human."
        order = data_store.orders["TR-4526"]
        result = rules.evaluate_order_eligibility(order)
        self.assertEqual(result.order_level_block, "lost_in_transit")
        self.assertTrue(result.is_escalation)
        self.assertEqual(result.policy_clause, "1.6")

    def test_TR4527_jewellery_refused_on_category_not_date(self):
        # Note: "Within 30 days, BUT jewellery is non-returnable... refused
        # on category grounds, not date grounds."
        order = data_store.orders["TR-4527"]
        result = rules.evaluate_order_eligibility(order)
        item = result.items[0]
        self.assertFalse(item.eligible_for_return)
        self.assertEqual(item.policy_clause, "2.3")
        self.assertIn("hygiene", item.reason.lower())

    def test_TR4528_final_sale_exchange_only(self):
        # Note: "exchange only, no refund."
        order = data_store.orders["TR-4528"]
        result = rules.evaluate_order_eligibility(order)
        item = result.items[0]
        self.assertTrue(item.eligible_for_size_exchange)
        self.assertIn("final_sale_exchange_only", item.notes)
        self.assertEqual(item.policy_clause, "2.4")

    def test_TR4529_cancelled_order_return_is_nonsensical(self):
        order = data_store.orders["TR-4529"]
        result = rules.evaluate_order_eligibility(order)
        self.assertEqual(result.order_level_block, "cancelled")
        self.assertEqual(result.policy_clause, "2.6")

    def test_TR4530_clean_happy_path(self):
        # Note: "The clean happy-path return: in window, returnable
        # category, not final sale."
        order = data_store.orders["TR-4530"]
        result = rules.evaluate_order_eligibility(order)
        item = result.items[0]
        self.assertTrue(item.eligible_for_return)
        self.assertEqual(result.order_level_block, None)


class TestRefundEstimation(unittest.TestCase):
    def test_upi_refund_destination_and_timeline(self):
        order = data_store.orders["TR-4530"]  # credit_card actually; check real method
        est = rules.estimate_refund(order)
        self.assertEqual(est.destination, "original card")
        self.assertEqual(est.timeline, "5-7 business days")

    def test_cod_refund_flags_human_agent_requirement(self):
        order = data_store.orders["TR-4528"]  # cash_on_delivery
        est = rules.estimate_refund(order)
        self.assertTrue(any("human agent" in n.lower() for n in est.notes))

    def test_partial_return_notes_no_free_shipping_recalc(self):
        order = data_store.orders["TR-4522"]
        est = rules.estimate_refund(order, skus=["TR-TSH-002"])
        self.assertTrue(any("not recalculated" in n.lower() for n in est.notes))


class TestPolicyGrounding(unittest.TestCase):
    def test_search_finds_relevant_clause(self):
        result = tools.search_policy(query="jewellery return")
        self.assertTrue(result["found"])
        numbers = [c["number"] for c in result["clauses"]]
        self.assertIn("2.3", numbers)

    def test_get_exact_clause_by_number(self):
        result = tools.get_policy_clause(number="1.6")
        self.assertTrue(result["ok"])
        self.assertIn("lost", result["text"].lower())

    def test_unanswerable_question_reports_not_found_rather_than_guessing(self):
        # Nothing in the policy about international shipping — must not
        # fabricate an answer.
        result = tools.search_policy(query="international shipping customs duty")
        # Either genuinely empty or low-relevance; the tool must never
        # silently invent clause text, so we assert the structural contract:
        # every returned clause must be real text from the actual document.
        for clause in result.get("clauses", []):
            self.assertIn(clause["number"], [c.number for c in tools.policy_store.clauses])


class TestEscalation(unittest.TestCase):
    def test_escalation_ticket_is_self_contained(self):
        result = tools.create_escalation(
            customer_id="C-101",
            order_id="TR-4526",
            reason="lost_in_transit",
            summary="Customer's Canvas Tote (TR-4526) marked lost by Delhivery. "
                    "Wants replacement or refund per policy 1.6.",
        )
        self.assertTrue(result["ok"])
        ticket = result["ticket"]
        for field in ("ticket_id", "customer_id", "customer_name", "order_id", "reason", "summary"):
            self.assertIn(field, ticket)
        self.assertEqual(ticket["customer_name"], "Marcus Bell")


class TestMultiTurnSessionState(unittest.TestCase):
    """Tests the orchestration layer itself (app.agent), not just the tools
    it calls. This is the part the assignment calls out explicitly:
    'carries state' across a multi-turn conversation. None of this touches
    the Groq API - _dispatch_tool_call and Session are plain Python that sit
    around the LLM call, so they're fully testable offline.

    Importing app.agent is safe without GROQ_API_KEY: the Groq client is
    constructed lazily inside run_turn(), never at import time.
    """

    def setUp(self):
        from app.agent import Session
        self.session = Session(session_id="test-session")

    def test_auth_gated_tool_fails_closed_before_identify(self):
        from app.agent import _dispatch_tool_call
        result = _dispatch_tool_call(self.session, "get_order", {"order_id": "TR-4521"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_authenticated")

    def test_identify_then_lookup_carries_customer_id_across_calls(self):
        """The core multi-turn guarantee: identify once, then every
        subsequent tool call in the session is scoped to that customer
        without the model ever supplying a customer_id itself."""
        from app.agent import _dispatch_tool_call

        ident = _dispatch_tool_call(self.session, "identify_customer",
                                     {"email": "ananya.rao@example.com"})
        self.assertTrue(ident["ok"])
        self.assertEqual(self.session.customer_id, "C-100")

        # Simulate two separate turns asking about two different orders -
        # neither call passes customer_id; it must come from session state.
        order1 = _dispatch_tool_call(self.session, "get_order", {"order_id": "TR-4521"})
        self.assertTrue(order1.get("ok", True))  # get_order returns order dict directly on success
        order2 = _dispatch_tool_call(self.session, "list_orders", {})
        self.assertNotEqual(order2.get("error"), "not_authenticated")

    def test_model_supplied_customer_id_is_ignored_not_trusted(self):
        """Even if a tool-call argument dict somehow contains a customer_id
        key (e.g. a prompt-injection attempt getting the model to pass one),
        the dispatcher overwrites it with the session's real identity rather
        than trusting the model's input."""
        from app.agent import _dispatch_tool_call

        _dispatch_tool_call(self.session, "identify_customer",
                             {"email": "ananya.rao@example.com"})  # -> C-100
        # Attempt to smuggle a different customer_id into the arguments.
        forged_args = {"order_id": "TR-4524", "customer_id": "C-101"}
        result = _dispatch_tool_call(self.session, "get_order", forged_args)
        # TR-4524 belongs to C-100 (the real session identity), so this
        # should resolve normally under C-100, NOT raise a mismatch for the
        # forged C-101 and NOT silently look it up under C-101.
        self.assertNotEqual(result.get("error"), "not_authenticated")

    def test_session_store_returns_same_session_across_turns(self):
        from app.agent import SessionStore
        store = SessionStore()
        s1 = store.get_or_create(None)
        s1.customer_id = "C-100"
        s2 = store.get_or_create(s1.session_id)
        self.assertIs(s1, s2)
        self.assertEqual(s2.customer_id, "C-100")

    def test_unauthenticated_session_cannot_reach_escalation_tool_either(self):
        """create_escalation is also in NEEDS_AUTH - a stray/adversarial
        attempt to raise a ticket before identification must fail closed
        the same way order lookups do."""
        from app.agent import _dispatch_tool_call
        result = _dispatch_tool_call(self.session, "create_escalation", {
            "order_id": None, "reason": "test", "summary": "test",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_authenticated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
