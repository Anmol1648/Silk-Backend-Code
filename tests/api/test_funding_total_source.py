"""What a company raised is settled by the rounds it recorded.

Two failures sat on the same figure.

The first is arithmetic. A round now keeps the currency and the scale its
source used, so `amount_value` is 20 on a INR 20 crore round and 5 on a US$5
Mn one. Both the profile panel and the dashboard card summed that column
directly: 25 of nothing, presented as US$25 Mn.

The second is precedence. A total stated by research -- an aggregator, a
press round-up, a figure in a deck -- was preferred over the rounds listed
directly beneath it on the same screen, with nothing saying the two
disagreed.

Nothing here names a company, a currency or a source: the rules are counted
and compared, so they hold for the next company as well as this one.
"""
import uuid

from django.core.management import call_command
from django.test import TestCase

from fundos.core.models import Company, Membership, Tenant, User
from fundos.profile.models import FundingRound
from fundos.profile.section_writer import update_section_from_data
from fundos.profile.services import get_or_create_profile
from fundos.profile.spec_serializer import build_sections, serialize_section


def _bootstrap():
    tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
    user = User.objects.create_user(
        email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
        name="Funding User")
    company = Company.objects.create(
        tenant_id=tenant.id, name="Total Co", created_by=user)
    Membership.objects.create(
        tenant_id=tenant.id, user=user, scope_type="company",
        scope_id=company.id, role="founder", status="active")
    return tenant, user, company, get_or_create_profile(company, user=user)


