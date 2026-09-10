"""
Stage-3 services (Doc 1 M3): workspace (hard-gated) → story → teaser →
deck → financial model → IM → package review → approved package.

Conventions (Doc 8 §5): E-GATE-423 without an approved Strategy Profile;
every generation snapshots its AI context; documents flow through the
Document framework; [DET] model math is pure and recomputed from
assumptions; guardrails stamp every representational output.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from fundos.core.exceptions import (
    ConflictError, DomainValidationError, GateLocked,
)
from fundos.core.services import recalc
from fundos.core.services.audit import audit

logger = logging.getLogger(__name__)

STORY_COMPONENTS = ["vision", "problem", "current_solutions", "solution",
                    "why_now", "market", "business_model", "advantage",
                    "traction", "growth", "investment", "exit"]

TEASER_MANDATORY = ("overview", "problem", "solution", "fund_raise")

IM_SECTIONS = ["executive_summary", "investment_highlights",
               "company_overview", "founder_story", "market", "industry",
               "product_technology", "business_model", "traction",
               "sales_marketing", "competition", "financial_performance",
               "financial_forecast", "use_of_funds", "valuation_summary",
               "risk_factors"]


def _approved_profile(deal):
    from fundos.strategy.models import StrategyProfile
    profile = StrategyProfile.objects.filter(
        deal_id=deal.id, is_active=True, status="approved").first()
    if not profile:
        raise GateLocked(
            "Stage 3 requires an approved Strategy Profile (BR-M3-000).")
    return profile


@transaction.atomic
def get_or_create_workspace(deal, *, user=None):
    """M3.1 — E-GATE-423 without an approved profile."""
    from fundos.materials.models import DealWorkspace

    profile = _approved_profile(deal)
    workspace = DealWorkspace.objects.filter(deal_id=deal.id).first()
    if workspace:
        if str(workspace.strategy_profile_id) != str(profile.id):
            # Profile re-approved since — retag and flag narrative docs.
            workspace.strategy_profile_id = profile.id
            workspace.save(update_fields=["strategy_profile_id", "updated_at"])
            recalc.propagate_change(deal.id, "strategy_profile",
                                    reason="strategy profile re-approved")
        return workspace
    workspace = DealWorkspace.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id,
        strategy_profile_id=profile.id,
        created_by=user if getattr(user, "pk", None) else None)
    audit("workspace.created", actor=user, deal_id=deal.id,
          entity="deal_workspace", entity_id=workspace.id)
    return workspace


def _make_document(deal, workspace, doc_type, title, content_json, user):
    from fundos.docs.models import Document
    document = Document.objects.filter(deal_id=deal.id, doc_type=doc_type,
                                       workspace_id=workspace.id).first()
    if not document:
        document = Document.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id,
            workspace_id=workspace.id, doc_type=doc_type, title=title,
            origin="fundos_generated",
            created_by=user if getattr(user, "pk", None) else None)
    if document.status in ("Approved", "Locked"):
        # BR-S3-020: approved versions never edited in place — fork.
        document.set_status("In Review", actor=user)
    document.new_version(content_json=content_json,
                         change_summary="AI generation",
                         created_by=user)
    document.set_status("AI Draft Ready", actor=user)
    return document


# ---------------------------------------------------------------------------
# M3.2 Investment story
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_story(deal, *, user=None):
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.materials.models import (
        InvestmentStory, MessageBlock, StoryComponent, StoryPositioningOption,
    )

    workspace = get_or_create_workspace(deal, user=user)
    ctx = llm_context.snapshot("investment_story", deal,
                               workspace_id=workspace.id)
    ai = llm_generate(
        role="investment_story",
        system=("You are a narrative strategist for fundraising. Build the "
                "12-component investment story arc (vision, problem, current "
                "solutions, solution, why now, market, business model, "
                "advantage, traction, growth, investment, exit), at least 3 "
                "positioning options with rationale, a primary thesis, and "
                "message blocks (elevator, intro30, overview2m, overview5m, "
                "ask). Anchor every claim in the given CKB/strategy context — "
                "never invent numbers. Return JSON per the investment_story "
                "schema."),
        prompt="Generate the investment story.",
        context=ctx, deal_id=deal.id, user=user,
        calling_context="materials.story")
    ai = guardrail.apply("investment_story", ai)

    prior = InvestmentStory.objects.filter(deal_id=deal.id,
                                           is_active=True).first()
    version = (prior.version_no + 1) if prior else 1
    if prior:
        prior.is_active = False
        prior.save(update_fields=["is_active", "updated_at"])

    story = InvestmentStory.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, workspace=workspace,
        strategy_profile_id=workspace.strategy_profile_id,
        version_no=version, thesis_primary=ai.get("thesis_primary", ""),
        created_by=user if getattr(user, "pk", None) else None)

    order = {c: i for i, c in enumerate(STORY_COMPONENTS)}
    for item in ai.get("components", []):
        StoryComponent.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, story=story,
            component=item.get("component", "")[:32],
            content=item.get("content", ""),
            evidence=item.get("evidence", []),
            sort_order=order.get(item.get("component"), 99))
    for item in ai.get("positioning_options", []):
        StoryPositioningOption.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, story=story,
            text=item.get("text", ""), rationale=item.get("rationale", ""))
    for item in ai.get("message_blocks", []):
        MessageBlock.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, story=story,
            block_type=item.get("blockType", "")[:16],
            content=item.get("content", ""))

    _make_document(deal, workspace, "story", "Investment Story",
                   {"thesis": story.thesis_primary}, user)
    recalc.clear(deal.id, "investment_story")
    audit("story.generated", actor=user, deal_id=deal.id,
          entity="investment_story", entity_id=story.id,
          meta={"version": version})
    _emit_generated(deal, "story")
    _recompute(deal)
    return story


@transaction.atomic
def approve_story(deal, story, *, user, selected_positioning_id=None):
    from fundos.materials.models import StoryPositioningOption
    if selected_positioning_id:
        option = StoryPositioningOption.objects.filter(
            id=selected_positioning_id, story=story).first()
        if not option:
            raise DomainValidationError("Unknown positioning option.")
        StoryPositioningOption.objects.filter(story=story).update(
            is_selected=False)
        option.is_selected = True
        option.save(update_fields=["is_selected", "updated_at"])
        story.selected_positioning = option.text
    story.status = "approved"
    story.save(update_fields=["status", "selected_positioning", "updated_at"])
    audit("story.approved", actor=user, deal_id=deal.id,
          entity="investment_story", entity_id=story.id)
    _recompute(deal)
    return story


# ---------------------------------------------------------------------------
# M3.3 Teaser
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_teaser(deal, *, user=None):
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.materials.models import Teaser, TeaserSection

    workspace = get_or_create_workspace(deal, user=user)
    _require_story(deal)
    ctx = llm_context.snapshot("teaser", deal, workspace_id=workspace.id)
    ai = llm_generate(
        role="teaser",
        system=("You are drafting a one-to-two page investor teaser from the "
                "approved investment story and strategy profile. Produce a "
                "headline, thesis, tagline and the standard sections "
                "(overview, problem, solution, market, business model, "
                "traction, team, investment opportunity, fund raise, "
                "contact), marking overview/problem/solution/fund_raise "
                "mandatory. Use context numbers verbatim. Return JSON per "
                "the teaser schema."),
        prompt="Generate the investor teaser.",
        context=ctx, deal_id=deal.id, user=user,
        calling_context="materials.teaser")
    ai = guardrail.apply("teaser", ai)

    Teaser.objects.filter(deal_id=deal.id).update(is_deleted=True,
                                                  deleted_at=timezone.now())
    teaser = Teaser.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, workspace=workspace,
        strategy_profile_id=workspace.strategy_profile_id,
        headline=ai.get("headline", ""), thesis=ai.get("thesis", ""),
        tagline=ai.get("tagline", ""),
        created_by=user if getattr(user, "pk", None) else None)
    for i, section in enumerate(ai.get("sections", [])):
        TeaserSection.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, teaser=teaser,
            section_key=section.get("sectionKey", "")[:48],
            title=section.get("title", ""), body=section.get("body", ""),
            is_mandatory=bool(section.get("isMandatory"))
            or section.get("sectionKey") in TEASER_MANDATORY,
            sort_order=i)

    document = _make_document(deal, workspace, "teaser", "Teaser",
                              {"headline": teaser.headline}, user)
    teaser.document_id = document.id
    teaser.save(update_fields=["document_id", "updated_at"])
    recalc.clear(deal.id, "teaser")
    audit("teaser.generated", actor=user, deal_id=deal.id, entity="teaser",
          entity_id=teaser.id)
    _emit_generated(deal, "teaser")
    _recompute(deal)
    return teaser


def _require_story(deal):
    from fundos.materials.models import InvestmentStory
    if not InvestmentStory.objects.filter(deal_id=deal.id,
                                          is_active=True).exists():
        raise ConflictError(
            "Generate the investment story first — it is the single source "
            "of narrative truth (BR-M3-020).")


# ---------------------------------------------------------------------------
# M3.4 Pitch deck — story-first, two steps
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_deck_outline(deal, *, user=None, template_code="",
                          slide_count=None):
    """Step 1: narrative outline for founder approval (BR-M3-040).

    `slide_count` (TC-081): the founder's requested deck length is passed
    to the generator and, as a deterministic backstop, an over-long outline
    is trimmed to the requested count (mandatory sections are ordered first
    by the generator, so trimming from the tail is safe).
    """
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.materials.models import PitchDeck

    workspace = get_or_create_workspace(deal, user=user)
    _require_story(deal)
    ctx = llm_context.snapshot("pitch_story", deal, workspace_id=workspace.id)
    if template_code:
        ctx["template_code"] = template_code
    if slide_count:
        ctx["requested_slide_count"] = int(slide_count)
    ai = llm_generate(
        role="pitch_story",
        system=("You are structuring a pitch deck narrative from the "
                "approved investment story. Return the slide-level outline "
                "as JSON per the pitch_story schema — story first, slides "
                "second."),
        prompt="Produce the pitch narrative outline.",
        context=ctx, deal_id=deal.id, user=user,
        calling_context="materials.deck.outline")
    ai = guardrail.apply("pitch_story", ai)

    outline = ai.get("narrative_outline", [])
    if slide_count and len(outline) > int(slide_count):
        outline = outline[:int(slide_count)]

    PitchDeck.objects.filter(deal_id=deal.id).update(
        is_deleted=True, deleted_at=timezone.now())
    deck = PitchDeck.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, workspace=workspace,
        strategy_profile_id=workspace.strategy_profile_id,
        narrative_outline=outline,
        created_by=user if getattr(user, "pk", None) else None)
    audit("deck.outline.generated", actor=user, deal_id=deal.id,
          entity="pitch_deck", entity_id=deck.id)
    return deck


@transaction.atomic
def generate_deck_slides(deal, deck, *, user=None):
    """Step 2: slides — only after outline approval (BR-M3-040)."""
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.materials.models import Slide

    if not deck.outline_approved:
        # Documented as a gate (E-GATE-423), not a generic 409 (TC-080).
        from fundos.core.exceptions import GateLocked
        raise GateLocked("Approve the narrative outline before slides "
                         "are generated (story-first).")
    ctx = llm_context.snapshot("pitch_slides", deal,
                               workspace_id=deck.workspace_id)
    ctx["narrative_outline"] = deck.narrative_outline
    ai = llm_generate(
        role="pitch_slides",
        system=("You are producing slide content from the APPROVED narrative "
                "outline. One message per slide: headline, narrative, key "
                "metrics (from context only), optional chart spec, speaker "
                "notes and investor takeaway. Return JSON per the "
                "pitch_slides schema."),
        prompt="Generate slide content for the approved outline.",
        context=ctx, deal_id=deal.id, user=user,
        calling_context="materials.deck.slides")
    ai = guardrail.apply("pitch_slides", ai)

    Slide.objects.filter(deck=deck).delete()
    for i, s in enumerate(ai.get("slides", [])):
        Slide.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, deck=deck,
            slide_type=s.get("slideType", "")[:32],
            headline=s.get("headline", ""), narrative=s.get("narrative", ""),
            key_metrics=s.get("keyMetrics", []),
            chart_spec=s.get("chartSpec"), icons=s.get("icons", []),
            speaker_notes=s.get("speakerNotes", ""),
            investor_takeaway=s.get("investorTakeaway", ""), sort_order=i)

    from fundos.materials.models import DealWorkspace
    workspace = DealWorkspace.objects.get(id=deck.workspace_id)
    document = _make_document(deal, workspace, "deck", "Pitch Deck",
                              {"slides": len(ai.get("slides", []))}, user)
    deck.document_id = document.id
    deck.save(update_fields=["document_id", "updated_at"])
    recalc.clear(deal.id, "pitch_deck")
    audit("deck.slides.generated", actor=user, deal_id=deal.id,
          entity="pitch_deck", entity_id=deck.id)
    _emit_generated(deal, "deck")
    _recompute(deal)
    return deck


# ---------------------------------------------------------------------------
# M3.5 Financial model — [DET] three-statement math
# ---------------------------------------------------------------------------

DEFAULT_ASSUMPTIONS = [
    ("revenue", "new_customers_per_month", 5, "count"),
    ("revenue", "arpa_monthly", 100000, "INR"),
    ("revenue", "monthly_churn_pct", 2, "%"),
    ("revenue", "gross_margin_pct", 70, "%"),
    ("hiring", "starting_monthly_payroll", 2000000, "INR"),
    ("hiring", "monthly_hiring_cost_growth_pct", 3, "%"),
    ("opex", "other_opex_pct_of_revenue", 25, "%"),
    ("capex", "monthly_capex", 100000, "INR"),
    ("wc", "starting_cash", 30000000, "INR"),
]


@transaction.atomic
def generate_financial_model(deal, *, user=None, horizon_months=36):
    """AI proposes assumptions (fin_model_assist), engine computes the
    3-statement output; assumptions live separately from statements
    (BR-M3-050) and every assumption is editable."""
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.materials.models import (
        FinancialAssumption, FinancialModel, FinancialScenario,
    )

    workspace = get_or_create_workspace(deal, user=user)
    FinancialModel.objects.filter(deal_id=deal.id).update(
        is_deleted=True, deleted_at=timezone.now())
    model = FinancialModel.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, workspace=workspace,
        strategy_profile_id=workspace.strategy_profile_id,
        horizon_months=horizon_months,
        created_by=user if getattr(user, "pk", None) else None)

    assumptions = {(g, k): (v, u, "ai_default_peer")
                   for g, k, v, u in DEFAULT_ASSUMPTIONS}
    try:
        ctx = llm_context.snapshot("fin_model_assist", deal,
                                   workspace_id=workspace.id)
        ai = llm_generate(
            role="fin_model_assist",
            system=("You suggest starting financial-model assumptions "
                    "benchmarked from the peer context. Return JSON per the "
                    "fin_model_assist schema: assumptions[{group, key, "
                    "value_num, unit, source}]. Suggest values only for "
                    "groups revenue/hiring/opex/capex/wc."),
            prompt="Propose starting assumptions.",
            context=ctx, deal_id=deal.id, user=user,
            calling_context="materials.model.assumptions")
        ai = guardrail.apply("fin_model_assist", ai)
        for item in ai.get("assumptions", []):
            key = (item.get("group", ""), item.get("key", ""))
            if item.get("value_num") is not None:
                assumptions[key] = (item["value_num"], item.get("unit", ""),
                                    item.get("source", "ai_default_peer"))
    except Exception as e:
        logger.error("MODEL: assumption assist failed (defaults kept): %s", e)

    for (group, key), (value, unit, source) in assumptions.items():
        FinancialAssumption.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, model=model,
            group=group, key=key, value_num=Decimal(str(value)), unit=unit,
            source=source)

    for name, adj in (("base", {}), ("upside", {"growth_mult": 1.3}),
                      ("downside", {"growth_mult": 0.7})):
        FinancialScenario.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, model=model,
            name=name, adjustments=adj)

    recompute_model(deal, model, user=user)
    document = _make_document(deal, workspace, "model", "Financial Model",
                              {"horizonMonths": horizon_months}, user)
    model.document_id = document.id
    model.save(update_fields=["document_id", "updated_at"])
    recalc.clear(deal.id, "financial_model")
    audit("model.generated", actor=user, deal_id=deal.id,
          entity="financial_model", entity_id=model.id)
    _emit_generated(deal, "model")
    _recompute(deal)
    return model


def recompute_model(deal, model, *, user=None):
    """[DET] pure recompute of P&L / cash lines + KPIs from assumptions.
    Deleting and rebuilding statement lines keeps math reproducible."""
    from fundos.materials.models import (
        FinancialAssumption, FinancialKpi, FinancialScenario,
        FinancialStatementLine,
    )

    values = {(a.group, a.key): float(a.value_num or 0)
              for a in FinancialAssumption.objects.filter(model=model)}

    def v(group, key, default=0.0):
        return values.get((group, key), default)

    FinancialStatementLine.objects.filter(model=model).delete()
    FinancialKpi.objects.filter(model=model).delete()

    for scenario in FinancialScenario.objects.filter(model=model):
        mult = float(scenario.adjustments.get("growth_mult", 1.0))
        customers = 0.0
        cash = v("wc", "starting_cash")
        payroll = v("hiring", "starting_monthly_payroll")
        lines, kpis = [], []
        for month in range(1, model.horizon_months + 1):
            new = v("revenue", "new_customers_per_month") * mult
            churn = customers * v("revenue", "monthly_churn_pct") / 100.0
            customers = max(customers + new - churn, 0.0)
            revenue = customers * v("revenue", "arpa_monthly")
            cogs = revenue * (1 - v("revenue", "gross_margin_pct") / 100.0)
            payroll *= (1 + v("hiring", "monthly_hiring_cost_growth_pct") / 100.0)
            other_opex = revenue * v("opex", "other_opex_pct_of_revenue") / 100.0
            capex = v("capex", "monthly_capex")
            ebitda = revenue - cogs - payroll - other_opex
            net_cash_flow = ebitda - capex
            cash += net_cash_flow
            burn = -net_cash_flow if net_cash_flow < 0 else 0.0
            runway = (cash / burn) if burn > 0 else 999.0

            def line(statement, key, value):
                lines.append(FinancialStatementLine(
                    tenant_id=deal.tenant_id, deal_id=deal.id, model=model,
                    scenario=scenario.name, statement=statement,
                    line_key=key, month_index=month,
                    value=Decimal(str(round(value, 2)))))

            line("pl", "revenue", revenue)
            line("pl", "cogs", cogs)
            line("pl", "payroll", payroll)
            line("pl", "other_opex", other_opex)
            line("pl", "ebitda", ebitda)
            line("cf", "capex", capex)
            line("cf", "net_cash_flow", net_cash_flow)
            line("bs", "cash", cash)

            for kpi_key, kpi_value in (("arr", revenue * 12),
                                       ("customers", customers),
                                       ("burn", burn),
                                       ("runway_months", min(runway, 999))):
                kpis.append(FinancialKpi(
                    tenant_id=deal.tenant_id, deal_id=deal.id, model=model,
                    scenario=scenario.name, kpi_key=kpi_key,
                    month_index=month,
                    value=Decimal(str(round(kpi_value, 4)))))
        FinancialStatementLine.objects.bulk_create(lines)
        FinancialKpi.objects.bulk_create(kpis)
    return model


@transaction.atomic
def update_assumption(deal, model, *, group, key, value_num, user=None):
    """Edit → recompute → mark dependents (BR-M3-051 sensitivity path)."""
    from fundos.materials.models import FinancialAssumption
    assumption = FinancialAssumption.objects.filter(
        model=model, group=group, key=key).first()
    if not assumption:
        raise DomainValidationError("Unknown assumption.",
                                    fields={"key": "unknown"})
    assumption.value_num = Decimal(str(value_num))
    assumption.source = "founder"
    assumption.save(update_fields=["value_num", "source", "updated_at"])
    recompute_model(deal, model, user=user)
    recalc.propagate_change(deal.id, "financial_model",
                            reason=f"assumption {group}.{key} changed")
    recalc.clear(deal.id, "financial_model")
    audit("model.assumption.updated", actor=user, deal_id=deal.id,
          entity="financial_model", entity_id=model.id,
          meta={"group": group, "key": key})
    return assumption


# ---------------------------------------------------------------------------
# M3.6 Investment memorandum
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_im(deal, *, user=None):
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.materials.models import ImSection, InvestmentMemorandum

    workspace = get_or_create_workspace(deal, user=user)
    _require_story(deal)
    ctx = llm_context.snapshot("im_section", deal, workspace_id=workspace.id)
    ctx["section_keys"] = IM_SECTIONS
    ai = llm_generate(
        role="im_section",
        system=("You are drafting a comprehensive investment memorandum "
                "with the 16 mandatory sections given in section_keys. Each "
                "section must be consistent with the story, strategy and "
                "financial context; numbers verbatim from context. Return "
                "JSON per the im_section schema."),
        prompt="Draft the investment memorandum sections.",
        context=ctx, deal_id=deal.id, user=user,
        calling_context="materials.im")
    ai = guardrail.apply("im_section", ai)

    InvestmentMemorandum.objects.filter(deal_id=deal.id).update(
        is_deleted=True, deleted_at=timezone.now())
    im = InvestmentMemorandum.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, workspace=workspace,
        strategy_profile_id=workspace.strategy_profile_id,
        created_by=user if getattr(user, "pk", None) else None)
    order = {k: i for i, k in enumerate(IM_SECTIONS)}
    for section in ai.get("sections", []):
        ImSection.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, im=im,
            section_key=section.get("sectionKey", "")[:48],
            title=section.get("title", ""), body=section.get("body", ""),
            is_mandatory=True,
            sort_order=order.get(section.get("sectionKey"), 99))

    # Consistency pass (im_consistency role) — flags, not silent fixes.
    try:
        check = llm_generate(
            role="im_consistency",
            system=("Compare the IM sections with the CKB and financial "
                    "context. Flag numeric or narrative inconsistencies as "
                    "{fieldKey, message, severity}. Return JSON per the "
                    "im_consistency schema."),
            prompt="Run the IM consistency check.",
            context={**ctx, "sections": ai.get("sections", [])[:16]},
            deal_id=deal.id, user=user,
            calling_context="materials.im.consistency")
        flags = check.get("flags", [])
        if flags:
            first = im.sections.order_by("sort_order").first()
            if first:
                first.consistency_flags = flags
                first.save(update_fields=["consistency_flags", "updated_at"])
    except Exception as e:
        logger.error("IM: consistency check failed: %s", e)

    document = _make_document(deal, workspace, "im", "Investment Memorandum",
                              {"sections": len(ai.get("sections", []))}, user)
    im.document_id = document.id
    im.save(update_fields=["document_id", "updated_at"])
    recalc.clear(deal.id, "investment_memorandum")
    audit("im.generated", actor=user, deal_id=deal.id,
          entity="investment_memorandum", entity_id=im.id)
    _emit_generated(deal, "im")
    _recompute(deal)
    return im


# ---------------------------------------------------------------------------
# M3.7 Package review + objection simulation
# ---------------------------------------------------------------------------

@transaction.atomic
def run_package_review(deal, *, user=None):
    """[DET] cross-document numeric validation + [AI] six-category review +
    objection simulation (Doc 1 M3-§8)."""
    from fundos.engines.dilution_engine import crossdoc_inconsistencies
    from fundos.engines.scoring import active_scoring_config
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.materials.models import (
        CrossDocInconsistency, InvestorObjection, PackageReview,
        ReviewFinding,
    )

    workspace = get_or_create_workspace(deal, user=user)
    cfg, cfg_id = active_scoring_config()

    # [DET] gather per-document numeric claims and cross-validate.
    values_by_doc = _collect_numeric_claims(deal)
    det_findings = crossdoc_inconsistencies(values_by_doc,
                                            cfg["crossdoc_tolerances"])

    ctx = llm_context.snapshot("package_review", deal,
                               workspace_id=workspace.id)
    ctx["deterministic_inconsistencies"] = det_findings
    ai = llm_generate(
        role="package_review",
        system=("You are a senior banker running the pre-launch package "
                "review across six categories: story, commercial, financial, "
                "presentation, investment, completeness. Produce category "
                "notes and prioritised recommendations {priority, category, "
                "reason, business_impact, affected_documents, "
                "estimated_improvement}. Return JSON per the package_review "
                "schema."),
        prompt="Review the investor package.",
        context=ctx, deal_id=deal.id, user=user,
        calling_context="materials.review")
    ai = guardrail.apply("package_review", ai)

    review = PackageReview.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, workspace=workspace,
        strategy_profile_id=workspace.strategy_profile_id,
        category_notes=ai.get("category_notes", {}),
        risk_summary=ai.get("risk_summary", []),
        outstanding_actions=ai.get("outstanding_actions", []),
        scoring_config_version_id=cfg_id,
        created_by=user if getattr(user, "pk", None) else None)
    for item in ai.get("recommendations", []):
        ReviewFinding.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, review=review,
            priority=item.get("priority", "med")[:8],
            category=item.get("category", "")[:24],
            reason=item.get("reason", ""),
            business_impact=item.get("business_impact", ""),
            affected_documents=item.get("affected_documents", []),
            estimated_improvement=item.get("estimated_improvement", "")[:128])
    for finding in det_findings:
        correction = finding["recommended_correction"]
        # Tester Issue 6: after a CKB edit the founder regenerated raise +
        # valuation but the review kept comparing against the (correctly)
        # STALE financial model — and the generic "fix the source facts"
        # advice sent them in circles. When a compared document's artefact
        # is pending recalculation, the correction now names the document
        # and the exact fix.
        _DOC_ARTIFACTS = {"model": ("financial_model", "Financial Model"),
                          "im": ("investment_memorandum",
                                 "Investment Memorandum")}
        for v in finding.get("values", []):
            artefact = _DOC_ARTIFACTS.get(str(v.get("document", "")).lower())
            if artefact and recalc.status_of(deal.id, artefact[0]) in (
                    "pending_recalc", "needs_review"):
                correction += (f" The {artefact[1]} was generated before the "
                               f"latest knowledge-base change — regenerate "
                               f"the {artefact[1]}, then re-run this review.")
                break
        CrossDocInconsistency.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, review=review,
            field_key=finding["field_key"], values=finding["values"],
            recommended_correction=correction)

    # Objection simulation (objection_sim role).
    try:
        objections = llm_generate(
            role="objection_sim",
            system=("Simulate the toughest investor objections for this "
                    "package by investor type, with reasons, supporting "
                    "data references, suggested responses, evidence, "
                    "confidence and priority. Return JSON per the "
                    "objection_sim schema."),
            prompt="Simulate investor objections.",
            context=ctx, deal_id=deal.id, user=user,
            calling_context="materials.review.objections")
        for item in objections.get("objections", []):
            InvestorObjection.objects.create(
                tenant_id=deal.tenant_id, deal_id=deal.id, review=review,
                investor_type=item.get("investor_type", "")[:32],
                question=item.get("question", ""),
                reason=item.get("reason", ""),
                supporting_data=item.get("supporting_data", {}),
                suggested_response=item.get("suggested_response", ""),
                evidence=item.get("evidence", []),
                confidence=item.get("confidence", "")[:8],
                priority=item.get("priority", "")[:8])
    except Exception as e:
        logger.error("REVIEW: objection simulation failed: %s", e)

    audit("package.reviewed", actor=user, deal_id=deal.id,
          entity="package_review", entity_id=review.id,
          meta={"detFindings": len(det_findings)})
    from fundos.core.alerting.tasks import emit_alert
    emit_alert("fundos.review.complete", {
        "deal_id": str(deal.id), "company": deal.company.name,
        "findings": len(det_findings), "tenant_id": str(deal.tenant_id)})
    _recompute(deal)
    return review


def _collect_numeric_claims(deal):
    """Pull comparable numeric claims from CKB / model / strategy for the
    [DET] cross-document check."""
    from fundos.core.services import ckb_service
    from fundos.materials.models import FinancialKpi, FinancialModel

    values = {}
    flat = {}
    for group in ckb_service.snapshot(deal.id).values():
        for key, meta in group.items():
            flat[key] = meta["value"]
    for field_key in ("arr", "revenue", "burn", "employees"):
        if flat.get(field_key) is not None:
            values.setdefault(field_key, []).append(
                {"document": "ckb", "value": flat[field_key]})

    model = FinancialModel.objects.filter(deal_id=deal.id).first()
    if model:
        kpi = FinancialKpi.objects.filter(model=model, scenario="base",
                                          kpi_key="arr",
                                          month_index=1).first()
        if kpi:
            values.setdefault("arr", []).append(
                {"document": "model", "value": float(kpi.value)})
    return values


# ---------------------------------------------------------------------------
# M3.8 Investor package approval
# ---------------------------------------------------------------------------

@transaction.atomic
def approve_investor_package(deal, *, user):
    """BR-M3-080: freeze the approved baseline — document version ids are
    pinned; later edits create new versions without touching the package."""
    from fundos.docs.models import Document
    from fundos.materials.models import InvestorPackage, PackageReview

    workspace = get_or_create_workspace(deal, user=user)
    latest_review = (PackageReview.objects.filter(deal_id=deal.id)
                     .order_by("-created_at").first())
    if not latest_review:
        raise ConflictError("Run the package review before approval.")

    # QA BUG-022: the spec blocks approval while high-priority issues
    # remain unresolved — the review already detects them; enforce here.
    unresolved = [i for i in latest_review.inconsistencies.filter(
        resolved=False)]
    if unresolved:
        # Tester Issue 6: name the documents actually involved and the
        # stale ones, so the founder regenerates the right artefact instead
        # of looping on raise/valuation.
        involved = sorted({str(v.get("document", "")).lower()
                           for i in unresolved for v in (i.values or [])
                           if v.get("document")})
        _LABELS = {"ckb": "Knowledge Base", "model": "Financial Model",
                   "im": "Investment Memorandum", "teaser": "Teaser",
                   "deck": "Pitch Deck"}
        stale = [label for artefact, label in (
            ("financial_model", "Financial Model"),
            ("investment_memorandum", "Investment Memorandum"),
            ("teaser", "Teaser"), ("pitch_deck", "Pitch Deck"))
            if recalc.status_of(deal.id, artefact) in ("pending_recalc",
                                                       "needs_review")]
        msg = ("Approval is blocked: the package review found unresolved "
               "cross-document inconsistencies between "
               + " and ".join(_LABELS.get(d, d) for d in involved) + ".")
        if stale:
            msg += (" Out of date with their inputs: " + ", ".join(stale)
                    + " — regenerate "
                    + ("it" if len(stale) == 1 else "them")
                    + ", re-run the review, then approve.")
        else:
            msg += (" Fix the underlying facts (usually in the knowledge "
                    "base), regenerate the affected documents, and re-run "
                    "the review.")
        raise ConflictError(
            msg,
            fields={"unresolvedInconsistencies":
                    [i.field_key for i in unresolved],
                    "staleDocuments": stale})
    mandatory = ("teaser", "deck", "model", "im")
    documents = {d.doc_type: d for d in Document.objects.filter(
        deal_id=deal.id, doc_type__in=mandatory,
        workspace_id=workspace.id)}
    missing = [t for t in mandatory if t not in documents]
    if missing:
        raise ConflictError("Mandatory documents missing.",
                            fields={"missing": missing})

    prior = InvestorPackage.objects.filter(deal_id=deal.id,
                                           is_active=True).first()
    version = (prior.version_no + 1) if prior else 1
    if prior:
        prior.is_active = False
        prior.save(update_fields=["is_active", "updated_at"])

    pinned = []
    for doc_type in mandatory:
        document = documents[doc_type]
        document.set_status("Approved", actor=user)
        pinned.append({"docType": doc_type,
                       "documentId": str(document.id),
                       "versionId": str(document.current_version_id)})

    package = InvestorPackage.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, workspace=workspace,
        strategy_profile_id=workspace.strategy_profile_id,
        version_no=version, status="approved",
        document_version_ids=pinned, approved_by=user,
        approved_at=timezone.now(), created_by=user)

    audit("package.approved", actor=user, deal_id=deal.id,
          entity="investor_package", entity_id=package.id,
          meta={"version": version})
    from fundos.core.alerting.tasks import emit_alert
    emit_alert("fundos.package.approved", {
        "deal_id": str(deal.id), "company": deal.company.name,
        "version": version, "tenant_id": str(deal.tenant_id)})
    _recompute(deal)
    return package


def _emit_generated(deal, what):
    from fundos.core.alerting.tasks import emit_alert
    emit_alert("fundos.doc.generated", {
        "deal_id": str(deal.id), "company": deal.company.name,
        "doc_type": what, "tenant_id": str(deal.tenant_id)})


def _recompute(deal):
    from fundos.core.services.stage_state import recompute_stage_completion
    recompute_stage_completion(deal.id)
