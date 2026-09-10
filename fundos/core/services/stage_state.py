"""
Stage-State Engine — "guide, never enforcer".

C6 / PRD §2.2: stages are NEVER locked. Every stage is visible and openable
at all times. Prerequisites are advisory: reported as met or pending so the
user knows what is outstanding, with only the specific ACTIONS that depend
on missing data disabled — never the page itself.

The strategy stage is the clearest example. A founder who already knows what
they are raising may enter the target raise and valuation directly and move
on to investor discovery without running the guided strategy. The stage
process is not mandatory; the major outputs are.

Stage numbering and labels come from the admin-owned StageDefinition
registry, not from literals scattered through the code.
"""
import logging
from decimal import Decimal

from fundos.core.services.audit import audit

logger = logging.getLogger(__name__)


def _stage_numbers():
    from fundos.platformcfg.services import stage_numbers
    return stage_numbers()


def ensure_stage_states(deal):
    """Create a state row per configured stage. All start available (C6)."""
    from fundos.core.models import StageState

    for n in _stage_numbers():
        StageState.objects.get_or_create(
            deal_id=deal.id, stage_no=n,
            defaults={"tenant_id": deal.tenant_id, "status": "available"})


def unlock_all_stages(deal_id):
    """Migration helper — clear any legacy locked rows."""
    from fundos.core.models import StageState

    return StageState.objects.filter(deal_id=deal_id, status="locked").update(
        status="available")


def hard_gate_met(deal_id, stage_no):
    """Retained for API compatibility.

    Always True — there are no hard gates. Prerequisites are advisory and
    surfaced through `prerequisite_status`.
    """
    return True


def override_stage(deal, stage_no, user):
    """Retained so older clients calling the override endpoint still work.

    Nothing needs overriding now that every stage is open, so this simply
    marks the stage in progress and records the visit.
    """
    from fundos.core.models import StageState

    state, _ = StageState.objects.get_or_create(
        deal_id=deal.id, stage_no=stage_no,
        defaults={"tenant_id": deal.tenant_id})
    if state.status in ("locked", "available"):
        state.status = "in_progress"
        state.save(update_fields=["status", "updated_at"])
    audit("stage.entered", actor=user, deal_id=deal.id,
          entity="stage_state", entity_id=state.id,
          meta={"stage_no": stage_no})
    return state


# --------------------------------------------------------------------------
# Advisory prerequisites (C6)
# --------------------------------------------------------------------------
def prerequisite_status(deal_id, stage_no):
    """Evaluate a stage's prerequisites. Advisory only — never blocking.

    Returns [{key, label, explanation, met}].
    """
    from fundos.platformcfg.services import stages

    definition = next((s for s in stages() if s["stageNo"] == stage_no), None)
    if not definition:
        return []

    out = []
    for prereq in definition.get("prerequisites") or []:
        out.append({
            "key": prereq.get("key", ""),
            "label": prereq.get("label", ""),
            "explanation": prereq.get("explanation", ""),
            "met": _evaluate_prerequisite(deal_id, prereq.get("key", "")),
        })
    return out


def _evaluate_prerequisite(deal_id, key):
    """Evaluate one named prerequisite.

    Unknown keys are treated as met, so a mis-typed admin entry never
    blocks anything.
    """
    try:
        if key == "company_profile_complete":
            from fundos.core.models import Deal
            from fundos.profile.models import CompanyProfile

            deal = Deal.objects.filter(id=deal_id).first()
            if not deal:
                return False
            profile = CompanyProfile.objects.filter(company=deal.company).first()
            return bool(profile and profile.is_complete)

        if key == "strategy_outputs_available":
            # C6: satisfied EITHER by an approved strategy OR by the founder
            # having entered the target raise and valuation directly.
            from fundos.strategy.models import StrategyProfile

            if StrategyProfile.objects.filter(
                    deal_id=deal_id, is_active=True,
                    status="approved").exists():
                return True
            return _manual_outputs_present(deal_id)
    except Exception as e:
        logger.debug("STAGE: prerequisite %s not evaluable: %s", key, e)
        return False
    return True


def _manual_outputs_present(deal_id):
    """True when the founder entered the headline terms directly (C6).

    The two INPUTS are the target raise and the pre-money valuation;
    post-money and dilution are calculated from them, so requiring the
    inputs is sufficient — a complete DealTargets row guarantees all four
    figures are available downstream.
    """
    try:
        from fundos.profile.targets import DealTargets

        row = DealTargets.objects.filter(
            deal_id=deal_id, is_active=True, is_deleted=False).first()
        if row and row.is_complete:
            return True
    except Exception:
        pass

    # Fallback for records written before DealTargets existed.
    try:
        from fundos.core.models import CkbField

        keys = ("target_raise_usd", "pre_money_valuation_usd")
        return CkbField.objects.filter(
            deal_id=deal_id, field_key__in=keys).count() >= len(keys)
    except Exception:
        return False


