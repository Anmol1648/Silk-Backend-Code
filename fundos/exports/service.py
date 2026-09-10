"""
Export service (Gaps G4/G14): pulls the active artefact, renders it to the
requested format, stores it in the object bucket under
exports/{deal}/{artifact}-{ts}.{ext}, records a Document/AccessLog-friendly
audit entry and returns a short-lived signed download URL.
"""
import logging
import os
import tempfile

from django.utils import timezone

from fundos.core.exceptions import DomainValidationError, NotFoundInDeal
from fundos.core.services.audit import audit
from fundos.core.services.currency import fx_block, present_money
from fundos.exports import renderers

logger = logging.getLogger(__name__)

SIGNED_URL_TTL_SEC = 900


def _fmt_money(usd_value, basis=""):
    m = present_money(usd_value, basis=basis)
    if m.get("value") is None:
        return "—"
    return f"{m['ccy']} {m['value']:,.0f}"


def _store_and_sign(deal, local_path, artifact, ext, *, user=None):
    from fundos.docs.storage import get_storage

    ts = timezone.now().strftime("%Y%m%d-%H%M%S")
    remote = f"exports/{deal.id}/{artifact}-{ts}.{ext}"
    if not get_storage().upload_file(local_path, remote):
        raise DomainValidationError("Export storage failed — retry.")
    url = get_storage().signed_url(remote, expires_sec=SIGNED_URL_TTL_SEC)
    audit(f"export.{artifact}", actor=user, deal_id=deal.id,
          entity="export", meta={"format": ext, "storageUri": remote})
    try:
        os.unlink(local_path)
    except OSError:
        pass
    return {"downloadUrl": url, "storageUri": remote, "format": ext,
            "expiresInSeconds": SIGNED_URL_TTL_SEC}


def _tmp(ext):
    fd, path = tempfile.mkstemp(suffix=f".{ext}")
    os.close(fd)
    return path


def _require(obj, message):
    if not obj:
        raise NotFoundInDeal(message)
    return obj


# ---------------------------------------------------------------------------
# Teaser
# ---------------------------------------------------------------------------

def _teaser_content(deal):
    from fundos.materials.models import Teaser
    teaser = _require(Teaser.objects.filter(deal_id=deal.id).first(),
                      "No teaser yet — generate it first.")
    sections = [(s.title or s.section_key,
                 [s.body]) for s in teaser.sections.all()]
    return teaser, sections


def export_teaser(deal, fmt, *, user=None):
    teaser, sections = _teaser_content(deal)
    subtitle = teaser.thesis or teaser.tagline
    if fmt == "pdf":
        path = renderers.render_pdf(
            _tmp("pdf"), title=teaser.headline or f"{deal.company.name} — "
                                                  f"Investment Teaser",
            subtitle=subtitle, sections=sections,
            disclaimer="Confidential. Prepared with FundOS.")
    elif fmt == "pptx":
        path = renderers.render_pptx(
            _tmp("pptx"), title=teaser.headline or deal.company.name,
            subtitle=subtitle,
            slides=[{"headline": h, "bullets": body} for h, body in sections])
    elif fmt == "docx":
        path = renderers.render_docx(
            _tmp("docx"), title=teaser.headline or deal.company.name,
            subtitle=subtitle, sections=sections,
            disclaimer="Confidential. Prepared with FundOS.")
    else:
        raise DomainValidationError("format must be pdf|pptx|docx.",
                                    fields={"format": "invalid"})
    return _store_and_sign(deal, path, "teaser", fmt, user=user)


# ---------------------------------------------------------------------------
# Pitch deck
# ---------------------------------------------------------------------------

