"""A growth multiple has to agree with the revenue rows beside it.

One profile stated a 13x growth metric while its own revenue series, over the
same years, worked out to about 7x. Both figures were on the same screen, and
nothing reconciled them: the model did the arithmetic in prose and the rows
were never consulted.

The number that can be checked is the one that stays. The multiple is
recomputed from the rows the profile itself carries, and what the model
asserted is reported in the run notes rather than silently swapped out.

Everything here is arithmetic on rows supplied by the test: no company, no
sector and no figure from any live profile.
"""
from django.test import TestCase

from fundos.profile import schema


def _payload(metrics, financials):
    return {"sections": {
        "company_metrics": {"data": metrics},
        "financial_summary": {"data": {"financials": financials}}}}


def _run(metrics, financials):
    notes = []
    profile = schema.normalize_profile(_payload(metrics, financials),
                                       notes=notes)
    return profile["sections"]["company_metrics"]["data"], notes


def _rows(*pairs, currency="INR", denomination="Cr"):
    return [{"financial_year": year, "revenue": revenue,
             "currency": currency, "denomination": denomination}
            for year, revenue in pairs]


class AContradictedMultipleIsRecomputed(TestCase):

    def test_the_reported_shape_of_the_defect(self):
        rows, notes = _run(
            [{"metric": "Revenue growth FY23-FY25", "value": "13x"}],
            _rows(("FY 2023", 8.0), ("FY 2025", 56.9)))
        self.assertEqual(rows[0]["value"], "7.11x")
        self.assertTrue(any("13x" in n for n in notes))

    def test_the_note_names_both_figures_and_the_years(self):
        _, notes = _run(
            [{"metric": "Revenue growth FY23-FY25", "value": "13x"}],
            _rows(("FY 2023", 8.0), ("FY 2025", 56.9)))
        note = " ".join(notes)
        self.assertIn("13x", note)
        self.assertIn("7.11x", note)
        self.assertIn("FY2023", note)

    def test_a_multiple_the_rows_agree_with_is_untouched(self):
        rows, notes = _run(
            [{"metric": "Revenue growth", "value": "7x"}],
            _rows(("FY 2023", 8.0), ("FY 2025", 56.9)))
        self.assertEqual(rows[0]["value"], "7x")
        self.assertEqual(notes, [])

    def test_a_small_difference_is_not_worth_correcting(self):
        """Rounding and a restated year both move a multiple a little."""
        rows, _ = _run([{"metric": "Revenue growth", "value": "8x"}],
                       _rows(("FY 2023", 8.0), ("FY 2025", 56.9)))
        self.assertEqual(rows[0]["value"], "8x")

    def test_the_named_years_decide_the_period(self):
        """A metric about two of four years is checked against those two."""
        rows, _ = _run(
            [{"metric": "Revenue growth FY24-FY25", "value": "9x"}],
            _rows(("FY 2023", 8.0), ("FY 2024", 20.0), ("FY 2025", 40.0)))
        self.assertEqual(rows[0]["value"], "2x")

    def test_an_unnamed_period_uses_the_whole_series(self):
        rows, _ = _run([{"metric": "Revenue growth", "value": "9x"}],
                       _rows(("FY 2023", 10.0), ("FY 2025", 30.0)))
        self.assertEqual(rows[0]["value"], "3x")


class WhatIsLeftAlone(TestCase):

    def test_a_metric_that_is_not_a_multiple(self):
        rows, notes = _run(
            [{"metric": "Active customers", "value": "12,000"}],
            _rows(("FY 2023", 8.0), ("FY 2025", 56.9)))
        self.assertEqual(rows[0]["value"], "12,000")
        self.assertEqual(notes, [])

    def test_a_multiple_that_is_not_about_growth(self):
        """An EV/revenue multiple is not a claim about a revenue series."""
        rows, _ = _run(
            [{"metric": "EV / Revenue", "value": "13x"}],
            _rows(("FY 2023", 8.0), ("FY 2025", 56.9)))
        self.assertEqual(rows[0]["value"], "13x")

    def test_a_cagr_is_a_rate_not_a_multiple(self):
        rows, _ = _run([{"metric": "Revenue CAGR", "value": "92%"}],
                       _rows(("FY 2023", 8.0), ("FY 2025", 56.9)))
        self.assertEqual(rows[0]["value"], "92%")

    def test_a_series_too_short_to_check_against(self):
        rows, notes = _run([{"metric": "Revenue growth", "value": "13x"}],
                           _rows(("FY 2025", 56.9)))
        self.assertEqual(rows[0]["value"], "13x")
        self.assertEqual(notes, [])

    def test_no_financial_rows_at_all(self):
        rows, _ = _run([{"metric": "Revenue growth", "value": "13x"}], [])
        self.assertEqual(rows[0]["value"], "13x")

    def test_a_series_starting_at_zero_implies_nothing(self):
        """Dividing by a zero base is not a multiple, it is an error."""
        rows, _ = _run([{"metric": "Revenue growth", "value": "13x"}],
                       _rows(("FY 2023", 0), ("FY 2025", 56.9)))
        self.assertEqual(rows[0]["value"], "13x")

    def test_a_series_stated_in_two_currencies_is_not_divided_across_them(self):
        rows, _ = _run(
            [{"metric": "Revenue growth", "value": "13x"}],
            [{"financial_year": "FY 2023", "revenue": 8.0,
              "currency": "INR", "denomination": "Cr"},
             {"financial_year": "FY 2025", "revenue": 56.9,
              "currency": "USD", "denomination": "Mn"}])
        self.assertEqual(rows[0]["value"], "13x")


class TheYearReaderHandlesWhatSourcesWrite(TestCase):

    def test_the_usual_forms(self):
        for text, year in (("FY 2024", 2024), ("FY24", 2024), ("2024", 2024),
                           ("FY-25", 2025)):
            self.assertEqual(schema._fiscal_year(text), year, text)

    def test_text_with_no_year_in_it(self):
        for text in ("", None, "last year", "FY"):
            self.assertIsNone(schema._fiscal_year(text))