# --------------------------------------------------------------------------
# Completion
# --------------------------------------------------------------------------
def recompute_stage_completion(deal_id):
    """Recompute completion_pct from live signals. Never a stored flag."""
    from fundos.core.models import StageState

    results = {
        1: _stage1_completion(deal_id),
        2: _stage2_completion(deal_id),
        3: _stage3_completion(deal_id),
    }
    # The tenant is required on the row and was never supplied, so the
    # get_or_create raised IntegrityError the first time a deal needed a
    # StageState created rather than fetched:
    #
    #     null value in column "tenant_id" of relation "stage_state"
    #     violates not-null constraint
    #
    # Every existing deal already had its three rows, so the `get` half always
    # won and the defect only reached a deal created after this code path
    # existed — which is why five tests died in setUp for months while the
    # feature looked fine. It is read off the deal rather than from the
    # ambient tenant context: this runs inside Celery, where that context is
    # not always set, and a row filed under the wrong tenant is worse than one
    # that fails loudly.
    from fundos.core.models import Deal

    tenant_id = (Deal.objects.filter(id=deal_id)
                 .values_list("tenant_id", flat=True).first())

    for stage_no, pct in results.items():
        state, _ = StageState.objects.get_or_create(
            deal_id=deal_id, stage_no=stage_no,
            defaults={"tenant_id": tenant_id})
        state.completion_pct = Decimal(str(round(pct, 2)))
        state.hard_gate_met = True          # no hard gates (C6)
        if pct >= 100:
            state.status = "complete"
        elif pct > 0 and state.status in ("locked", "available"):
            state.status = "in_progress"
        elif state.status == "locked":
            state.status = "available"      # clear legacy locks
        state.save(update_fields=["completion_pct", "hard_gate_met",
                                  "status", "updated_at"])
    return results


def _stage1_completion(deal_id):
    """Stage 1 is the Company Profile — completion is profile completeness
    (C2), which is section-based rather than readiness-based."""
    from fundos.core.models import Deal

    deal = Deal.objects.filter(id=deal_id).first()
    if not deal:
        return 0.0

    try:
        from fundos.profile.models import CompanyProfile
        from fundos.profile.services import recompute_completeness

        profile = CompanyProfile.objects.filter(company=deal.company).first()
        if profile:
            pct = float(recompute_completeness(profile))
            # C2: completeness also requires the owner's review confirmation.
            if pct >= 100 and not profile.reviewed_at:
                return 95.0
            return pct
    except Exception as e:
        logger.debug("STAGE1: profile completion unavailable: %s", e)

    # Fallback for deals predating the profile model.
    try:
        from fundos.config.feature_flags import mandatory_ckb_fields
        from fundos.core.models import CkbField

        mandatory = mandatory_ckb_fields()
        if not mandatory:
            return 0.0
        present = CkbField.objects.filter(
            deal_id=deal_id, field_key__in=mandatory,
        ).exclude(value_text__isnull=True, value_num__isnull=True,
                  value_json__isnull=True).count()
        return min(100.0, 100.0 * present / len(mandatory))
    except Exception:
        return 0.0


def _stage2_completion(deal_id):
    """Strategy completion. C6: the major OUTPUTS matter, not the process,
    so manually entered targets count toward completion."""
    try:
        from fundos.strategy.models import (
            DilutionScenario, FounderObjectives, FundraisingStrategy,
            InstrumentOption, PeerUniverse, RaiseRecommendation,
            StrategyProfile, Valuation,
        )
    except Exception:
        return 0.0

    if StrategyProfile.objects.filter(deal_id=deal_id, is_active=True,
                                      status="approved").exists():
        return 100.0

    # Founder supplied the outputs directly and skipped the guided flow.
    if _manual_outputs_present(deal_id):
        return 100.0

    pct = 0.0
    if FounderObjectives.objects.filter(deal_id=deal_id, is_active=True).exists():
        pct += 15
    if _cash_position_present(deal_id):
        pct += 10
    if PeerUniverse.objects.filter(deal_id=deal_id, is_active=True).exists():
        pct += 15
    if RaiseRecommendation.objects.filter(deal_id=deal_id, is_active=True).exists():
        pct += 15
    if Valuation.objects.filter(deal_id=deal_id, is_active=True).exists():
        pct += 15
    if InstrumentOption.objects.filter(deal_id=deal_id).exists() or \
            DilutionScenario.objects.filter(deal_id=deal_id).exists():
        pct += 10
    if FundraisingStrategy.objects.filter(deal_id=deal_id, is_active=True).exists():
        pct += 15
    return min(pct, 100.0)


def _cash_position_present(deal_id):
    try:
        from fundos.core.models import Deal
        from fundos.profile.models import CashPosition

        deal = Deal.objects.filter(id=deal_id).first()
        if not deal:
            return False
        return CashPosition.objects.filter(company=deal.company,
                                           is_active=True).exists()
    except Exception:
        return False


def _stage3_completion(deal_id):
    """Stage 3 is Investor Discovery in the new journey. The former Stage-3
    materials are retained as Company Profile addenda (C7) and are not part
    of this calculation."""
    try:
        from fundos.materials.models import InvestorPackage
        if InvestorPackage.objects.filter(deal_id=deal_id, is_active=True,
                                          status="approved").exists():
            return 100.0
    except Exception:
        pass
    return 0.0