def export_deck(deal, fmt, *, user=None):
    from fundos.materials.models import PitchDeck, Slide
    deck = _require(PitchDeck.objects.filter(deal_id=deal.id).first(),
                    "No pitch deck yet — generate it first.")
    slides = list(Slide.objects.filter(deck=deck))   # Meta orders by sort_order
    _require(slides, "The deck has no slides yet — approve the outline and "
                     "generate slides first.")
    content = [{
        "headline": s.headline,
        "bullets": [m if isinstance(m, str)
                    else f"{m.get('label', '')}: {m.get('value', '')}"
                    for m in (s.key_metrics or [])],
        "narrative": s.narrative,
        "speakerNotes": s.speaker_notes,
    } for s in slides]
    if fmt == "pptx":
        path = renderers.render_pptx(
            _tmp("pptx"), title=f"{deal.company.name} — Pitch Deck",
            subtitle=deal.name, slides=content)
    elif fmt in ("pdf", "notes"):
        sections = [(c["headline"] or f"Slide {i + 1}",
                     ([c["narrative"]] if fmt == "pdf"
                      else [c["narrative"], c["speakerNotes"]])
                     + c["bullets"])
                    for i, c in enumerate(content)]
        path = renderers.render_pdf(
            _tmp("pdf"), title=f"{deal.company.name} — Pitch Deck"
                               + (" (speaker notes)" if fmt == "notes" else ""),
            subtitle=deal.name, sections=sections,
            disclaimer="Confidential. Prepared with FundOS.")
        fmt = "pdf"
    else:
        raise DomainValidationError("format must be pptx|pdf|notes.",
                                    fields={"format": "invalid"})
    return _store_and_sign(deal, path, "deck", fmt, user=user)


# ---------------------------------------------------------------------------
# Financial model
# ---------------------------------------------------------------------------

def _model_data(deal):
    from collections import OrderedDict

    from fundos.materials.models import (
        FinancialAssumption, FinancialKpi, FinancialModel,
        FinancialStatementLine,
    )
    model = _require(FinancialModel.objects.filter(deal_id=deal.id).first(),
                     "No financial model yet — generate it first.")
    assumptions = [{
        "key": a.key, "label": f"{a.group} · {a.key}",
        "value": float(a.value_num) if a.value_num is not None else "",
        "unit": a.unit,
    } for a in FinancialAssumption.objects.filter(model=model)]

    # Statement lines are one row per month — pivot to per-line series.
    statements = {}
    lines = FinancialStatementLine.objects.filter(
        model=model, scenario="base").order_by("statement", "line_key",
                                               "month_index")
    grouped = OrderedDict()
    for row in lines:
        grouped.setdefault((row.statement, row.line_key), []).append(
            float(row.value))
    names = {"pl": "P&L", "bs": "Balance Sheet", "cf": "Cash Flow"}
    for (statement, line_key), values in grouped.items():
        statements.setdefault(names.get(statement, statement), []).append(
            {"lineKey": line_key, "label": line_key.replace("_", " ").title(),
             "values": values})

    kpi_grouped = OrderedDict()
    for k in FinancialKpi.objects.filter(model=model, scenario="base") \
                                 .order_by("kpi_key", "-month_index"):
        kpi_grouped.setdefault(k.kpi_key, k)   # latest month per KPI
    kpis = [{"key": key, "label": key.replace("_", " ").upper(),
             "value": float(k.value), "period": f"M{k.month_index}"}
            for key, k in kpi_grouped.items()]
    return model, assumptions, statements, kpis


