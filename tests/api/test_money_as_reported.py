"""An amount, the currency it was stated in, and the scale it was stated at.

A number alone is not a fact about money. "20" is ₹20 crore or $20 million
depending on two labels that have to travel with it, and when they don't,
something downstream supplies them by guessing.

`funding_history.amount_usd_mn` could hold only USD millions, so a round the
deck states as ₹20 crore had to be converted before it could be written — by
the model, at a rate it decided in a sentence ("approximately 83 INR to 1
USD") and then discarded. What reached the database was `2.4`: the deck's own
words gone, the rate unauditable, and 12 crore rounded to $1.6M when it is
$1.45M, putting a scorecard row 10% low.

`financial_summary` shows the other half of the same failure — ₹56.9 crore
converted to 6.828 and then labelled `"currency": "INR"`. Read as stated,
FY2031's 415.8 means ₹415.8 million, about $5M. It means $415.8M.
"""
from django.core.management import call_command
from django.test import TestCase

from fundos.core.services import money
from tests.conftest_helpers import auth_headers, make_world


class AnAmountIsReadAsItWasWritten(TestCase):

    def test_the_indian_convention_is_understood(self):
        self.assertEqual(money.parse("₹20 crore")[1:], ("INR", "Cr"))
        self.assertEqual(money.parse("Rs. 12 Cr")[1:], ("INR", "Cr"))
        self.assertEqual(money.parse("INR 5 lakh")[1:], ("INR", "L"))

    def test_the_usual_dollar_forms_are_understood(self):
        for text in ("US$ 6.3Mn", "$2.4 million", "USD 10 Mn"):
            self.assertEqual(money.parse(text)[1], "USD", text)

    def test_the_amount_survives_its_labels(self):
        amount, _, _ = money.parse("₹20 crore")
        self.assertEqual(float(amount), 20.0)

    def test_a_bare_number_claims_no_currency_and_no_scale(self):
        """Inferring either is the whole failure. "20" is just 20."""
        self.assertEqual(money.parse("20"), (20, "", ""))

    def test_nothing_readable_is_not_an_error(self):
        self.assertEqual(money.parse("not disclosed")[0], None)
        self.assertEqual(money.parse(None)[0], None)


class AScaleIsWorthWhatItSays(TestCase):

    def test_crore_and_lakh(self):
        self.assertEqual(float(money.units(20, "Cr")), 200_000_000.0)
        self.assertEqual(float(money.units(5, "L")), 500_000.0)

    def test_millions_and_billions(self):
        self.assertEqual(float(money.units(2.4, "Mn")), 2_400_000.0)
        self.assertEqual(float(money.units(1.5, "Bn")), 1_500_000_000.0)

    def test_no_scale_means_whole_units(self):
        self.assertEqual(float(money.units(500, "")), 500.0)


class TheUsdCompanionIsDerivedAndSaysSo(TestCase):

    def test_dollars_need_no_rate(self):
        value, rate = money.to_usd_mn(2.4, "USD", "Mn")
        self.assertEqual(value, 2.4)
        self.assertIsNone(rate, "a USD figure was 'converted'")

    def test_rupees_are_converted_at_a_stated_rate(self):
        value, rate = money.to_usd_mn(20, "INR", "Cr", rate=83)
        self.assertAlmostEqual(value, 2.4096, places=3)
        self.assertEqual(rate, 83.0)

    def test_the_rate_is_recorded_so_a_wrong_one_is_correctable(self):
        at_83 = money.to_usd_mn(12, "INR", "Cr", rate=83)
        at_95 = money.to_usd_mn(12, "INR", "Cr", rate=95)
        self.assertNotEqual(at_83[0], at_95[0])
        self.assertEqual((at_83[1], at_95[1]), (83.0, 95.0))

    def test_twelve_crore_is_not_one_point_six_million(self):
        """The rounding that produced 1.5x where 1.67x is true."""
        value, _ = money.to_usd_mn(12, "INR", "Cr", rate=83)
        self.assertLess(value, 1.5)

    def test_a_currency_with_no_rate_is_left_blank_not_passed_through(self):
        """Passing an unconvertible figure through as though it were already
        USD is how ₹56.9 crore came to be stored as 6.828 and labelled INR."""
        self.assertEqual(money.to_usd_mn(5, "EUR", "Mn"), (None, None))

    def test_a_missing_amount_converts_to_nothing(self):
        self.assertEqual(money.to_usd_mn(None, "INR", "Cr"), (None, None))


class AnAmountRendersAsItWasWritten(TestCase):

    def test_rupees_in_crore(self):
        self.assertEqual(money.display(20, "INR", "Cr"), "₹20 Cr")

    def test_dollars_in_millions(self):
        self.assertEqual(money.display(6.3, "USD", "Mn"), "US$ 6.3 Mn")

    def test_no_amount_renders_as_nothing_not_zero(self):
        self.assertEqual(money.display(None, "INR", "Cr"), "")

    def test_the_block_separates_fact_from_convenience(self):
        block = money.block(20, "INR", "Cr", rate=83, as_of="2025-01-15")
        self.assertEqual((block["amount"], block["currency"],
                          block["denomination"]), (20.0, "INR", "Cr"))
        self.assertEqual(block["fxBasis"], "derived")
        self.assertEqual(block["fxAsOf"], "2025-01-15")

    def test_an_unconvertible_block_carries_no_rate_at_all(self):
        block = money.block(5, "EUR", "Mn")
        self.assertIsNone(block["amountUsdMn"])
        self.assertNotIn("fxRate", block)


