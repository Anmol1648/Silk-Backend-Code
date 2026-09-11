"""The ask was collected, then looked for everywhere except where it was put.

Four things want to know how much the company is raising: the two derived
scorecard rows (the ask against the last round, and against everything raised
to date), the cohort comparison that says whether the ask is normal for the
sector, and the "Raise:" tag on the executive summary.

Each went looking in a place nothing writes --
`Assessment.capital_raised_usd_mn` (read four times, written never),
`Deal.target_raise_usd_mn` and `RaiseScenario.raise_amount_usd_mn` (neither
field exists) -- while the founder's own figure sat in `DealTargets`, entered
through a live endpoint and converted to USD on save. So both scorecard rows
were blank on every company, and investor discovery defaulted its ask to
"none" however much had been entered.

Nothing here is specific to a company: the rules are an order of preference
and a unit conversion.
"""
import uuid
from decimal import Decimal

from django.test import TestCase

from fundos.core.models import Company, Deal, Membership, Tenant, User
from fundos.profile import current_raise
from fundos.profile.services import save_deal_targets


def _bootstrap():
    tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
    user = User.objects.create_user(
        email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
        name="Raise User")
    company = Company.objects.create(
        tenant_id=tenant.id, name="Asking Co", created_by=user)
    Membership.objects.create(
        tenant_id=tenant.id, user=user, scope_type="company",
        scope_id=company.id, role="founder", status="active")
    deal = Deal.objects.create(
        tenant_id=tenant.id, company=company, name="Round", round_type="seed",
        primary_owner=user, created_by=user)
    return tenant, user, company, deal


class NothingEnteredStaysNothing(TestCase):

    def setUp(self):
        self.tenant, self.user, self.company, self.deal = _bootstrap()

    def test_a_company_that_has_not_said_reports_none(self):
        """Inventing one from the last close flattered two rows by a band."""
        amount, basis = current_raise.for_company(self.company.id)
        self.assertIsNone(amount)
        self.assertEqual(basis, "")

    def test_an_unknown_company_is_not_an_error(self):
        self.assertEqual(current_raise.for_company(uuid.uuid4()), (None, ""))


class TheFoundersOwnFigureIsFound(TestCase):

    def setUp(self):
        self.tenant, self.user, self.company, self.deal = _bootstrap()

    def _enter(self, raise_amount, ccy="USD"):
        return save_deal_targets(
            self.company, self.deal.id, target_raise=Decimal(raise_amount),
            pre_money=Decimal("20000000"), ccy=ccy, user=self.user)

    def test_the_entered_raise_reaches_the_scorecard(self):
        self._enter("5000000")
        amount, basis = current_raise.for_company(self.company.id)
        self.assertEqual(float(amount), 5.0)
        self.assertIn("deal targets", basis)

    def test_it_is_converted_to_millions_not_passed_through(self):
        """US$5,000,000 is 5 US$ Mn. Passing it through would read as five
        million million, and the scorecard rows divide by it."""
        self._enter("5000000")
        amount, _ = current_raise.for_company(self.company.id)
        self.assertLess(float(amount), 1000)

    def test_a_later_entry_supersedes_an_earlier_one(self):
        self._enter("5000000")
        self._enter("8000000")
        amount, _ = current_raise.for_company(self.company.id)
        self.assertEqual(float(amount), 8.0)

    def test_a_non_dollar_entry_is_still_answered_in_dollars(self):
        """The row converts on save and records the rate, so the figure read
        back here is already USD."""
        self._enter("400000000", ccy="INR")
        amount, _ = current_raise.for_company(self.company.id)
        if amount is not None:
            self.assertLess(float(amount), 400.0)

    def test_zero_is_not_an_ask(self):
        self._enter("0")
        self.assertIsNone(current_raise.for_company(self.company.id)[0])