def export_model(deal, fmt, *, user=None):
    model, assumptions, statements, kpis = _model_data(deal)
    if fmt == "xlsx":
        path = renderers.render_model_xlsx(
            _tmp("xlsx"), assumptions=assumptions, statements=statements,
            kpis=kpis)
    elif fmt == "csv":
        rows = [["Statement", "Line"]
                + [f"P{i + 1}" for i in range(
                    max((len(l["values"]) for lines in statements.values()
                         for l in lines), default=0))]]
        for name, lines in statements.items():
            for line in lines:
                rows.append([name, line["label"]] + line["values"])
        path = renderers.render_csv(_tmp("csv"), rows)
    elif fmt == "pdf":
        tables = [(name, [["Line"]
                          + [f"P{i + 1}" for i in range(
                              max((len(l["values"]) for l in lines),
                                  default=0))]]
                   + [[l["label"]] + [f"{v:,.0f}" if isinstance(v, (int, float))
                                      else v for v in l["values"]]
                      for l in lines])
                  for name, lines in statements.items()]
        path = renderers.render_pdf(
            _tmp("pdf"), title=f"{deal.company.name} — Financial Model",
            subtitle=f"Deterministic 3-statement output · {deal.name}",
            sections=[("Assumptions",
                       [f"{a['label']}: {a['value']} {a['unit']}".strip()
                        for a in assumptions])],
            tables=tables,
            disclaimer="Deterministic model output. Confidential.")
    else:
        raise DomainValidationError("format must be xlsx|csv|pdf.",
                                    fields={"format": "invalid"})
    return _store_and_sign(deal, path, "model", fmt, user=user)


# ---------------------------------------------------------------------------
# Investment memorandum
# ---------------------------------------------------------------------------

def export_im(deal, fmt, *, user=None):
    from fundos.materials.models import InvestmentMemorandum
    im = _require(InvestmentMemorandum.objects.filter(deal_id=deal.id).first(),
                  "No investment memorandum yet — generate it first.")
    sections = [(s.title or s.section_key, [s.body])
                for s in im.sections.all()]
    watermark = ""
    if fmt == "watermarked":
        watermark, fmt = "CONFIDENTIAL", "pdf"
    if fmt == "pdf":
        path = renderers.render_pdf(
            _tmp("pdf"),
            title=f"{deal.company.name} — Investment Memorandum",
            subtitle=deal.name, sections=sections,
            disclaimer="Confidential. Not an offer of securities. "
                       "Prepared with FundOS.",
            watermark=watermark)
    elif fmt == "docx":
        path = renderers.render_docx(
            _tmp("docx"),
            title=f"{deal.company.name} — Investment Memorandum",
            subtitle=deal.name, sections=sections,
            disclaimer="Confidential. Not an offer of securities.")
    else:
        raise DomainValidationError("format must be pdf|docx|watermarked.",
                                    fields={"format": "invalid"})
    return _store_and_sign(deal, path, "im", fmt, user=user)


# ---------------------------------------------------------------------------
# Reports — readiness / strategy / review (PDF)
# ---------------------------------------------------------------------------

def export_readiness_report(deal, *, user=None):
    """Tester Issue 11: the exported PDF was a one-page stub (summary + a
    bare Dimension/Status table). It now mirrors the full on-screen
    assessment: numeric score, per-dimension explanation + evidence, the
    gap analysis grouped by kind, and the prioritised recommendations."""
    from fundos.readiness.models import (
        ReadinessAssessment, ReadinessDimension, ReadinessGap,
    )
    assessment = _require(
        ReadinessAssessment.objects.filter(deal_id=deal.id,
                                           is_active=True).first(),
        "No readiness assessment yet — generate it first.")
    dims = ReadinessDimension.objects.filter(assessment=assessment)
    gaps = list(ReadinessGap.objects.filter(assessment=assessment))

    dim_rows = [["Dimension", "Status", "Score", "Explanation"]]
    for d in dims:
        dim_rows.append([
            d.dimension, d.status,
            f"{float(d.sub_score):.1f}" if d.sub_score is not None else "—",
            (d.explanation or "")[:400]])

    evidence_rows = [["Dimension", "Evidence"]]
    for d in dims:
        ev = d.evidence or []
        evidence_rows.append([d.dimension,
                              ", ".join(str(e) for e in ev) or "—"])

    sections = [("Summary (AI Generated Insights)", [assessment.summary])]

    KIND_TITLES = (("strength", "Strengths"), ("risk", "Risks"),
                   ("missing", "Missing information"))
    for kind, title in KIND_TITLES:
        items = [g.text for g in gaps if g.kind == kind and g.text]
        if items:
            sections.append((title, [f"• {t}" for t in items]))
    recs = [g for g in gaps if g.kind == "recommendation"]
    if recs:
        sections.append(("Prioritised recommendations", [
            f"• {g.text}"
            + (f" [{g.impact} impact]" if g.impact else "")
            + (f" ({g.dimension})" if g.dimension else "")
            for g in recs]))

    score_txt = (f" — score {float(assessment.overall_score):.1f}/100"
                 if assessment.overall_score is not None else "")
    path = renderers.render_pdf(
        _tmp("pdf"), title=f"{deal.company.name} — Investor Readiness",
        subtitle=f"Overall: {assessment.overall_band}{score_txt} · "
                 f"version {assessment.version_no}",
        sections=sections,
        tables=[("Dimensions", dim_rows), ("Evidence", evidence_rows)],
        disclaimer="AI-assisted assessment; deterministic scoring. "
                   "Confidential.")
    return _store_and_sign(deal, path, "readiness-report", "pdf", user=user)


