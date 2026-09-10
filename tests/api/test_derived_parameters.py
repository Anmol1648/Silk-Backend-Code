"""Scorecard rows that are arithmetic are computed, not read.

"Raise vs Last Round" is the current ask divided by the previous round.
Nothing in a deck states it, so a model asked for it does the division
itself — and the row then arrives labelled "Estimate, extractor confidence:
high … this row rests on that confidence rather than on the strength of its
source", which is the wrong claim twice over. The division is certain; the
uncertainty belongs to the two inputs, which are extracted separately and
already carry their own tiers.

Three failures were observed on real Tyreplex and Zyla runs, and every test
here is one of them:

1. The model converted currency to divide. One run stated BOTH answers in the
   same sentence — "INR 20 crore (~$2.4M) and … INR 12 crore (~$1.6M) … =
   1.5x. (20 Cr / 12 Cr = 1.66x)" — and stored 1.5x, because 12 crore was
   rounded to $1.6M when it is $1.45M. In a ratio the currency cancels out.
2. It divided by the round that CLOSED in January 2025 instead of the $10M
   being raised now: 1.5x and 39.3% stored, 4.2x and 164% true, both stored
   values scoring Excellent where the truth is Fair and below-Fair.
3. It cited its own working — four hundred words of reasoning stored as a
   verbatim quote from the dossier.
"""
from django.test import TestCase

from fundos.assessment import derived

#: Tyreplex's funding table exactly as it is stored, nulls and all.
TYREPLEX = [
    {"date": "2025-03-10", "round": "Seed", "amount_usd_mn": None},
    {"date": "2025-01-15", "round": "Series A", "amount_usd_mn": 2.4},
    {"date": "2024-12-04", "round": "Seed", "amount_usd_mn": None},
    {"date": "2022-08", "round": "Seed (Pre-Series A)", "amount_usd_mn": 1.6},
    {"date": "2021-06", "round": "Seed", "amount_usd_mn": None},
]


class ThePreviousRoundIsTheLastOneThatStatesAnAmount(TestCase):

    def test_the_most_recent_priced_round_wins(self):
        self.assertEqual(
            derived.previous_round(TYREPLEX)["date"], "2025-01-15")

    def test_a_round_with_no_amount_is_skipped_not_read_as_zero(self):
        """Three of five Tyreplex rounds state no amount. A null denominator
        is a missing input, not a small one."""
        self.assertIsNotNone(derived.previous_round(TYREPLEX)["amount_usd_mn"])

    def test_a_partial_date_orders_correctly(self):
        """"2022-08" is a real value in this data."""
        rounds = [{"date": "2022-08", "round": "Pre-A", "amount_usd_mn": 1.6},
                  {"date": "2021-06-01", "round": "Seed",
                   "amount_usd_mn": 0.5}]
        self.assertEqual(derived.previous_round(rounds)["round"], "Pre-A")

    def test_no_priced_round_at_all_yields_nothing(self):
        self.assertIsNone(derived.previous_round(
            [{"date": "2024-01-01", "round": "Seed", "amount_usd_mn": None}]))

    def test_an_empty_table_yields_nothing(self):
        self.assertIsNone(derived.previous_round([]))
        self.assertIsNone(derived.previous_round(None))


class TheTotalSaysWhereItCameFrom(TestCase):

    def test_a_complete_table_is_summed(self):
        rounds = [{"amount_usd_mn": 2.4}, {"amount_usd_mn": 1.6}]
        total, basis = derived.total_raised(rounds, 6.1)
        self.assertEqual(float(total), 4.0)
        self.assertEqual(basis, "rounds")

    def test_an_incomplete_table_falls_back_and_says_so(self):
        """Tyreplex's table accounts for $4.0M of a reported $6.1M. Summing it
        would understate the denominator by a third; using the reported figure
        silently hides that a third of it is unevidenced."""
        total, basis = derived.total_raised(TYREPLEX, 6.1)
        self.assertEqual(float(total), 6.1)
        self.assertEqual(basis, "reported")

    def test_neither_available_is_not_zero(self):
        self.assertEqual(derived.total_raised(TYREPLEX, None), (None, ""))