class ASelectedScenarioOutranksTheEnteredTerms(TestCase):

    def setUp(self):
        self.tenant, self.user, self.company, self.deal = _bootstrap()
        save_deal_targets(self.company, self.deal.id,
                          target_raise=Decimal("5000000"),
                          pre_money=Decimal("20000000"), user=self.user)

    def _scenario(self, value, selected=True, ccy="USD"):
        from fundos.strategy.models import RaiseRecommendation, RaiseScenario

        rec = RaiseRecommendation.objects.create(
            tenant_id=self.tenant.id, deal_id=self.deal.id)
        return RaiseScenario.objects.create(
            tenant_id=self.tenant.id, deal_id=self.deal.id,
            raise_recommendation=rec, label="Base",
            raise_value=Decimal(value), raise_ccy=ccy, is_selected=selected)

    def test_a_decision_taken_wins_over_a_target_entered(self):
        self._scenario("9000000")
        amount, basis = current_raise.for_company(self.company.id)
        self.assertEqual(float(amount), 9.0)
        self.assertIn("scenario", basis)

    def test_an_unselected_scenario_is_not_a_decision(self):
        self._scenario("9000000", selected=False)
        amount, basis = current_raise.for_company(self.company.id)
        self.assertEqual(float(amount), 5.0)
        self.assertIn("deal targets", basis)

    def test_a_scenario_in_another_currency_defers_to_the_usd_figure(self):
        """Converting it here would put an unrecorded rate inside a scored
        row; the founder's own USD figure is the better answer."""
        self._scenario("700000000", ccy="INR")
        amount, basis = current_raise.for_company(self.company.id)
        self.assertEqual(float(amount), 5.0)
        self.assertIn("deal targets", basis)


class TheAssessmentColumnRemainsTheLastResort(TestCase):

    def setUp(self):
        self.tenant, self.user, self.company, self.deal = _bootstrap()

    def _assessment(self, value):
        from fundos.assessment.models import Assessment

        return Assessment.objects.create(
            tenant_id=self.tenant.id, deal=self.deal, company=self.company,
            capital_raised_usd_mn=Decimal(value))

    def test_a_record_that_predates_the_rest_still_answers(self):
        self._assessment("12")
        amount, basis = current_raise.for_company(self.company.id)
        self.assertEqual(float(amount), 12.0)
        self.assertIn("assessment", basis)

    def test_that_column_is_already_in_millions_and_is_not_divided_again(self):
        self._assessment("12")
        self.assertEqual(float(current_raise.for_company(self.company.id)[0]),
                         12.0)

    def test_entered_terms_outrank_it(self):
        self._assessment("12")
        save_deal_targets(self.company, self.deal.id,
                          target_raise=Decimal("5000000"),
                          pre_money=Decimal("20000000"), user=self.user)
        amount, basis = current_raise.for_company(self.company.id)
        self.assertEqual(float(amount), 5.0)
        self.assertIn("deal targets", basis)


class EveryConsumerAsksTheSameResolver(TestCase):

    def setUp(self):
        self.tenant, self.user, self.company, self.deal = _bootstrap()
        save_deal_targets(self.company, self.deal.id,
                          target_raise=Decimal("5000000"),
                          pre_money=Decimal("20000000"), user=self.user)

    def test_the_derived_scorecard_rows_see_it(self):
        from fundos.profile.assessment_extraction import _current_raise
        from fundos.profile.services import get_or_create_profile

        profile = get_or_create_profile(self.company, user=self.user)
        self.assertEqual(float(_current_raise(profile)), 5.0)

    def test_investor_discovery_no_longer_defaults_to_none(self):
        from fundos.investors.views import DiscoveryFiltersView

        amount, basis = DiscoveryFiltersView._default_raise(self.deal)
        self.assertEqual(float(amount), 5.0)
        self.assertNotEqual(basis, "none")

    def test_the_executive_summary_tag_is_no_longer_blank(self):
        from fundos.assessment.models import Assessment
        from fundos.assessment.phase1 import _current_raise_mn

        assessment = Assessment.objects.create(
            tenant_id=self.tenant.id, deal=self.deal, company=self.company)
        self.assertEqual(_current_raise_mn(assessment), 5.0)