class AFundingRoundKeepsWhatTheDeckSaid(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        self.profile = get_or_create_profile(self.world["a"]["company"],
                                             user=self.world["a"]["founder"])

    def _write(self, rows):
        from fundos.profile.section_writer import update_section_from_data
        update_section_from_data(self.profile, "funding_history", rows,
                                 user=self.world["a"]["founder"])

    def _rows(self):
        from fundos.profile.spec_serializer import serialize_section
        return serialize_section(self.profile, "funding_history")["data"]

    def test_a_rupee_round_is_stored_and_served_in_rupees(self):
        self._write([{"date": "2025-01-15", "round": "Series A",
                      "amount": 20, "currency": "INR", "denomination": "Cr",
                      "investors": ["PeerCapital"]}])
        row = self._rows()[0]
        self.assertEqual(row["amount"], 20.0)
        self.assertEqual(row["currency"], "INR")
        self.assertEqual(row["denomination"], "Cr")
        self.assertEqual(row["amount_display"], "₹20 Cr")

    def test_the_usd_companion_is_derived_beside_it(self):
        self._write([{"date": "2025-01-15", "round": "Series A",
                      "amount": 20, "currency": "INR", "denomination": "Cr"}])
        row = self._rows()[0]
        self.assertIsNotNone(row["amount_usd_mn"])
        self.assertIsNotNone(row["fx_rate"])
        self.assertEqual(row["fx_as_of"], "2025-01-15",
                         "the rate must carry the date it applies to")

    def test_the_reported_figure_is_not_overwritten_by_the_conversion(self):
        self._write([{"date": "2025-01-15", "round": "Series A",
                      "amount": 20, "currency": "INR", "denomination": "Cr"}])
        row = self._rows()[0]
        self.assertEqual(row["amount"], 20.0, "the deck's own figure was lost")
        self.assertNotEqual(row["amount"], row["amount_usd_mn"])

    def test_a_dollar_round_needs_no_conversion(self):
        self._write([{"date": "2024-06-01", "round": "Seed", "amount": 1.5,
                      "currency": "USD", "denomination": "Mn"}])
        row = self._rows()[0]
        self.assertEqual(row["amount_usd_mn"], 1.5)
        self.assertIsNone(row["fx_rate"])

    def test_an_undisclosed_amount_stays_undisclosed(self):
        """Not zero. Three of Tyreplex's five rounds state no amount."""
        self._write([{"date": "2021-06", "round": "Seed", "amount": None,
                      "currency": "", "denomination": ""}])
        row = self._rows()[0]
        self.assertIsNone(row["amount"])
        self.assertIsNone(row["amount_usd_mn"])
        self.assertEqual(row["amount_display"], "")

    def test_an_older_payload_with_only_usd_still_writes(self):
        """Every existing caller sends `amount_usd_mn` and must keep working."""
        self._write([{"date": "2024-01-01", "round": "Seed",
                      "amount_usd_mn": 3.2}])
        row = self._rows()[0]
        self.assertEqual(row["amount_usd_mn"], 3.2)

    def test_two_rounds_in_different_currencies_both_survive(self):
        self._write([
            {"date": "2025-01-15", "round": "Series A", "amount": 20,
             "currency": "INR", "denomination": "Cr"},
            {"date": "2022-08-01", "round": "Pre-A", "amount": 1.5,
             "currency": "USD", "denomination": "Mn"}])
        shown = {r["round"]: r["amount_display"] for r in self._rows()}
        self.assertEqual(shown["Series A"], "₹20 Cr")
        self.assertEqual(shown["Pre-A"], "US$ 1.5 Mn")

    def test_the_template_row_names_the_new_fields(self):
        """A client renders the template before anything fills the section."""
        from fundos.profile.spec_serializer import _LIST_ROW_TEMPLATES

        template = _LIST_ROW_TEMPLATES["funding_history"]
        for field in ("amount", "currency", "denomination", "amount_display",
                      "fx_rate", "fx_as_of"):
            self.assertIn(field, template)


class TheModelIsToldToReportNotConvert(TestCase):

    def test_the_contract_asks_for_the_figure_as_stated(self):
        from fundos.profile import schema

        spec = schema.sections_by_key().get("funding_history") or {}
        fields = spec.get("fields") or {}
        self.assertIn("currency", fields)
        self.assertIn("denomination", fields)
        self.assertIn("no conversion", fields.get("amount", ""))

    def test_the_indian_scales_are_named_so_they_can_be_copied(self):
        from fundos.profile import schema

        spec = schema.sections_by_key().get("funding_history") or {}
        denomination = (spec.get("fields") or {}).get("denomination", "")
        self.assertIn("Cr", denomination)
        self.assertIn("crore", denomination)
