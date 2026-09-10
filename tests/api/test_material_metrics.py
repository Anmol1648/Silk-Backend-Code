"""A model's confidence must never poison the transaction it is written in.

`MaterialExtractionLink.confidence` is a DecimalField and was fed
`metric.get("confidence")` straight from the LLM. Asked for a confidence, a
model answers "high" about as often as 0.9 — and the resulting ValidationError
did not merely lose that metric. It broke the enclosing transaction, so every
later query in the task failed with TransactionManagementError, the critique
was discarded after the model had been paid for, and the uploaded document
contributed nothing to the scorecard.

This surfaced only once the critique started succeeding: while it failed
validation on a missing `detected_type`, the metrics loop was never reached.
"""
from django.test import TestCase

from fundos.readiness.tasks import _confidence


class ConfidenceCoercionTests(TestCase):

    def test_a_decimal_is_kept(self):
        self.assertEqual(_confidence(0.9), 0.9)
        self.assertEqual(_confidence("0.75"), 0.75)

    def test_the_word_that_broke_it(self):
        """'high' is the exact value from the 27-Aug failure."""
        self.assertEqual(_confidence("high"), 0.9)

    def test_the_other_words_a_model_uses(self):
        self.assertEqual(_confidence("medium"), 0.6)
        self.assertEqual(_confidence("low"), 0.3)
        self.assertEqual(_confidence("HIGH"), 0.9)
        self.assertEqual(_confidence("  High  "), 0.9)

    def test_a_percentage_is_normalised(self):
        self.assertEqual(_confidence("90%"), 0.9)
        self.assertEqual(_confidence(90), 0.9)

    def test_nonsense_becomes_none_not_an_exception(self):
        """None is honest. An exception takes the whole critique with it."""
        for bad in ("banana", "", "  ", None, [], {}, "N/A"):
            self.assertIsNone(_confidence(bad), f"{bad!r} should be None")

    def test_a_boolean_is_not_a_confidence(self):
        """bool is a subclass of int; True must not become 1.0."""
        self.assertIsNone(_confidence(True))
        self.assertIsNone(_confidence(False))

    def test_out_of_range_is_rejected(self):
        self.assertIsNone(_confidence(-1))
        self.assertIsNone(_confidence(1000))

    def test_the_boundaries_are_kept(self):
        self.assertEqual(_confidence(0), 0)
        self.assertEqual(_confidence(1), 1)

    def test_it_never_raises_on_any_input(self):
        """The whole point: this feeds a DecimalField inside a transaction."""
        for value in (object(), b"x", 1 + 2j, float("nan"), float("inf")):
            try:
                _confidence(value)
            except Exception as exc:      # pragma: no cover - the assertion
                self.fail(f"_confidence({value!r}) raised {exc!r}")