class TheRatiosAreArithmetic(TestCase):

    def test_the_raise_multiple_uses_the_ask_not_the_last_close(self):
        """The reported bug: 1.5x stored where 4.17x is true."""
        value, working = derived.raise_multiple(10.0, TYREPLEX)
        self.assertEqual(value, 4.17)
        self.assertEqual(working["previous_round"], "Series A")

    def test_the_raise_share_uses_the_ask_too(self):
        """39.3% stored where 164% is true — outside even the Fair band."""
        value, _ = derived.raise_share(10.0, TYREPLEX, 6.1)
        self.assertEqual(value, 163.93)

    def test_a_ratio_needs_no_exchange_rate(self):
        """Both rounds in INR crore divide to the same number they would in
        any currency. Converting to one decimal first is what produced 1.5x
        instead of 1.67x."""
        rounds = [{"date": "2022-08-01", "round": "Pre-A",
                   "amount_usd_mn": 12}]
        value, _ = derived.raise_multiple(20, rounds)
        self.assertEqual(value, 1.67)

    def test_a_missing_ask_gives_no_number_and_says_which_term(self):
        value, working = derived.raise_multiple(None, TYREPLEX)
        self.assertIsNone(value)
        self.assertIn("being raised", working["missing"])

    def test_a_missing_previous_round_gives_no_number(self):
        value, working = derived.raise_multiple(
            10.0, [{"date": "2024-01-01", "amount_usd_mn": None}])
        self.assertIsNone(value)
        self.assertIn("previous round", working["missing"])

    def test_a_zero_denominator_is_refused_not_divided_by(self):
        value, _ = derived.raise_multiple(
            10.0, [{"date": "2024-01-01", "amount_usd_mn": 0}])
        self.assertIsNone(value)

    def test_the_working_is_shown(self):
        _, working = derived.raise_multiple(10.0, TYREPLEX)
        self.assertIn("10.0 / 2.4", working["formula"])

    def test_a_reported_total_carries_its_caveat(self):
        _, working = derived.raise_share(10.0, TYREPLEX, 6.1)
        self.assertIn("not from the funding table", working["caveat"])

    def test_junk_inputs_do_not_raise(self):
        for bad in ("", "n/a", None, True, [], {}):
            self.assertIsNone(derived.raise_multiple(bad, TYREPLEX)[0])


class TheRowInheritsTheWeakestTier(TestCase):
    """A ratio can be no better evidenced than the numbers it divides."""

    def test_web_research_drags_the_row_down(self):
        self.assertEqual(derived.weakest_tier(1, 3), 3)

    def test_two_verified_inputs_stay_verified(self):
        self.assertEqual(derived.weakest_tier(1, 1), 1)

    def test_an_unknown_tier_is_ignored_not_treated_as_best(self):
        self.assertEqual(derived.weakest_tier(None, 2), 2)
        self.assertIsNone(derived.weakest_tier(None, None))

    def test_the_two_rows_can_differ(self):
        """Tyreplex's multiple rests on the funding table; its share rests on
        a Tracxn figure. They must not claim the same tier."""
        rows = derived.build_rows(
            current_raise=10.0, rounds=TYREPLEX, reported_total=6.1,
            input_tiers={"current_raise": 1, "previous_round": 1,
                         "total_raised": 3})
        by_key = {r["input_key"]: r for r in rows}
        self.assertEqual(by_key[derived.RAISE_MULTIPLE]["source_tier"], 1)
        self.assertEqual(by_key[derived.RAISE_SHARE]["source_tier"], 3)


class TheRowsAreShapedLikeExtractedOnes(TestCase):

    def _rows(self, **kwargs):
        options = {"current_raise": 10.0, "rounds": TYREPLEX,
                   "reported_total": 6.1}
        options.update(kwargs)
        return {r["input_key"]: r for r in derived.build_rows(**options)}

    def test_both_rows_are_produced(self):
        self.assertEqual(sorted(self._rows()), sorted(derived.DERIVED_KEYS))

    def test_the_value_and_unit_are_carried(self):
        row = self._rows()[derived.RAISE_MULTIPLE]
        self.assertEqual(row["value"], "4.17")
        self.assertEqual(row["unit"], "x")

    def test_the_source_type_says_it_was_computed(self):
        self.assertEqual(self._rows()[derived.RAISE_MULTIPLE]["source_type"],
                         "derived")

    def test_confidence_is_certain_because_division_is(self):
        """Doubt belongs to the terms, which carry their own tiers."""
        self.assertEqual(self._rows()[derived.RAISE_MULTIPLE]["confidence"],
                         1.0)

    def test_the_justification_is_the_sum_not_a_monologue(self):
        row = self._rows()[derived.RAISE_MULTIPLE]
        self.assertLess(len(row["justification"]), 120)
        self.assertIn("/", row["justification"])

    def test_a_row_whose_inputs_are_missing_is_omitted_not_zeroed(self):
        """A blank row is excluded from the roll-up and redistributes its
        weight. A zero would score Poor and be read as a finding."""
        self.assertEqual(self._rows(current_raise=None), {})

    def test_the_share_is_omitted_when_no_total_is_known(self):
        rows = self._rows(reported_total=None,
                          rounds=[{"date": "2024-01-01",
                                   "amount_usd_mn": None}])
        self.assertNotIn(derived.RAISE_SHARE, rows)