def export_strategy_report(deal, *, user=None):
    from fundos.strategy.models import (
        FundraisingStrategy, RaiseRecommendation, Valuation,
    )
    strategy = _require(
        FundraisingStrategy.objects.filter(deal_id=deal.id,
                                           is_active=True).first(),
        "No strategy blueprint yet — generate it first.")
    rec = RaiseRecommendation.objects.filter(deal_id=deal.id,
                                             is_active=True).first()
    valuation = Valuation.objects.filter(deal_id=deal.id,
                                         is_active=True).first()
    fx = fx_block()
    sections = [
        ("Executive summary", [strategy.executive_summary]),
        ("Recommended raise",
         [f"Recommended: {_fmt_money(rec.recommended_value)} "
          f"(range {_fmt_money(rec.low_value)} – {_fmt_money(rec.high_value)})"
          if rec else "—"]),
        ("Indicative valuation range",
         [f"{_fmt_money(valuation.range_low_value)} – "
          f"{_fmt_money(valuation.range_high_value)} "
          f"(confidence: {valuation.confidence})" if valuation else "—"]),
        ("Why this strategy", [strategy.why_this_strategy]),
        ("Risks", strategy.risks or []),
        ("Success factors", strategy.success_factors or []),
        ("FX assumption",
         [f"USD/INR rate {fx['usdInr']:g} — amounts presented in "
          f"{fx['presentationCcy']}."]),
    ]
    path = renderers.render_pdf(
        _tmp("pdf"), title=f"{deal.company.name} — Fundraising Strategy",
        subtitle=deal.name, sections=sections,
        disclaimer="Indicative ranges only — not investment advice or a "
                   "valuation certification (SEBI guardrail). Confidential.")
    return _store_and_sign(deal, path, "strategy-report", "pdf", user=user)


def export_review_report(deal, *, user=None):
    from fundos.materials.models import PackageReview
    review = _require(
        PackageReview.objects.filter(deal_id=deal.id).order_by(
            "-created_at").first(),
        "No package review yet — run it first.")
    sections = []
    if review.category_notes:
        sections.append(("Category notes", [
            f"{cat}: {note}" for cat, note in review.category_notes.items()]))
    if review.risk_summary:
        sections.append(("Risk summary", [
            r if isinstance(r, str) else str(r)
            for r in review.risk_summary]))
    if review.outstanding_actions:
        sections.append(("Outstanding actions", [
            a if isinstance(a, str) else str(a)
            for a in review.outstanding_actions]))
    findings = list(review.findings.all())
    if findings:
        sections.append(("Findings", [
            f"[{f.priority}] {f.category or 'general'} — {f.reason}"
            + (f" (impact: {f.business_impact})" if f.business_impact else "")
            for f in findings]))
    path = renderers.render_pdf(
        _tmp("pdf"), title=f"{deal.company.name} — Package Review",
        subtitle=deal.name, sections=sections or [("Overall",
                                                   ["Review completed."])],
        disclaimer="Confidential. Prepared with FundOS.")
    return _store_and_sign(deal, path, "review-report", "pdf", user=user)