class FundingCase(TestCase):
    """Shared bootstrap: the section registry drives what is writable."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def _round(self, **kwargs):
        kwargs.setdefault("round_name", "Seed")
        return FundingRound.objects.create(
            tenant_id=self.tenant.id, profile=self.profile, **kwargs)

    def _data(self):
        return serialize_section(self.profile, "company_profile")["data"]


class TheTotalAddsUsdNotMixedUnits(FundingCase):

    def test_a_round_in_a_local_scale_is_not_added_as_dollars(self):
        """20 crore rupees is a few million dollars, not twenty."""
        self._round(amount_value=20, amount_ccy="INR",
                    amount_denomination="Cr")
        total = self._data()["total_funding_raised_usd_mn"]
        self.assertLess(total, 5)
        self.assertGreater(total, 1)

    def test_rounds_in_different_currencies_are_summed_in_one(self):
        self._round(amount_value=20, amount_ccy="INR",
                    amount_denomination="Cr")
        self._round(amount_value=5, amount_ccy="USD",
                    amount_denomination="Mn")
        total = self._data()["total_funding_raised_usd_mn"]
        self.assertGreater(total, 5)
        self.assertLess(total, 10)

    def test_a_stored_companion_is_preferred_over_todays_rate(self):
        """It was struck when the round was written, and recorded with it."""
        self._round(amount_value=20, amount_ccy="INR",
                    amount_denomination="Cr", amount_usd_mn=2.4)
        self.assertEqual(self._data()["total_funding_raised_usd_mn"], 2.4)

    def test_a_row_that_never_stated_a_scale_still_means_millions(self):
        """The column pair was USD millions by contract before the scale had
        anywhere to go. Reading the blank as whole units would report a $5 Mn
        round as $0.000005 Mn."""
        self._round(amount_value=5)
        self.assertEqual(self._data()["total_funding_raised_usd_mn"], 5.0)

    def test_an_unconvertible_round_is_left_out_and_said_so(self):
        """Counting it as dollars would be a silent invention; dropping it
        silently would make a short total look complete."""
        self._round(amount_value=5, amount_ccy="USD",
                    amount_denomination="Mn")
        self._round(amount_value=40, amount_ccy="ZWL",
                    amount_denomination="Mn")
        data = self._data()
        self.assertEqual(data["total_funding_raised_usd_mn"], 5.0)
        basis = data["total_funding_raised_basis"]
        self.assertIn("1 of 2", basis)
        self.assertIn("NOT included", basis)

    def test_the_basis_stays_quiet_when_every_round_counted(self):
        self._round(amount_value=5, amount_ccy="USD",
                    amount_denomination="Mn")
        basis = self._data()["total_funding_raised_basis"]
        self.assertIn("1 round", basis)
        self.assertNotIn("NOT included", basis)

    def test_no_rounds_means_no_total_rather_than_zero(self):
        data = self._data()
        self.assertIsNone(data["total_funding_raised_usd_mn"])
        self.assertEqual(data["total_funding_raised_basis"], "")

    def test_the_dashboard_card_agrees_with_the_panel(self):
        from fundos.profile.summary import company_summaries

        self._round(amount_value=20, amount_ccy="INR",
                    amount_denomination="Cr")
        self._round(amount_value=5, amount_ccy="USD",
                    amount_denomination="Mn")
        card = company_summaries([self.company.id])[str(self.company.id)]
        self.assertEqual(card["totalFundingReceivedUsdMn"],
                         self._data()["total_funding_raised_usd_mn"])


class TheTotalIsAlsoSayableAsReported(FundingCase):

    def test_one_currency_throughout_is_summed_as_reported(self):
        self._round(amount_value=20, amount_ccy="INR",
                    amount_denomination="Cr")
        self._round(amount_value=32.25, amount_ccy="INR",
                    amount_denomination="Cr")
        data = self._data()
        self.assertEqual(data["total_funding_raised_amount"], 52.25)
        self.assertEqual(data["total_funding_raised_currency"], "INR")
        self.assertEqual(data["total_funding_raised_denomination"], "Cr")
        self.assertEqual(data["total_funding_raised_display"], "INR:52.25:Cr")

    def test_mixed_currencies_claim_no_single_reported_figure(self):
        """There is no one currency the sum could be stated in."""
        self._round(amount_value=20, amount_ccy="INR",
                    amount_denomination="Cr")
        self._round(amount_value=5, amount_ccy="USD",
                    amount_denomination="Mn")
        data = self._data()
        self.assertIsNone(data["total_funding_raised_amount"])
        self.assertEqual(data["total_funding_raised_currency"], "")

    def test_the_display_still_parses_back_to_money(self):
        from fundos.profile.section_writer import _parse_display_to_usd_mn

        self._round(amount_value=20, amount_ccy="INR",
                    amount_denomination="Cr")
        display = self._data()["total_funding_raised_display"]
        self.assertIsNotNone(_parse_display_to_usd_mn(display))


class TheRoundsOutrankAStatedTotal(FundingCase):

    def _state(self, total):
        update_section_from_data(
            self.profile, "company_profile",
            {"description_of_business": "Anything", "website": "",
             "country": "IN", "macro_sector": "", "sub_sector": "",
             "funding_status_name": "", "revenue_size_name": "",
             "currency_id": "USD", "total_funding_raised_usd_mn": total},
            user=self.user)

    def test_the_table_wins_when_the_two_disagree(self):
        self._round(amount_value=5, amount_ccy="USD",
                    amount_denomination="Mn")
        self._state(30)
        self.assertEqual(self._data()["total_funding_raised_usd_mn"], 5.0)

    def test_the_stated_figure_is_shown_beside_it_not_discarded(self):
        self._round(amount_value=5, amount_ccy="USD",
                    amount_denomination="Mn")
        self._state(30)
        data = self._data()
        self.assertEqual(data["total_funding_raised_reported_usd_mn"], 30)
        self.assertIn("30", data["total_funding_raised_gap"])
        self.assertIn("25", data["total_funding_raised_gap"])

    def test_agreement_produces_no_gap_line(self):
        self._round(amount_value=5, amount_ccy="USD",
                    amount_denomination="Mn")
        self._state(5)
        self.assertEqual(self._data()["total_funding_raised_gap"], "")

    def test_a_rounding_sized_difference_is_not_worth_a_line(self):
        self._round(amount_value=5, amount_ccy="USD",
                    amount_denomination="Mn")
        self._state(5.1)
        self.assertEqual(self._data()["total_funding_raised_gap"], "")

    def test_a_stated_total_stands_alone_with_no_rounds_recorded(self):
        """Nothing to prefer over it -- and this is how a total entered by
        hand survives on a profile whose history is not filled in."""
        self._state(30)
        data = self._data()
        self.assertEqual(data["total_funding_raised_usd_mn"], 30)
        self.assertEqual(data["total_funding_raised_gap"], "")

    def test_a_profile_with_neither_shows_no_gap_line(self):
        data = self._data()
        self.assertIsNone(data["total_funding_raised_reported_usd_mn"])
        self.assertEqual(data["total_funding_raised_gap"], "")

    def test_the_gap_sentence_names_no_company(self):
        """The fix has to hold for every company created after this one."""
        self._round(amount_value=5, amount_ccy="USD",
                    amount_denomination="Mn")
        self._state(30)
        gap = self._data()["total_funding_raised_gap"].lower()
        self.assertNotIn("total co", gap)


class AValuationKeepsTheCurrencyItWasStatedIn(FundingCase):

    def _data(self):
        return build_sections(self.profile)["company_profile"]["data"]

    def test_a_dollar_valuation_is_unchanged(self):
        self._round(round_name="Series A", announced_date="2025-03-01",
                    pre_money_value=40, post_money_value=52.5,
                    valuation_ccy="USD")
        data = self._data()
        self.assertEqual(data["latest_pre_money_usd_mn"], 40)
        self.assertEqual(data["latest_post_money_usd_mn"], 52.5)

    def test_a_local_currency_valuation_is_converted_not_relabelled(self):
        """40 in a non-dollar currency is not 40 million dollars."""
        self._round(round_name="Series A", announced_date="2025-03-01",
                    pre_money_value=40, post_money_value=52.5,
                    valuation_ccy="INR")
        data = self._data()
        self.assertLess(data["latest_pre_money_usd_mn"], 5)
        self.assertEqual(data["latest_pre_money_amount"], 40)
        self.assertEqual(data["latest_pre_money_currency"], "INR")

    def test_an_absent_valuation_stays_absent(self):
        self._round(round_name="Series A", announced_date="2025-03-01")
        data = self._data()
        self.assertIsNone(data["latest_pre_money_usd_mn"])
        self.assertEqual(data["latest_pre_money_display"], "")
        self.assertEqual(data["latest_pre_money_currency"], "")
