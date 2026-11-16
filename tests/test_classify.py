"""One case per decision-table row plus the regex helpers.  Run: python -m unittest

The model is replaced by FakeModel so the suite needs no Ollama and runs in well
under a second.  What is covered:

    Rules       each of the four header rules fires and lands in the right category;
                a sensitive hit stops the pipeline before the model (calls == 0)
    ModelPaths  each row of classify.decide() reached through classify_one()
    Helpers     Luhn, the card regex needing Luhn, the PHI combination, strip_quotes

What is not covered, on purpose: the Ollama client itself (needs a live server;
exercised by the demo), report.py and store.py (thin glue over csv/sqlite; the
end-to-end run exercises them), generate.py (test data, not product).
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from retain import classify  # noqa: E402

# The shipped policy, so the tests also catch a broken policy.json.
POLICY = json.load(open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "policy.json"), encoding="utf-8"))
# Fixed "today" so age arithmetic in the tests never drifts.
NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


class FakeModel:
    """Stands in for classify.Model: returns a canned answer and counts calls."""

    def __init__(self, klass="UNSURE", series=None):
        self.answer = {"klass": klass, "series": series, "reason": "fake"}
        self.calls = 0

    def classify(self, msg):
        self.calls += 1
        return self.answer


def mail(subject="Hello", body="Body text here.", days_old=400, frm="a.user@cityofexample.gov", date=True, **headers):
    """Build a message and run it through classify.parse().  Extra kwargs become headers (List_Id -> List-Id)."""
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, "b.user@cityofexample.gov", subject, "<x@y>"
    if date:
        m["Date"] = format_datetime(NOW - timedelta(days=days_old))
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return classify.parse(m.as_bytes(), 0)


def run(msg, model=None):
    """classify_one with the shipped policy and the fixed clock."""
    return classify.classify_one(msg, POLICY, model or FakeModel(), NOW)


class Rules(unittest.TestCase):
    """Stages 1 and 2: things decided without the model."""

    def test_calendar_old_is_delete_proposed(self):
        m = mail("Accepted: Budget review")
        m.content_types.add("text/calendar")     # what a real invite's text/calendar part produces
        r = run(m)
        self.assertEqual((r["category"], r["decision"]), ("CALENDAR", "DELETE_PROPOSED"))

    def test_list_header(self):
        self.assertEqual(run(mail(List_Unsubscribe="<mailto:x>"))["category"], "BULK_LIST")

    def test_auto_reply(self):
        self.assertEqual(run(mail(Auto_Submitted="auto-replied"))["category"], "AUTO_REPLY")

    def test_system_sender(self):
        self.assertEqual(run(mail(frm="noreply@cityofexample.gov"))["category"], "SYSTEM")

    def test_sensitive_skips_model(self):
        # The data-residency guarantee in one assertion: the model is never called.
        fm = FakeModel("TRANSITORY")
        r = run(mail(body="my ssn is 123-45-6789"), fm)
        self.assertEqual((r["decision"], r["flags"], fm.calls), ("REVIEW_SENSITIVE", ["SSN"], 0))


class ModelPaths(unittest.TestCase):
    """Stage 3 answers through every row of the decision table."""

    def test_record_retains_with_until(self):
        # 400 days before NOW is late 2025; CONTRACTS is 5 years -> 2030.
        r = run(mail("Amendment 2"), FakeModel("RECORD", "CONTRACTS"))
        self.assertEqual((r["decision"], r["category"]), ("RETAIN", "CONTRACTS"))
        self.assertTrue(r["retain_until"].startswith("2030"))

    def test_permanent_has_no_until(self):
        self.assertIsNone(run(mail(), FakeModel("RECORD", "MINUTES"))["retain_until"])

    def test_recent_transitory_pending(self):
        self.assertEqual(run(mail(days_old=3), FakeModel("TRANSITORY"))["decision"], "RETAIN_PENDING")

    def test_old_transitory_proposed(self):
        self.assertEqual(run(mail(days_old=31), FakeModel("TRANSITORY"))["decision"], "DELETE_PROPOSED")

    def test_unsure_reviews(self):
        self.assertEqual(run(mail(), FakeModel("UNSURE"))["decision"], "REVIEW")

    def test_no_date_transitory_reviews(self):
        r = run(mail(date=False), FakeModel("TRANSITORY"))
        self.assertEqual(r["decision"], "REVIEW")
        self.assertIn("no Date", r["reason"])


class Helpers(unittest.TestCase):
    """The regex and text helpers on their own."""

    def test_luhn(self):
        self.assertTrue(classify.luhn("4111111111111111"))
        self.assertFalse(classify.luhn("4111111111111112"))

    def test_card_needs_luhn(self):
        # Same shape, one digit off: the regex matches both, Luhn accepts only the first.
        self.assertEqual(classify.scan_sensitive("card 4111 1111 1111 1111"), ["PCI_CARD"])
        self.assertEqual(classify.scan_sensitive("ref 4111 1111 1111 1112"), [])

    def test_phi(self):
        self.assertIn("PHI", classify.scan_sensitive("Patient MRN: 4482913 diagnosis"))

    def test_strip_quotes(self):
        own = classify.strip_quotes("Thanks!\n\nOn Tue, Bob wrote:\n> contract amendment attached\n> please sign")
        self.assertEqual(own, "Thanks!")


if __name__ == "__main__":
    unittest.main()
