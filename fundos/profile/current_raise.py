"""How much the company is raising RIGHT NOW, in US$ millions.

Four things want this number -- two scorecard rows (the ask against the last
round, and against everything raised to date), the cohort comparison that
says whether the ask is normal for the sector, and the "Raise:" tag on the
executive summary -- and each of them went looking for it somewhere nothing
writes:

    Assessment.capital_raised_usd_mn     read in four places, written in none
    Deal.target_raise_usd_mn             no such field
    RaiseScenario.raise_amount_usd_mn    no such field (it is `raise_value`)

Meanwhile the founder's own answer sits in `DealTargets.target_raise_usd`,
entered through a live endpoint and converted to USD on save. So the number
was collected and then looked for everywhere except where it was put, and
two scorecard rows stayed blank on every company.

ORDER OF PREFERENCE, most authoritative first:

    1. The raise scenario the deal has SELECTED -- a decision taken.
    2. The founder's entered headline terms.
    3. The assessment's own column, for a record that predates the rest.

None is a real answer and stays one. A company that has not said what it is
raising cannot have its ask scored, and inventing one from the last close is
how two rows came to flatter a deal by a full band each.
"""
import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

#: `target_raise_usd` and `raise_value` hold WHOLE currency units -- the
#: column is named `_usd`, not `_usd_mn`; post-money is computed as a plain
#: sum with the pre-money entered the same way; and the summary strip renders
#: it with no scale suffix while rendering the assessment column with "M".
#: Three independent signals, and the conversion is done in one place so a
#: wrong reading is one line to correct rather than four.
UNITS_PER_MILLION = Decimal(1_000_000)


def _millions(whole_units):
    """Whole USD -> USD millions, or None."""
    if whole_units is None:
        return None
    try:
        value = Decimal(str(whole_units))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value / UNITS_PER_MILLION


def _from_selected_scenario(company_id):
    """The raise the deal has settled on, if one has been selected."""
    try:
        from fundos.strategy.models import RaiseScenario

        from fundos.core.models import Deal

        # A deal-scoped row carries a deal_id COLUMN, not a foreign key, so
        # the company has to be resolved to its deals first.
        deal_ids = list(Deal.objects.filter(company_id=company_id)
                        .values_list("id", flat=True))
        if not deal_ids:
            return None, ""
        row = (RaiseScenario.objects
               .filter(deal_id__in=deal_ids, is_selected=True,
                       is_deleted=False)
               .exclude(raise_value=None)
               .order_by("-updated_at").first())
    except Exception as exc:              # pragma: no cover - never fatal
        logger.debug("RAISE: scenarios unavailable: %s", exc)
        return None, ""
    if row is None:
        return None, ""
    # A scenario in another currency has not been converted anywhere, and
    # converting it here at today's rate would put an unrecorded rate inside
    # a scored row. The founder's own USD figure is the better answer.
    if (row.raise_ccy or "USD").upper() != "USD":
        return None, ""
    return _millions(row.raise_value), "selected raise scenario"


def _from_deal_targets(company_id):
    """The headline terms the founder entered, in USD as stored."""
    try:
        from fundos.profile.targets import DealTargets

        row = (DealTargets.objects
               .filter(company_id=company_id, is_active=True,
                       is_deleted=False)
               .exclude(target_raise_usd=None)
               .order_by("-updated_at").first())
    except Exception as exc:              # pragma: no cover - never fatal
        logger.debug("RAISE: deal targets unavailable: %s", exc)
        return None, ""
    if row is None:
        return None, ""
    return _millions(row.target_raise_usd), "founder-entered deal targets"


def _from_assessment(company_id):
    """The assessment's own column, already in millions."""
    from fundos.assessment.models import Assessment

    row = (Assessment.objects
           .filter(deal__company_id=company_id)
           .exclude(capital_raised_usd_mn=None)
           .order_by("-created_at").first())
    if row is None:
        return None, ""
    return row.capital_raised_usd_mn, "assessment record"


def for_company(company_id):
    """``(amount_usd_mn, basis)`` -- the basis names where it came from.

    ``(None, "")`` when nothing has been entered, which is a real answer.
    """
    for source in (_from_selected_scenario, _from_deal_targets,
                   _from_assessment):
        amount, basis = source(company_id)
        if amount is not None:
            logger.info("RAISE: US$%.4g Mn from the %s.", float(amount),
                        basis)
            return amount, basis
    return None, ""
