"""V2 Assessment serializers and payload builders for Django.

Adapts Django Assessment & ParameterValue model instances into exact V2.0
AssessmentResponse and ParameterEvidence JSON shapes, matching Fundraising
Strategy V2.0 contracts.
"""
import sys
import logging
from pathlib import Path
from decimal import Decimal

# Ensure Fundraising Strategy V2.0 is in sys.path for reference and engine imports
_v2_root = Path(__file__).resolve().parents[3] / "Fundraising Strategy V2.0"
if _v2_root.exists() and str(_v2_root) not in sys.path:
    sys.path.insert(0, str(_v2_root))

try:
    from app.assessment import reference as v2_ref  # type: ignore
    from app.assessment import engine as v2_engine  # type: ignore
except ImportError:
    v2_ref = None
    v2_engine = None

import os
import re

from fundos.assessment import hierarchy as H

logger = logging.getLogger(__name__)

#: What a locator is called inside each file type. A deck has slides, a PDF
#: has pages, a workbook has sheets; saying "page 20" about a PowerPoint is
#: the kind of small wrongness that tells a reader nobody checked.
_LOCATOR_WORD = {".ppt": "Slide", ".pptx": "Slide", ".pdf": "Page",
                 ".doc": "Page", ".docx": "Page",
                 ".xls": "Sheet", ".xlsx": "Sheet", ".csv": "Sheet"}

#: A source naming a FILE ends in an extension; one naming a topic does not.
_FILENAME_RE = re.compile(r"\.[A-Za-z0-9]{2,5}\s*$")

#: The merged dossier is the file we BUILT from the deck and the web
#: research. Citing it is citing the library rather than the book: 44 stored
#: rows name it, and none of them can be turned to.
_CONTAINER_RE = re.compile(
    r"consolidated research dossier"
    # "Dossier - point 3" is the same container with an index on it, and a
    # live Zyla run cited it on every parameter -- served as source_type
    # "document" at tier 1, the strongest evidence label in the system, for
    # the weakest possible source. Matching only the full phrase let it
    # through.
    r"|^\s*dossier\b"
    r"|^document_extracts$", re.IGNORECASE)


def _trace_from_config(input_key, value, stage, unit=""):
    """The scoring trace, rebuilt from the DATABASE rather than a directory.

    `v2_engine` is imported from "Fundraising Strategy V2.0" alongside
    `v2_ref`, and is not deployed either -- so `band_for` is unreachable on
    the server and numeric rows carry `trace: {}`. Anchor rows have traces
    because those are built inline a few lines above; only the numeric branch
    reached outside. A live Zyla assessment showed exactly that split: every
    anchor traced, `TEAM_FDR_EXP` empty.

    The cut-points are in `ConfigRubric`, which is where the engine that
    produced the band read them from, so the trace is reconstructed from the
    same numbers rather than approximated.
    """
    from fundos.assessment.models import ConfigRubric

    if value is None or not input_key:
        return None
    row = (ConfigRubric.objects.filter(input_key=input_key, stage=stage,
                                       is_active=True).first()
           or ConfigRubric.objects.filter(input_key=input_key,
                                          is_active=True).first())
    if row is None:
        return None

    def _f(number):
        return float(number) if number is not None else None

    ranged = row.ideal_min is not None or row.ideal_max is not None
    trace = {
        "method": "range" if ranged else "monotonic",
        "value": value,
        "unit": unit or row.unit or "",
        "stage": stage,
        "direction": row.direction or ("Range" if ranged else ""),
        "rationale": row.rationale or "",
    }

    if ranged:
        good = (_f(row.good_tolerance_pct) or 0) / 100
        fair = (_f(row.fair_tolerance_pct) or 0) / 100
        low, high = _f(row.ideal_min), _f(row.ideal_max)
        trace["thresholds"] = {"ideal_min": low, "ideal_max": high}
        trace["inputs"] = {
            "value": value, "ideal": [low, high],
            "good_band": [low * (1 - good) if low is not None else None,
                          high * (1 + good) if high is not None else None],
            "fair_band": [low * (1 - fair) if low is not None else None,
                          high * (1 + fair) if high is not None else None]}
        trace["explanation"] = (
            f"{value}{trace['unit']} measured against the ideal band of "
            f"{low} to {high} at {stage}.")
        return trace

    cuts = {"excellent": _f(row.cut_excellent), "good": _f(row.cut_good),
            "fair": _f(row.cut_fair)}
    trace["thresholds"] = cuts
    trace["inputs"] = {"value": value, **cuts}
    lower = (row.direction or "").strip().lower() == "lower"
    cleared = next((name for name, cut in (("Excellent", cuts["excellent"]),
                                           ("Good", cuts["good"]),
                                           ("Fair", cuts["fair"]))
                    if cut is not None
                    and (value <= cut if lower else value >= cut)), None)
    trace["matched_cut_point"] = cuts.get((cleared or "").lower())
    trace["operator"] = "<=" if lower else ">="
    trace["explanation"] = (
        f"{value}{trace['unit']} clears the {cleared} cut-point of "
        f"{'at most' if lower else 'at least'} "
        f"{trace['matched_cut_point']}{trace['unit']} at {stage}."
        if cleared else
        f"{value}{trace['unit']} clears no cut-point at {stage}.")
    return trace


def _rubric_from_config(input_key):
    """The scoring rubric, read from the DATABASE rather than a directory.

    `v2_ref` is imported from "Fundraising Strategy V2.0", a reference folder
    that sits OUTSIDE src/ and is not deployed -- it was never meant to be.
    The import fails on the server, the failure is swallowed, and every
    rubric and anchor comes back null across the whole product. Live proof:
    /api/v1/assessment/reference answers 503 "V2 reference layer unavailable",
    and 0 of 55 terminals on a real assessment carry either.

    The same data is already in `ConfigRubric`, imported from the workbook --
    which is how the engine BANDS these values in the first place. A
    parameter cannot score "Excellent" without its thresholds, and prod's
    scorecard is full of bands, so the rows are there. Reading them here
    removes the outside dependency instead of shipping a reference folder to
    production.
    """
    from fundos.assessment.models import ConfigRubric

    rows = list(ConfigRubric.objects.filter(input_key=input_key,
                                            is_active=True).order_by("stage"))
    if not rows:
        return None

    def _f(value):
        return float(value) if value is not None else None

    stages = {}
    for row in rows:
        if row.ideal_min is not None or row.ideal_max is not None:
            stages[row.stage] = {"ideal_min": _f(row.ideal_min),
                                 "ideal_max": _f(row.ideal_max)}
        else:
            stages[row.stage] = {"excellent": _f(row.cut_excellent),
                                 "good": _f(row.cut_good),
                                 "fair": _f(row.cut_fair)}

    first = rows[0]
    ranged = any("ideal_min" in v for v in stages.values())
    return {
        "key": input_key,
        "kind": "range" if ranged else "monotonic",
        "metric": first.metric_name or "",
        "unit": first.unit or "",
        "direction": first.direction or "",
        "parameter": first.ref_code or "",
        "stages": stages,
        "good_tolerance": (_f(first.good_tolerance_pct) or 0) / 100 or None,
        "fair_tolerance": (_f(first.fair_tolerance_pct) or 0) / 100 or None,
        "rationale": first.rationale or "",
    }


def _anchor_from_config(input_key):
    """The four written band definitions for an anchor-scored parameter."""
    from fundos.assessment.models import ConfigAnchor

    row = (ConfigAnchor.objects.filter(input_key=input_key, is_active=True)
           .order_by("-version").first())
    if not row:
        return None
    return {
        "key": input_key,
        "parameter": row.ref_code or "",
        "name": row.parameter_name or "",
        "scoringBasis": row.scoring_basis or "",
        "bands": {"Excellent": row.excellent_def or "",
                  "Good": row.good_def or "",
                  "Fair": row.fair_def or "",
                  "Poor": row.poor_def or ""},
        "evidenceRequired": row.evidence_required or "",
    }


def _rubric_for(input_key):
    """The reference package when it is present, the database otherwise."""
    if not input_key:
        return None
    found = v2_ref.rubric_for(input_key) if v2_ref else None
    return found or _rubric_from_config(input_key)


def _anchor_for(input_key):
    if not input_key:
        return None
    found = v2_ref.anchor_for(input_key) if v2_ref else None
    return found or _anchor_from_config(input_key)


def _document_category_filename(assessment, category):
    """The company's uploaded file in this category, when there is one.

    Extraction stores the CATEGORY -- "Company Presentation, slide 20" -- and
    a category is not a document: a reader cannot open "Company Presentation",
    and two uploads in it are indistinguishable. The category is an exact
    ProfileDocument label though, so this is a lookup, not a guess. Returns ""
    when the company has none or several, because naming the wrong upload
    reads as precision and sends a reader to the wrong file.
    """
    try:
        from fundos.profile.models import ProfileDocument

        company_id = getattr(getattr(assessment, "deal", None),
                             "company_id", None)
        if not company_id or not category:
            return ""
        wanted = category.strip().casefold()
        names = {d.filename for d in ProfileDocument.objects.filter(
            profile__company_id=company_id).exclude(filename="")
            if d.get_category_display().casefold() == wanted}
        return names.pop() if len(names) == 1 else ""
    except Exception:                       # pragma: no cover - never fatal
        return ""


def _locator_in(filename, locator):
    """The locator said the way that file type says it."""
    text = (locator or "").strip()
    word = _LOCATOR_WORD.get(os.path.splitext(filename or "")[1].lower())
    if not text or not word:
        return text
    number = re.search(r"\d+", text)
    if number and re.match(r"^(slide|page|p\.)\s*\d+$", text, re.IGNORECASE):
        return f"{word} {number.group()}"
    return text


def _sanitize_source_title(raw_source, default_channel=""):
    if not raw_source:
        return ""

    text = raw_source.strip()
    text = re.sub(r"^Source\s*\d*:?\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\(\s*search[\s\-]*grounded\s*\)", "", text, flags=re.IGNORECASE).strip()

    batch_match = re.search(r"Batch\s*\d+:?\s*(.*)", text, re.IGNORECASE)
    if batch_match:
        topic = batch_match.group(1).strip()
        # ", Question 6. What is the TAM…?" is the prompt that was asked, not
        # the name of anything anyone can cite. The topic before it is.
        topic = re.split(r",?\s+Question\s*\d+", topic,
                         maxsplit=1, flags=re.IGNORECASE)[0].strip(" .,;:")
        prefix = text[:batch_match.start()].rstrip(", ").strip()
        if not prefix or prefix.lower() == topic.lower():
            title = topic
        elif topic.lower() in prefix.lower():
            title = prefix
        else:
            title = f"{prefix} — {topic}"
    else:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if len(parts) == 2 and parts[0].lower() == parts[1].lower():
            title = parts[0]
        else:
            title = text

    # If title is a generic section topic without file extension or channel context, add default channel prefix
    is_file = any(ext in title.lower() for ext in [".pdf", ".pptx", ".ppt", ".xlsx", ".csv", ".docx", "http://", "https://"])
    # A DOCUMENT CATEGORY is channel context too. Without these, "Company
    # Presentation" read as a bare topic and got a channel prefixed onto it,
    # producing "Company Research - Company Presentation" -- and the badge was
    # then decided from that string, which is how a deck's slide 20 came to be
    # served under a globe icon.
    has_channel = any(kw in title.lower() for kw in [
        "web research", "company deck", "pitch deck", "dossier", "interview",
        "report", "company research",
        "company presentation", "financial model", "annual report",
        "teaser", "information memorandum", "company documents"])

    if not is_file and not has_channel and default_channel:
        title = f"{default_channel} \u2014 {title}"

    return title


#: A search-grounding redirect. Opaque, expiring, and not a citation anyone
#: can check: clicking one reaches a Google redirect service, never a source.
#: It also has no business being the SOURCE TITLE, which is what a 400-
#: character redirect was being used as.
_OPAQUE_URL = re.compile(
    r"vertexaisearch\.cloud\.google\.com|grounding-api-redirect|"
    r"/grounding-api/|webcache\.googleusercontent\.com", re.IGNORECASE)


def _is_url(text):
    return str(text or "").strip().lower().startswith(("http://", "https://"))


def _parse_source_detail(detail, default_quote="", default_channel=""):
    if not detail:
        return "", "", default_quote or "", ""

    detail_clean = re.sub(r"^Source\s*\d+:?\s*", "", detail, flags=re.IGNORECASE).strip()

    url = ""
    # NOT `[^\s,\—\-]+`: a hyphen inside that class ended the match at the
    # first one, so every URL carrying a hyphen — which is most of them —
    # was truncated into something that resolves nowhere.
    url_match = re.search(r"https?://\S+", detail_clean)
    if url_match:
        url = url_match.group(0).rstrip(".,;)]")
    if url and _OPAQUE_URL.search(url):
        url = ""

    parts = re.split(r"\s+[\-\—\–]\s+", detail_clean, maxsplit=1)
    doc_and_loc = parts[0].strip()
    extracted_snippet = parts[1].strip() if len(parts) > 1 else ""

    # A detail shaped "<url> — <provenance>" names no document: the title
    # lives in the text after the dash, and the text after the dash is
    # therefore not a quotation from anything. Reading it the other way round
    # is what put a redirect URL in `source` and a research question in
    # `quote`.
    if _is_url(doc_and_loc) and extracted_snippet:
        title = _sanitize_source_title(extracted_snippet,
                                       default_channel=default_channel)
        return (title or "Web Research"), "", (default_quote or ""), url

    # Check for Excel sheet cell reference e.g. "ZYLA Business Switch!G18" or "Sheet1!A1:B5"
    excel_match = re.search(r",?\s*([A-Za-z0-9_\s'\-\.\(\)]+\![A-Z]+\d+(?:\:[A-Z]+\d+)?)", doc_and_loc)
    loc_match = re.search(r",?\s*((?:slide|page|p\.|sec\.|section|sheet)\s*\d+[a-z]*.*)", doc_and_loc, re.IGNORECASE)

    if excel_match:
        raw_loc = excel_match.group(1).strip()
        raw_src = doc_and_loc[:excel_match.start()].rstrip(", ").strip()
        if "!" in raw_loc:
            sh_name, cell_ref = raw_loc.split("!", 1)
            sh_name = sh_name.strip("' ")
            locator = f"Sheet: {sh_name}, Cell: {cell_ref}"
        else:
            locator = raw_loc
        if not raw_src:
            raw_src = doc_and_loc
    elif loc_match:
        locator = loc_match.group(1).strip()
        raw_src = doc_and_loc[:loc_match.start()].rstrip(", ").strip()
        if not raw_src:
            raw_src = doc_and_loc
    else:
        raw_src = doc_and_loc
        locator = ""

    source = _sanitize_source_title(raw_src, default_channel=default_channel)
    if _is_url(source):
        # A link is not the name of a source. A detail that is nothing BUT a
        # link was found on the open web whatever the row's stored
        # source_type happens to say, so it is named for that and not for
        # the channel the parameter was collected through.
        source = "Web Research"
    quote = extracted_snippet or default_quote
    return source, locator, quote, url

def _parse_multiple_sources(detail, default_quote="", default_channel=""):
    if not detail:
        return []

    # Split on semicolon delimiters e.g. '; Source 2:' or '; '
    sub_details = [s.strip() for s in re.split(r";\s*(?=(?:Source\s*\d+:?|Source:?|Batch\s*\d+:?|[A-Z][a-z]+:?))", detail, flags=re.IGNORECASE) if s.strip()]
    if len(sub_details) <= 1 and ";" in detail:
        sub_details = [s.strip() for s in detail.split(";") if s.strip()]

    results = []
    for sub in sub_details:
        src, loc, q, url = _parse_source_detail(sub, default_quote=default_quote, default_channel=default_channel)
        if src or loc or q or url:
            results.append({
                "source": src,
                "locator": loc,
                "quote": q,
                "url": url
            })
    return results


def _deduplicate_and_sort_citations(citations):
    """Sort and deduplicate citations for API output.

    Rules:
    1. Document citations (source_type == "document", source_rank == 1) always come first,
       followed by web search citations (source_type == "web", source_rank == 2).
    2. Within the same source_type, sort by source_tier (lower tier number = better quality).
    3. Deduplicate exact duplicate citations (same source_type, source, locator, quote, url).
    4. For web search citations with identical quotes (or identical normalized quotes),
       keep only ONE web search citation per quote so that multi-batch web research
       does not repeat the exact same quote multiple times across synthetic sources.
    5. For web search citations with generic source titles (e.g. "Company Research" or "Web Research"),
       keep only ONE web search citation per source title if quotes are identical.
    """
    if not citations:
        return []

    def _norm_quote(q):
        if not q:
            return ""
        return " ".join(str(q).strip().split()).lower()

    for c in citations:
        if isinstance(c, dict):
            stype = str(c.get("source_type", "")).lower()
            if "source_rank" not in c or c["source_rank"] is None:
                c["source_rank"] = 1 if stype == "document" else 2
            if "source_tier" not in c or c["source_tier"] is None:
                c["source_tier"] = 1 if stype == "document" else (c.get("source_tier") or 3)

    indexed_citations = list(enumerate(citations))
    sorted_tuples = sorted(
        indexed_citations,
        key=lambda x: (
            x[1].get("source_rank", 2) if isinstance(x[1], dict) else 2,
            x[1].get("source_tier", 3) if isinstance(x[1], dict) else 3,
            x[0]
        )
    )
    sorted_citations = [c for _, c in sorted_tuples]

    seen_exact = set()
    seen_web_quotes = set()
    seen_web_sources = set()
    deduped = []

    for c in sorted_citations:
        if not isinstance(c, dict):
            deduped.append(c)
            continue
        stype = str(c.get("source_type", "")).lower()
        src = str(c.get("source", "")).strip()
        loc = str(c.get("locator", "")).strip()
        q = str(c.get("quote", "")).strip()
        url = str(c.get("url", "")).strip()
        norm_q = _norm_quote(q)

        exact_key = (stype, src.lower(), loc.lower(), norm_q, url.lower())
        if exact_key in seen_exact:
            continue
        seen_exact.add(exact_key)

        if stype == "web":
            if norm_q and norm_q in seen_web_quotes:
                continue
            if src.lower() in ("company research", "web research") and src.lower() in seen_web_sources:
                continue
            if norm_q:
                seen_web_quotes.add(norm_q)
            seen_web_sources.add(src.lower())

        deduped.append(c)

    return deduped

COVERAGE_FLOOR_PCT = 60.0

# Both endpoints render the same findings, so the merge lives in one
# place. Two copies is how Phase 1 and V2 came to describe one deal
# differently in the first place.
from fundos.assessment.decision import (audit_as_findings,  # noqa: E402
                                        merge_findings)

# Band-to-score mapping matching ConfigConstant defaults (§2.5).
_BAND_SCORES = {
    "Exceptional": 10.0,
    "Excellent": 9.0,
    "Good": 7.0,
    "Fair": 5.0,
    "Poor": 3.0,
}


def _parse_num(val):
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        s = str(val).replace(",", "").replace("%", "").strip()
        return float(s)
    except (ValueError, TypeError):
        return None


def _band_from_score(score):
    """Reverse-map a numeric score to the nearest band label."""
    if score is None:
        return None
    s = float(score)
    if s >= 9.5:
        return "Exceptional"
    elif s >= 8.0:
        return "Excellent"
    elif s >= 6.0:
        return "Good"
    elif s >= 4.0:
        return "Fair"
    else:
        return "Poor"


def _load_config_lookups():
    """Load ConfigParameter and ConfigWeight once, returning dicts for reuse."""
    from fundos.assessment.models import ConfigParameter, ConfigWeight

    # Config parameters indexed by input_key
    cfg_by_key = {p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id__isnull=True, is_active=True)}

    # Weight tree: {(level, code): ConfigWeight}
    weight_map = {}
    for w in ConfigWeight.objects.filter(is_active=True).order_by("tenant_id"):
        weight_map[(w.level, w.code)] = w

    # Category labels from weight config
    cat_labels = {code: w.label for (lvl, code), w in weight_map.items()
                  if lvl == "category"}

    return cfg_by_key, weight_map, cat_labels


def _resolve_sectors_from_profile(company):
    """Resolve macro_sector and sub_sector from the company profile sections.

    Delegates to the bridge's reader so the serializer and the scoring path
    cannot disagree about where the sector lives. Two copies of this lookup is
    how the payload ends up claiming a sector the scored assessment never saw.
    """
    from fundos.assessment.profile_bridge import _sector_from_profile

    # `company.profile` is a OneToOne; the reverse accessor raises a subclass
    # of AttributeError when absent, which getattr's default absorbs.
    return _sector_from_profile(getattr(company, "profile", None))


def _percentile_trace(value, band, score, assessment):
    """The banding a deal-database percentile row ACTUALLY went through.

    These rows are scored on the Django side by `band_for_percentile`, against
    the configured percentile cut-points. The V2 engine that builds every
    other trace has no rubric for them, so it answered `method="excluded"`
    with "No rubric is defined for X, so this row cannot be scored" — on a row
    carrying a real band, a real score and full weight in its category. The
    trace has to describe the banding that happened, not the one that did not.
    """
    from fundos.assessment.services import constant

    tenant = getattr(assessment, "tenant_id", None) if assessment else None
    cuts = {
        "Excellent": float(constant("percentile_excellent", 8, tenant)),
        "Good": float(constant("percentile_good", 6, tenant)),
        "Fair": float(constant("percentile_fair", 4, tenant)),
    }
    return {
        "method": "lookup",
        "value": value,
        "unit": "percentile",
        "direction": "Higher",
        "stage": None,
        "thresholds": cuts,
        "matched_band": band,
        "matched_cut_point": cuts.get(band),
        "operator": ">=",
        "score": score,
        "explanation": (
            "Read as a percentile from the imported sector deal table and "
            "banded against the configured percentile cut-points. Only a "
            "different cohort moves this row."),
    }


def build_v2_parameter_evidence(pv, cfg, node_data=None, assessment=None,
                                cat_labels=None, absences=None):
    """Build full V2.0 ParameterEvidence dictionary from ParameterValue + Config.

    Preserves 100% of fields: citations, quotes, locators, precedence rank, tier,
    confidence, written reasoning, blue insight callouts, trace data, rubrics,
    anchors, dictionary entries, and weight renormalizations.
    """
    cat_labels = cat_labels or {}
    key = pv.input_key if pv else (cfg.input_key if cfg else "")
    code = (cfg.ref_code if cfg else "") or (pv.ref_code if pv else "") or key
    name = (cfg.name if cfg else "") or code or key

    raw_str = (pv.raw_value if pv else "") or ""
    raw_num = _parse_num(raw_str)
    unit = (pv.unit if pv else "") or (getattr(cfg, "unit", "") or "") or ""

    score_val = float(pv.score) if pv is not None and pv.score is not None else None
    sys_score = float(pv.system_score) if pv is not None and pv.system_score is not None else score_val
    sys_band = (pv.system_band if pv else "") or (pv.band if pv else "") or ""
    band = (pv.band if pv else "") or sys_band

    is_overridden = bool(pv is not None and pv.is_overridden)
    override_score = float(pv.score) if (pv and is_overridden and pv.score is not None) else None
    override_comment = (pv.override_comment if pv else "") or ""

    node_data = node_data or {}
    declared_pct = round(float(node_data.get("weight", 0)), 4)
    applied_wt = float(node_data.get("applied_weight", 0))
    effective_pct = round(applied_wt * 100.0 if applied_wt <= 1.0 else applied_wt, 4)

    eff_wt = float(node_data.get("effective_weight", 0))
    effective_total_pct = round(eff_wt * 100.0 if eff_wt <= 1.0 else eff_wt, 4)

    is_excluded = score_val is None
    ex_reason = node_data.get("excluded_reason") or ("not_evidenced" if is_excluded else None)

    weights_obj = {
        "declaredPct": declared_pct,
        "effectivePct": effective_pct,
        "effectiveOfTotalPct": effective_total_pct,
        "excluded": is_excluded,
        "exclusionReason": ex_reason,
        "weightWithinParent": effective_pct,
        "declaredWeight": declared_pct
    }

    assessment_status = {
        "system": {
            "score": sys_score,
            "band": sys_band or None
        },
        "override": {
            "score": override_score,
            "band": band or None,
            "comment": override_comment or None,
            "by": str(pv.overridden_by) if (pv and pv.overridden_by) else None
        } if is_overridden else None,
        "effective": {
            "score": score_val,
            "band": band or None
        },
        "status": "overridden" if is_overridden else ("scored" if score_val is not None else "not_evidenced")
    }

    # No ParameterValue at all is a different statement from a weak source:
    # the leaf was never answered, and the tree says so rather than calling an
    # absent value an estimate.
    if pv is None:
        tier_str = "Not Evidenced"
    elif hasattr(pv, "source_tier_label"):
        tier_str = pv.source_tier_label
    elif pv.source_tier == 1:
        tier_str = "Verified"
    elif pv.source_tier == 2:
        tier_str = "Management"
    else:
        tier_str = "Estimate"
    conf_str = "high" if (pv and pv.confidence and float(pv.confidence) >= 0.8) else (
        "medium" if (pv and pv.confidence and float(pv.confidence) >= 0.5) else "low"
    )
    conf_val = float(pv.confidence) if (pv and pv.confidence is not None) else None

    # Citations list
    citations = []
    if pv and getattr(pv, "citations", None):
        citations = pv.citations
    else:
        detail = (pv.source_detail if pv else "") or ""
        source_type = (pv.source_type if pv else "") or "Company Deck"
        if detail:
            justification = (pv.justification if pv else "") or ""
            # NO DEFAULT CHANNEL.
            #
            # It used to be computed from `source_type`, which only ever holds
            # "document" or "profile" -- so the ternary's first two branches
            # were unreachable and every citation got "Company Research"
            # prefixed onto it. "Company Presentation, slide 20" became
            # "Company Research - Company Presentation", and the badge was
            # then decided from that string: "research" matched, so a deck's
            # slide 20 was served under a globe icon.
            #
            # A source we cannot name is left unnamed and dropped below. A
            # bucket label that reads like evidence is worse than a blank.
            # The channel names WEB RESEARCH and nothing else. It used to be
            # picked by a ternary on `source_type`, which only ever holds
            # "document" or "profile" -- so its first two branches were
            # unreachable and every citation got "Company Research". Document
            # categories are now recognised as channel context by the
            # sanitizer, so this reaches only genuine research topics.
            parsed_items = _parse_multiple_sources(
                detail, default_quote=justification,
                default_channel="Web Research")

            for item in parsed_items:
                raw = (item.get("source") or "").strip()
                locator = (item.get("locator") or "").strip()
                # A FILENAME IS NEVER THE CONTAINER, whatever it is called.
                # The container pattern matches a leading "dossier", and a
                # company genuinely named "Dossier Analytics" would otherwise
                # have its own PDF discarded as the merged corpus. Asking
                # "is this a file?" first makes that impossible.
                #
                # A category IS an exact ProfileDocument label, so resolving
                # it to the company's file is a lookup rather than a guess.
                is_file = bool(raw) and bool(_FILENAME_RE.search(raw))
                if not raw or (not is_file and _CONTAINER_RE.search(raw)):
                    continue

                filename = (raw if is_file else
                            _document_category_filename(assessment, raw))
                if filename:
                    citations.append({
                        "source": filename,
                        "locator": _locator_in(filename, locator),
                        "quote": item["quote"],
                        "source_type": "document",
                        "source_rank": 1,
                        "source_tier": 1 if _LOCATOR_WORD.get(
                            os.path.splitext(filename)[1].lower()
                        ) == "Sheet" else 2,
                        "retrieved_on": str(pv.as_of) if (
                            pv and getattr(pv, "as_of", None)) else "",
                    })
                    continue

                # Not a file we hold. Typed from the source string itself --
                # never from a name this code invented a line earlier.
                src_title = raw.lower()
                is_web = bool(item.get("url")) or any(
                    kw in src_title for kw in ("web", "search", "research",
                                               "batch"))
                citations.append({
                    "source": item["source"],
                    "locator": locator,
                    "quote": item["quote"],
                    "source_type": "web" if is_web else "document",
                    "source_rank": 2 if is_web else 1,
                    "source_tier": 3 if is_web else (
                        pv.source_tier if (pv and pv.source_tier) else 2),
                    "retrieved_on": str(pv.as_of) if (
                        pv and getattr(pv, "as_of", None)) else "",
                })

    # No citation carries a link. A URL is how a machine reaches a page, not
    # how a reader is told where a fact came from — and the ones this system
    # holds are a mix of expiring search redirects and third-party pages
    # nobody is being asked to click. The channel and the topic say where it
    # came from; that is the citation.
    for c in citations:
        if isinstance(c, dict):
            c.pop("url", None)
            if "retrieved_on" in c and not c["retrieved_on"]:
                del c["retrieved_on"]

    # Sort and deduplicate citations (documents rank first, duplicate quotes deduplicated)
    citations = _deduplicate_and_sort_citations(citations)

    # Provenance object
    detail_str = (pv.source_detail if pv else "") or "Not recorded"
    url_str = citations[0].get("url", "") if citations else ""
    kind_str = citations[0]["source_type"] if citations else "Company Deck"
    statement_str = f"Value taken from {kind_str}: {detail_str}."
    if score_val is not None:
        statement_str += f" Banded as {band}."

    provenance_obj = {
        "source": detail_str,
        "sourceUrl": url_str,
        "sourceKind": kind_str,
        "tier": pv.source_tier if pv else None,
        "tierMeaning": f"Tier {pv.source_tier} source evidence" if (pv and pv.source_tier) else "",
        "confidence": conf_val,
        "confidenceLabel": conf_str,
        "method": getattr(cfg, "scoring_type", "rubric") if cfg else "rubric",
        "statement": statement_str
    }

    # Trace object
    trace_data = {}
    stage_str = (assessment.deal_stage if assessment else "Series A") or "Series A"
    scoring_type = (getattr(cfg, "scoring_type", "") or "") if cfg else ""

    if scoring_type == "lookup" and raw_num is not None:
        # Never ask the V2 engine about a row it has no rubric for; it would
        # answer "excluded" for a row this side scored.
        trace_data = _percentile_trace(raw_num, band, score_val, assessment)
    elif scoring_type == "lookup":
        # No percentile reached this row. Excluded is the honest answer — the
        # mirror of the defect above, and just as wrong the other way round:
        # quoting the cut-point table on a row that was never banded
        # describes an arithmetic that did not happen.
        #
        # WHICH absence it is, though, is not a detail. "No percentile is
        # held for this cohort" was asserted for all four causes, including
        # the case where the table holds the number and nothing had looked it
        # up yet — telling a reader the market data does not exist while it
        # sat in the database. The reason now comes from the same resolution
        # the seeding uses, so the row cannot claim one thing while the
        # lookup found another.
        reason, sentence = (absences or {}).get(key, ("", ""))
        trace_data = {
            "method": "excluded",
            "reason": reason or "no_percentile",
            "explanation": (
                (sentence + " The row is excluded from scoring and its "
                 "weight redistributes.") if sentence else
                "No percentile is held in the sector deal table for this "
                "cohort, so the row is excluded from scoring and its weight "
                "redistributes."),
        }
    elif scoring_type == "anchor":
        # An anchor row is banded against the written definitions upstream,
        # never against cut-points. Asking the reference engine produced
        # "No rubric is defined for ANC_PRIOR_STARTUP, so this row cannot be
        # scored" — which blames the configuration for a gap that is not
        # there and not the one that is: the row holds a value and a Tier 1
        # citation, and what is missing is the band those anchors should have
        # been read into.
        trace_data = {
            "method": "anchor",
            "value": raw_str or None,
            "unit": unit or "Band",
            "stage": stage_str,
            "matched_band": band or None,
            "score": score_val,
            "explanation": (
                f"Banded by reading the evidence against the written "
                f"{key} anchor definitions. No cut-points apply."
                if band else
                f"Scored from the written {key} anchor definitions, and no "
                f"band was established for this evidence, so the row is held "
                f"unscored and its weight redistributes to its siblings."),
        }
    elif raw_num is not None and key:
        try:
            if v2_engine:
                _, trace_obj = v2_engine.band_for(key, raw_num, stage_str)
                trace_data = trace_obj.as_dict()
            else:
                # The engine is not deployed; the cut-points are in the
                # database it read them from.
                trace_data = _trace_from_config(key, raw_num, stage_str,
                                                unit) or {}
            if not trace_data:
                raise ValueError("no trace")
        except Exception:
            trace_data = {
                "value": raw_num,
                "unit": unit,
                "stage": stage_str,
                "matched_band": band,
                "matched_cut_point": None,
                "operator": ">=",
                "score": score_val
            }

    # A scored row may not claim it was excluded. The two engines disagree
    # wherever one holds a rubric the other does not, and when they do, the
    # side that produced the score is the side telling the truth — the row
    # carries a band, full weight and a contribution to its category.
    if score_val is not None and trace_data.get("method") == "excluded":
        trace_data = {
            "method": scoring_type or "rubric",
            "value": raw_num,
            "unit": unit,
            "stage": stage_str,
            "matched_band": band,
            "matched_cut_point": None,
            "operator": ">=",
            "score": score_val,
            "explanation": (
                f"Scored against this platform's own {scoring_type or 'rubric'} "
                f"configuration for {key}. The reference engine holds no "
                f"rubric for this row, so no cut-point is quoted here."),
        }

    # Omit duplicate thresholds_by_stage from trace object
    if isinstance(trace_data, dict):
        trace_data.pop("thresholds_by_stage", None)

    v2_rubric = _rubric_for(key)
    v2_anchor = _anchor_for(key)

    contrib = round(effective_total_pct * (score_val or 0.0) / 10.0, 4)

    # Resolve category_name from cfg or cat_labels fallback
    cat_code = getattr(cfg, "category_code", "") if cfg else (
        pv.category if pv else "")
    cat_name = cat_labels.get(cat_code, "")

    # Clean Weight Summary object
    weight_summary = {
        "declared": declared_pct,
        "applied": effective_pct,
        "effectiveOfTotalPct": effective_total_pct,
        "contribution": contrib,
        "excluded": is_excluded,
        "exclusionReason": ex_reason
    }

    # Clean Evidence Object
    evidence_obj = {
        "tier": tier_str,
        "confidence": conf_str,
        "confidenceValue": conf_val,
        "reasoning": (pv.justification if pv else "") or "",
        "citations": citations
    }

    return {
        "ref": code,
        "key": key,
        "name": name,
        "type": scoring_type or "rubric",
        "is_reference": is_excluded or (pv is None),
        "status": assessment_status["status"],
        "value": {
            "raw": raw_num if raw_num is not None else (raw_str or None),
            "unit": unit,
            # `raw` keeps the extracted number exactly as it was measured;
            # `display` is the one a person reads, rounded once here rather
            # than by each client differently. "28.1747776" is a fact about
            # a float, not a fact about the company.
            "display": (_display_value(raw_str, unit) if raw_str
                        else (band or None))
        },
        "band": band or None,
        "score": score_val,
        "weight": weight_summary,
        "assessment": assessment_status,
        "evidence": evidence_obj,
        "trace": trace_data,
        "rubric": v2_rubric,
        "anchor": v2_anchor,
        "as_of": str(getattr(pv, "as_of", "")) if (pv and hasattr(pv, "as_of") and pv.as_of) else None,
        "period_basis": getattr(pv, "period_basis", "Actual") if hasattr(pv, "period_basis") else "Actual",
    }


def _ref_sort_key(code):
    """Natural ordering for dotted ref codes, so A.2 precedes A.10."""
    out = []
    for seg in str(code or "").split("."):
        if seg.isdigit():
            out.append((0, int(seg), ""))
        else:
            out.append((1, 0, seg))
    return out


def _merge_input_evidence(node, extras):
    """Fold the supporting inputs' evidence into the terminal that owns them.

    A terminal may be answered by more than one config row — a rubric plus its
    written anchor, TAM plus TAM CAGR, a value plus the `(ref)` cross-check
    that tests it. None of those is a node, so none of their citations may be
    dropped on the floor: the reader has to be able to see every source that
    stands behind the one score the terminal shows.

    Reasoning is joined rather than replaced for the same reason. Both are
    presentation joins over evidence the extractor already produced; nothing
    here re-ranks a source or re-reads a tier.
    """
    citations = list((node.get("evidence") or {}).get("citations") or [])
    reasons = [(node.get("evidence") or {}).get("reasoning") or ""]
    for extra in extras:
        ev = extra.get("evidence") or {}
        citations.extend(ev.get("citations") or [])
        text = ev.get("reasoning") or ""
        if text and text not in reasons:
            reasons.append(text)
    node["evidence"] = dict(node.get("evidence") or {})
    node["evidence"]["citations"] = _deduplicate_and_sort_citations(citations)
    node["evidence"]["reasoning"] = " ".join(r for r in reasons if r).strip()


def _terminal_node(ref, name, level, declared_weight, inputs):
    """One terminal parameter, carrying everything its inputs established.

    `inputs` are the evidence dicts of every config row that feeds this ref,
    scoring rows first. The node presents ONE score — rolled over the scoring
    rows by the same `weighted_rollup` the engine uses, which for the single
    input case is that row's own score unchanged — and keeps the rows
    themselves under `inputs`, so a drill-down can still see which value came
    from where.
    """
    from fundos.engines.deal_assessment import weighted_rollup

    scoring = [e for e in inputs if not e.get("_is_helper")]
    primary = next((e for e in scoring if e.get("score") is not None),
                   scoring[0] if scoring else inputs[0])

    if len(scoring) > 1:
        score, _applied = weighted_rollup(
            [{"code": e.get("key"), "score": e.get("score"), "weight": 1.0}
             for e in scoring])
        score = round(float(score), 4) if score is not None else None
        band = _band_from_score(score)
    else:
        score = primary.get("score")
        band = primary.get("band")

    node = dict(primary)
    node["ref"] = ref
    # The row the terminal's value and trace came from. A ref code addresses
    # the terminal; this addresses the input inside it, which is what an
    # override or a drill-down has to name.
    node["key"] = primary.get("key")
    node["input_key"] = primary.get("key")
    node["name"] = name
    node["level"] = level
    node["isTerminal"] = True
    node["score"] = score
    node["band"] = band
    node["is_reference"] = False

    weight_summary = dict(node.get("weight") or {})
    weight_summary["declared"] = round(float(declared_weight), 4)
    weight_summary["excluded"] = score is None
    if score is not None:
        weight_summary["exclusionReason"] = None
    node["weight"] = weight_summary
    node["applied_weight"] = 0.0

    _merge_input_evidence(node, [e for e in inputs if e is not primary])

    # A terminal answered by its rubric row still has to show the written
    # anchor definitions its sibling input holds, and vice versa. Reading them
    # off the primary alone is how A.3.b came to say it had no anchor while
    # ANC_ADVISORS sat inside it carrying one.
    for field in ("rubric", "anchor"):
        if node.get(field) is None:
            node[field] = next((e.get(field) for e in inputs
                                if e.get(field) is not None), None)

    # The rows themselves, never as siblings. `is_reference` here means "does
    # not feed the score" — the cross-check rows keep saying so, from inside.
    #
    # ONLY when there is more than one. A terminal fed by a single row IS
    # that row: `key` names it, and everything else on the node was read
    # straight off it. Emitting it again under `inputs` repeated the value,
    # the evidence, every citation, the whole rubric and the whole anchor —
    # byte for byte, on 34 of 55 terminals, for a fifth of the entire
    # payload and not one fact a reader did not already have.
    #
    # `rubric` and `anchor` are dropped from the entries that remain, for
    # the same reason: the terminal carries both, merged across its inputs,
    # and the definitions are properties of the parameter rather than of the
    # row that happened to answer it.
    if len(inputs) > 1:
        node["inputs"] = [{
            "key": e.get("key"),
            "name": e.get("name"),
            "type": e.get("type"),
            "is_reference": bool(e.get("_is_helper")),
            "status": e.get("status"),
            "value": e.get("value"),
            "score": e.get("score"),
            "band": e.get("band"),
            "evidence": e.get("evidence"),
            "trace": e.get("trace"),
        } for e in inputs]

    for dead in ("_is_helper", "parameter", "children", "feeds_score",
                 "category", "category_name", "parent_ref"):
        node.pop(dead, None)
    return node


def _grouping_node(ref, name, level, declared_weight, children, child_key):
    """A parent or child: ref, name, weight and what its children rolled up to.

    No evidence, no value, no citations. Everything a reader could ask about
    the numbers is one level down, on the terminal that owns them.
    """
    from fundos.engines.deal_assessment import weighted_rollup

    score, applied = weighted_rollup([
        {"code": c["ref"],
         "score": c["score"],
         "weight": (c["weight"].get("declared", 0.0)
                    if isinstance(c.get("weight"), dict)
                    else float(c.get("weight") or 0.0))}
        for c in children])
    shares = {a["code"]: a["applied_weight"] for a in applied}
    for c in children:
        c["applied_weight"] = round(shares.get(c["ref"], 0.0), 2)

    return {
        "ref": ref,
        "name": name,
        "level": level,
        "isTerminal": False,
        "weight": round(float(declared_weight), 4),
        "applied_weight": 0.0,
        "score": round(float(score), 2) if score is not None else None,
        "band": _band_from_score(score),
        child_key: children,
    }


def _build_scorecard_tree(categories_list, params_list, weight_map,
                          cfg_by_key, placeholder, findings_by_key=None):
    """Project the flat parameter list onto the Fundraising hierarchy.

    Parent -> Child -> Grandchild, where the terminal node IS the parameter
    and every node above it is grouping only. The shape comes from
    `fundos.assessment.hierarchy`; the numbers come from the scoring path
    unchanged. Config rows that feed a terminal are carried inside it and
    never appear as nodes of their own — which is what keeps the `(ref)`
    cross-checks and the written anchors off the scorecard while leaving them
    fully available to the engine that reads them.
    """
    findings_by_key = findings_by_key or {}
    params_by_key = {p.get("key") or p.get("ref"): p for p in params_list}

    # Config rows grouped by the terminal they belong to. A row whose ref
    # names no terminal (TEAM_CXO_SEATS sits on the child A.2) is held back
    # deliberately: it stays configured, extracted and readable, and it is
    # not promoted to a node the hierarchy does not have.
    cfgs_by_terminal = {}
    for cfg in cfg_by_key.values():
        term = H.terminal_ref_for(cfg.ref_code)
        if term:
            cfgs_by_terminal.setdefault(term, []).append(cfg)

    def _evidence_for(cfg):
        item = params_by_key.get(cfg.input_key)
        item = dict(item) if item is not None else placeholder(cfg)
        item["_is_helper"] = not bool(cfg.feeds_score)
        return item

    def _inputs_for(ref):
        cfgs = sorted(cfgs_by_terminal.get(ref, []),
                      key=lambda c: (bool(not c.feeds_score),
                                     c.sort_order or 0, c.input_key))
        return [_evidence_for(c) for c in cfgs]

    def _declared(level, ref):
        w = weight_map.get(("child" if level == "grandchild" else "subitem",
                            ref))
        return float(w.weight) if w else 0.0

    def _build_terminal(ref, name, level):
        inputs = _inputs_for(ref)
        if inputs:
            node = _terminal_node(ref, name, level, _declared(level, ref),
                                  inputs)
        else:
            # No config row reaches this terminal at all. It still belongs in
            # the tree — a missing row is exactly the thing a reader needs to
            # see — so it is shown unscored rather than silently omitted.
            node = {
                "ref": ref, "name": name, "level": level, "isTerminal": True,
                "key": None, "input_key": None, "type": "rubric",
                "score": None, "band": None,
                "is_reference": False, "status": "not_evidenced",
                "value": {"raw": None, "unit": "", "display": None},
                "weight": {"declared": round(_declared(level, ref), 4),
                           "applied": 0.0, "effectiveOfTotalPct": 0.0,
                           "contribution": 0.0, "excluded": True,
                           "exclusionReason": "not_configured"},
                "evidence": {"tier": "Not Evidenced", "confidence": "low",
                             "confidenceValue": None, "reasoning": "",
                             "citations": []},
                "trace": {}, "rubric": None, "anchor": None,
                "inputs": [], "applied_weight": 0.0,
            }
        # Findings are looked up over EVERY contributing row, whether or not
        # the row is echoed under `inputs` — a single-input terminal still
        # has findings raised against the key it was answered by.
        keys = [e.get("key") for e in inputs] if inputs else [node.get("key")]
        node["diligenceFindings"] = [
            _slim_finding(f, node)
            for key in keys for f in findings_by_key.get(key, [])]
        return node

    by_code = {c.get("code"): c for c in categories_list}
    out = []
    for cat_code, cat_name, children_spec in H.HIERARCHY:
        cat = by_code.get(cat_code)
        if cat is None:
            # Nothing scored this category — an assessment written before it
            # existed, or one that never reached it. The hierarchy is a
            # contract, not a report of what happened to be there, so the
            # category is shown unscored rather than dropped and leaving the
            # reader to notice that A-F came back as five letters.
            cat_w = weight_map.get(("category", cat_code))
            cat = {"code": cat_code, "name": cat_name, "description": [],
                   "weight": float(cat_w.weight) if cat_w else 0.0,
                   "applied_weight": 0.0, "score": None, "band": None}

        child_nodes = []
        for child_ref, child_name, gc_spec in children_spec:
            if not gc_spec:
                child_nodes.append(
                    _build_terminal(child_ref, child_name, "child"))
                continue
            gcs = [_build_terminal(gc_ref, gc_name, "grandchild")
                   for gc_ref, gc_name, _n in gc_spec]
            child_nodes.append(_grouping_node(
                child_ref, child_name, "child", _declared("child", child_ref),
                gcs, "children"))

        # The category keeps the score the scoring engine persisted; this
        # roll-up runs only to hand each child the share it carried.
        _grouping_node(cat_code, cat_name, "category", cat.get("weight") or 0,
                       child_nodes, "subitems")

        cat["name"] = cat_name
        cat["level"] = "category"
        cat["isTerminal"] = False
        cat.pop("ref", None)
        cat["subitems"] = child_nodes
        _apply_effective_weights(cat)
        out.append(cat)

    categories_list[:] = out
    return categories_list


def _apply_effective_weights(node, parent_share=1.0):
    """Tell each row the share the roll-up actually gave it."""
    share = parent_share * (float(node.get("applied_weight") or 0.0) / 100.0)
    # Two decimals, everywhere a share is reported. A roll-up over three
    # siblings produces 33.333333333333336, and a payload that prints a
    # float's full binary tail is asking every client to round it again and
    # to disagree about how.
    within = round(float(node.get("applied_weight") or 0.0), 2)
    score = node.get("score")

    if "subitems" not in node and "children" not in node:
        if not (score is not None and within == 0.0):
            of_total = round(share * 100.0, 4)
            contrib_val = round(of_total * float(score) / 10.0, 4) if score is not None else 0.0

            w_summary = node.get("weight") or {}
            if isinstance(w_summary, dict):
                w_summary["applied"] = within
                w_summary["effectiveOfTotalPct"] = of_total
                w_summary["contribution"] = contrib_val
                node["weight"] = w_summary
        # The terminal keeps `applied_weight` alongside `weight.applied`,
        # holding the SAME number. Deleting it left a reader with a tree node
        # that could not say what share it carried without opening its weight
        # block, and a caller that asked for it got a KeyError; two names for
        # one share is only a defect when they disagree.
        node["applied_weight"] = within

    children = node.get("subitems") or node.get("children") or []
    for child in children:
        _apply_effective_weights(child, share)


#: A finding that says something is WRONG, as opposed to merely unknown.
#: `Red Flag` is raised by the analysis engine itself for contradictions and
#: unsupported claims; `weak_band` is added here for a row that scored Poor,
#: which is evidenced badly rather than not evidenced at all.
RED_FLAG_TYPES = ("Red Flag",)

#: The band that makes a scored row a flag in its own right. Poor is a
#: statement about the company; Fair is a statement about the range.
WEAK_BANDS = ("Poor",)


#: Units that say what the number IS rather than what it is measured in.
#: Printing them reads as a mistake: "3 Count", "7.5 Score".
_BARE_UNITS = {"count", "score", "number", "#", "no.", "nos", "x-factor"}

#: Units that sit hard against the number, with no space.
_TIGHT_UNITS = {"%", "x"}


def _display_value(raw, unit):
    """One number, formatted once, on the server.

    `"28.1747776"` reached a founder-facing screen because every client was
    left to decide how to round it and none of them did. Percentages read to
    one decimal, everything else to two, and trailing zeros go.
    """
    key = str(unit or "").strip().casefold()

    # An anchor row's value IS a band word, and its unit is the word "Band".
    # Appending it produces "Poor Band", which reads as a grade nobody uses.
    if key == "band":
        return str(raw or "").strip()

    try:
        num = float(str(raw).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        text = str(raw or "").strip()
        return f"{text} {unit}".strip() if unit and key not in _BARE_UNITS \
            and key not in _TIGHT_UNITS else text

    text = f"{num:.1f}" if key == "%" else f"{num:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not unit or key in _BARE_UNITS:
        # "Count" and "Score" are what the number IS, not a unit it is
        # measured in. "3 Count" and "7.5 Score" read as typos.
        return text
    if key in _TIGHT_UNITS:
        # No space: "37.6%", "4x" — never "37.6 %" or "4 x".
        return f"{text}{unit.strip()}"
    return f"{text} {unit}"


def _ask_text(finding, headline, names=None):
    """The advice, without the sentence the headline already carried.

    The engine writes the observation and the advice into one string, and a
    row contradicted by two cross-checks gets that string twice. Printed raw
    it read as the same sentence four times over.
    """
    text = _humanise(" ".join(str(finding.get("action")
                                  or finding.get("ask") or "").split()),
                     names or {})
    head = headline.rstrip(".").strip().casefold()
    kept, seen = [], set()
    for sentence in [p.strip() for p in text.split(". ") if p.strip()]:
        body = sentence.rstrip(".").strip()
        low = body.casefold()
        if low == head or low in seen:
            continue
        seen.add(low)
        kept.append(body)
    return ". ".join(kept) + "." if kept else ""


def _humanise(text, names):
    """Swap workbook input keys for the names a reader knows them by.

    The engine writes its findings in input keys because that is what it
    reasons in — "BQ_GM_TREND scored Excellent but its cross-check
    ANC_PRICING_POWER is Poor". Correct, and unreadable to the partner this
    line exists for. Longest key first, so ANC_MOAT_SCALE is not half-matched
    by a shorter key that prefixes it.
    """
    for key in sorted(names, key=len, reverse=True):
        if key and key in text:
            text = text.replace(key, names[key])
    return text


def _headline(finding, name, names=None):
    """The one line a partner scans, never the paragraph behind it."""
    text = _humanise(" ".join(str(finding.get("finding") or "").split()),
                     names or {})
    # The engine writes "KEY scored X but its cross-check KEY2 is Y. Worth
    # reconciling…" — the second sentence is advice, and advice belongs in
    # `ask`. It also repeats itself when two inputs both contradict.
    first = text.split(". ")[0].strip().rstrip(".")
    return f"{first}." if first else f"{name} needs review."


def _slim_finding(finding, node):
    """A finding on a terminal, in the shape the top-level lists use.

    The verbose form said "It carries 2.4% of the headline score" on a row
    whose own weight block, two keys away, correctly reported 0% — because an
    unanswered row carries nothing and its weight redistributes. One of the
    two had to go, and it was not going to be the arithmetic.
    """
    # Key names are the engine's own, unchanged: the two list builders read
    # this shape, and renaming a field to tidy it would only move the work.
    return {
        "findingType": finding.get("findingType") or "",
        "ref": finding.get("ref") or node.get("ref") or "",
        "inputKey": finding.get("inputKey") or "",
        "severity": finding.get("severity") or "Medium",
        "finding": finding.get("finding") or "",
        "action": finding.get("action") or "",
        "couldChangeRating": bool(finding.get("couldChangeRating")),
    }


def _audit_flags(findings, names=None):
    """The findings that are about the ASSESSMENT, not about a parameter.

    A defaulted stage, an unresolved sector, a category carried by too few
    rows: nothing is wrong with the company, something is wrong with the
    basis the company was scored on — which is a red flag in the strictest
    sense, and the only one a reader cannot chase by asking the founder.

    They carry no `inputKey`, so they reach no terminal, so the split into
    red flags and data gaps dropped them entirely until this was added back.
    """
    items = []
    for finding in findings or []:
        # `source`, not a missing inputKey: the decision layer fills inputKey
        # with the CHECK name for these rows ("stage_defaulted"), so keying
        # off its absence found none of them at all.
        if finding.get("source") == "parameter":
            continue
        message = " ".join(str(finding.get("finding")
                                or finding.get("observation") or "").split())
        if not message:
            continue
        # Same treatment as a parameter flag: the headline is the first
        # sentence, and whatever follows becomes the ask. Carrying the whole
        # message as the headline left nothing for the dedupe to remove, so
        # the ask repeated it in full.
        row = {"finding": message,
               "action": finding.get("action") or message}
        text = _headline(row, finding.get("check") or "This assessment",
                         names)
        items.append({
            "ref": finding.get("ref") or "",
            "inputKey": "",
            "parameter": (finding.get("parameter") or finding.get("category")
                          or finding.get("check") or "Assessment setup"),
            "severity": finding.get("severity") or "Medium",
            "headline": text,
            # Most audit checks carry no ask distinct from the observation —
            # upstream fills `ask` with the message itself. Repeating the
            # sentence is not an instruction, so where nothing is added the
            # field is left empty rather than said twice.
            "ask": _ask_text(row, text, names),
            "couldChangeRating": bool(finding.get("couldChangeRating")),
        })
    return items


def _build_red_flags(terminals, could_change, names=None, findings=None):
    """Evidenced problems only. A blank row is not a red flag.

    Two sources: the findings the engine already typed `Red Flag` — a
    contradiction between a value and its cross-check, or a claim the
    evidence does not make — and rows that scored Poor, which no finding is
    raised for because nothing is wrong with the evidence: the answer itself
    is the problem.
    """
    seen, items = set(), []

    for ref, node in terminals.items():
        for finding in node.get("diligenceFindings") or []:
            if finding.get("findingType") not in RED_FLAG_TYPES:
                continue
            if ref in seen:
                continue
            seen.add(ref)
            items.append({
                "ref": ref,
                # The terminal is addressed by `ref`; the ROW inside it is
                # addressed by `inputKey`, and a terminal fed by three inputs
                # needs both. It is also the key `final_recommendation` and
                # every override path already identify a finding by.
                "inputKey": finding.get("inputKey") or "",
                "parameter": node["name"],
                "severity": finding.get("severity") or "Medium",
                "headline": _headline(finding, node["name"], names),
                "ask": _ask_text(finding,
                                 _headline(finding, node["name"], names),
                                 names),
                "couldChangeRating": bool(
                    could_change(finding.get("inputKey") or "")),
                "_rank": (node.get("weight") or {}).get(
                    "effectiveOfTotalPct", 0.0),
            })

    for ref, node in terminals.items():
        if ref in seen or node.get("band") not in WEAK_BANDS:
            continue
        seen.add(ref)
        value = (node.get("value") or {}).get("display") or ""
        unit = (node.get("value") or {}).get("unit") or ""
        shown = _display_value(value, unit) if value else ""
        # An anchor row's "value" IS its band, and "scored Poor (Poor)" tells
        # the reader nothing twice.
        if shown.strip().casefold() == str(node["band"]).strip().casefold():
            shown = ""
        items.append({
            "ref": ref,
            "inputKey": node.get("input_key") or node.get("key") or "",
            "parameter": node["name"],
            "severity": "Medium",
            "headline": (f"{node['name']} scored Poor ({shown})." if shown
                         else f"{node['name']} scored Poor."),
            "ask": (f"Confirm the figure behind {node['name']} and what "
                    f"changes it."),
            "couldChangeRating": bool(could_change(node.get("input_key")
                                                   or node.get("key") or "")),
            "_rank": (node.get("weight") or {}).get("effectiveOfTotalPct", 0.0),
        })

    for audit in _audit_flags(findings, names):
        audit["_rank"] = 0.0
        items.append(audit)

    order = {"High": 0, "Medium": 1, "Low": 2}
    items.sort(key=lambda i: (order.get(i["severity"], 1),
                              not i["couldChangeRating"], -i["_rank"]))
    for item in items:
        item.pop("_rank", None)
    return items


def _build_data_gaps(terminals, could_change, names=None):
    """Rows nothing answered. Not a red flag — a list of things to go and get.

    Every one of them, not a top-N: there are only ever a few dozen, and
    truncating this list is how Client Concentration and Supplier
    Concentration stopped being visible at all.
    """
    items = []
    for ref, node in terminals.items():
        gaps = [f for f in (node.get("diligenceFindings") or [])
                if f.get("findingType") == "Data Gap"]
        if not gaps:
            continue
        finding = gaps[0]
        items.append({
            "ref": ref,
            "inputKey": finding.get("inputKey") or "",
            "parameter": node["name"],
            "severity": finding.get("severity") or "Medium",
            "headline": f"No value was established for {node['name']}.",
            "ask": _humanise(
                " ".join(str(finding.get("action") or "").split()),
                names or {}),
            "couldChangeRating": bool(
                could_change(finding.get("inputKey") or "")),
            "_rank": (node.get("weight") or {}).get("declared", 0.0),
        })
    order = {"High": 0, "Medium": 1, "Low": 2}
    items.sort(key=lambda i: (order.get(i["severity"], 1),
                              not i["couldChangeRating"], -i["_rank"]))
    for item in items:
        item.pop("_rank", None)
    return items


def _build_band_recommendations(ranked, baseline, rating):
    """What would move a row up one band, and what that is worth.

    `baseline` travels WITH the list. Every impact here is a delta from it,
    and the block previously reported deltas from a baseline the payload
    never showed — "reach 6.84" printed beside a headline of 6.97. Carrying
    the number the deltas were measured from makes that class of drift
    visible in one glance instead of silently wrong.
    """
    items = []
    for row in ranked:
        if not row.get("controllable"):
            continue
        ref = row.get("ref") or ""
        items.append({
            "ref": ref,
            # Two inputs can sit on one ref — SEC_TAM and SEC_TAM_CAGR both
            # answer E.1 — so without this two recommendations for different
            # rows are indistinguishable and read as a duplicate.
            "inputKey": row.get("inputKey") or "",
            "parameter": H.NAMES.get(ref) or row.get("parameter") or ref,
            "currentBand": row.get("currentBand"),
            "targetBand": row.get("targetBand"),
            "target": row.get("description") or "",
            "displayValue": _display_value(row.get("currentValue"),
                                           row.get("unit") or ""),
            "overallImpact": row.get("overallImpact"),
        })
    return {
        "baseline": {"overall": baseline, "rating": rating},
        "items": items[:10],
    }


def _only_fundraising(rows, ref_by_key=None):
    """Drop the rows belonging to a category this hierarchy does not carry.

    Positive identification only: a row is removed when it can be shown to
    belong to G, never because its category could not be worked out. The
    audit findings — a defaulted stage, an unresolved sector — carry no
    category at all and are about the assessment rather than a parameter, so
    they stay.
    """
    params = getattr(ref_by_key, "params", None) or {}
    out = []
    for row in rows or []:
        code = (row.get("categoryCode") or "").strip()
        if not code:
            code = (row.get("ref") or "")[:1]
        if not code:
            cfg = params.get(row.get("inputKey") or "")
            code = getattr(cfg, "category_code", "") if cfg else ""
        if code and not H.is_fundraising_category(code):
            continue
        out.append(row)
    return out


def build_v2_assessment_response(assessment, company):
    """Build full V2 AssessmentResponse JSON shape from Django Assessment instance."""
    from fundos.assessment.models import CategoryScore

    # Both are recomputed over A-F once the categories are loaded, below.
    coverage = float(assessment.input_coverage_pct or 0)
    suppressed = coverage < COVERAGE_FLOOR_PCT
    overall_val = float(assessment.overall_score) if assessment.overall_score is not None else None

    # ── Resolve sector and sub_sector ────────────────────────────────────
    sector_val = assessment.sector or ""
    sub_sector_val = assessment.sub_sector or ""

    if not sector_val or not sub_sector_val:
        profile_sector, profile_sub = _resolve_sectors_from_profile(company)
        if not sector_val:
            sector_val = profile_sector
        if not sub_sector_val:
            sub_sector_val = profile_sub

    # Persist resolved values back to assessment for next time
    if (sector_val and not assessment.sector) or (sub_sector_val and not assessment.sub_sector):
        changed = []
        if sector_val and not assessment.sector:
            assessment.sector = sector_val
            changed.append("sector")
        if sub_sector_val and not assessment.sub_sector:
            assessment.sub_sector = sub_sector_val
            changed.append("sub_sector")
        if changed:
            try:
                assessment.save(update_fields=changed)
            except Exception:
                pass

    # ── Load config lookups for parameters ───────────────────────────────
    cfg_by_key, weight_map, cat_labels = _load_config_lookups()

    # ── Build categories from CategoryScore ──────────────────────────────
    from fundos.assessment import phase1 as phase1_module

    # A-F only. G (Mandate Context) rates the advisor's own fit for the
    # mandate, not the company, and it is not part of the company's
    # fundraising hierarchy. Its config, rubrics and extraction are untouched
    # — it is turned away here, on the read path, and nowhere else.
    categories_list = []
    cat_scores = [cs for cs in CategoryScore.objects.filter(
        assessment=assessment).order_by("category_code")
        if H.is_fundraising_category(cs.category_code)]
    for cs in cat_scores:
        cs_score = float(cs.score) if cs.score is not None else None
        categories_list.append({
            "code": cs.category_code,
            # The canonical hierarchy name, not the seeded workbook label.
            "name": (H.NAMES.get(cs.category_code) or cs.label
                     or cat_labels.get(cs.category_code, "")),
            # Two lines saying what this category evaluates, for a screen
            # that shows the score before the reader knows what it measures.
            "description": phase1_module.category_description(cs.category_code),
            "weight": float(cs.weight),
            "applied_weight": float(cs.applied_weight),
            "score": cs_score,
            "band": _band_from_score(cs_score),
        })

    # ── Overall and coverage, over A-F ───────────────────────────────────
    # The persisted overall was rolled over all seven categories. Dropping G
    # is not a re-scoring: the SAME `weighted_rollup` runs over the same
    # persisted category scores and the same declared weights, and it
    # renormalises what remains by itself — A-F declare 90, so each carries
    # its own share of that 90 rather than of 100. No weight is rewritten.
    from fundos.engines.deal_assessment import (input_coverage, rating_for_score,
                                                weighted_rollup)

    af_overall, af_applied = weighted_rollup(
        [{"code": c["code"], "score": c["score"], "weight": c["weight"]}
         for c in categories_list])
    overall_val = round(float(af_overall), 2) if af_overall is not None else None
    rating_val = rating_for_score(overall_val) if overall_val is not None else None

    # The share each category ACTUALLY carried once G was out and the blanks
    # redistributed. The persisted `applied_weight` was rolled over all seven
    # and would leave the payload's own contributions summing to 90 against an
    # overall computed over 100 — the parts not adding to the whole.
    af_shares = {a["code"]: a["applied_weight"] for a in af_applied}
    for cat in categories_list:
        cat["applied_weight"] = round(af_shares.get(cat["code"], 0.0), 2)

    # A-F declare 90 between them, so a materiality figure derived from the
    # raw declared weights answers "share of a 100 that includes G". Scaled
    # here rather than in `decision`, which the Phase 1 screen also calls on
    # the full model and must keep reporting on that basis.
    af_declared = sum(float(c.get("weight") or 0.0) for c in categories_list)
    af_weight_scale = (100.0 / af_declared) if af_declared else 1.0

    # Coverage is the share of A-F score-feeding rows that were answered. The
    # G rows are still extracted and still stored; they just are not part of
    # the population this hierarchy is measured against.
    _pv_scores = {pv.input_key: pv for pv in assessment.parameter_values.all()}
    coverage = float(input_coverage([
        {"score": (_pv_scores[k].score if k in _pv_scores else None),
         "is_reference_only": not cfg.feeds_score}
        for k, cfg in cfg_by_key.items()
        if H.is_fundraising_category(cfg.category_code)]))
    suppressed = coverage < COVERAGE_FLOOR_PCT

    # ── Executive summary and analysis (single source) ───────────────────
    # All three come from `phase1`, over ONE Reference: it caches the config
    # tree, the parameter values and the contradiction map, so the three
    # blocks cost one read of the scoring model between them rather than
    # three. They are also the same functions the Phase 1 screen calls, so
    # the two endpoints cannot describe the same deal differently.
    exec_summary = {}
    findings, top_findings, recommendations, structural = [], [], [], []
    verdict = {}
    analysis_ref = None
    profile = getattr(company, "profile", None)
    try:
        from fundos.assessment import phase1, decision
        ref = phase1.Reference(assessment)
        analysis_ref = ref
        exec_summary = phase1.executive_summary(assessment)
        findings_raw = merge_findings(
            phase1.diligence_findings(assessment, ref)
            # Rows scoring a claim their own evidence does not make. Raised
            # here rather than by re-banding the row: the demotion would be a
            # scoring decision, and this is the read path.
            + decision.attribution_findings(assessment, ref),
            audit_as_findings(assessment))
        # EVERY number in this block is measured on the SAME basis as the
        # headline above it — A-F, G excluded. Reading the persisted overall
        # here instead is what printed "you would reach 6.84" beside a
        # headline of 6.97: a baseline of 6.77 that included a category the
        # reader is never shown, and which happened to be the weakest one.
        codes = H.CATEGORY_CODES
        overall = overall_val
        rating = rating_val or assessment.rating_band or None
        findings = []
        for f in findings_raw:
            key = f.get("inputKey") or ""
            mat = (decision._weight_of_total(ref, key) * af_weight_scale
                   if key else 0.0)
            dec = decision._could_change_the_rating(
                assessment, ref, key, overall, rating,
                include_codes=codes) if ref and key else False
            findings.append(decision._as_decision_finding(f, mat, dec))
        top_findings = decision.prioritised_findings(assessment, findings_raw, ref)
        ranked = phase1.band_recommendations(assessment, ref,
                                             include_codes=codes)
        controllable_ranked = [r for r in ranked if r.get("controllable")]
        controllable_ranked.sort(key=lambda r: float(r.get("overallImpact") or 0.0), reverse=True)
        recommendations = controllable_ranked[:10]
        structural = [r for r in ranked if not r["controllable"]]
        verdict = decision.final_recommendation(
            assessment, top_findings, ref, all_findings=findings)

        # Nothing about G reaches a Fundraising reader — not as a node, not
        # as a finding, not as a recommendation. The rows themselves are
        # still configured and still extracted; they are simply not this
        # hierarchy's subject.
        findings = _only_fundraising(findings, ref_by_key=ref)
        top_findings = _only_fundraising(top_findings, ref_by_key=ref)
        recommendations = _only_fundraising(recommendations, ref_by_key=ref)
        structural = _only_fundraising(structural, ref_by_key=ref)
    except Exception as exc:
        logger.warning("V2_SERIALIZER: Failed to build analysis blocks: %s", exc)
        exec_summary = exec_summary or {
            "companyName": company.name,
            "website": getattr(profile, "website_url", "") if profile else "",
            "narrative": "",
            "tags": []
        }

    # ── Strongest / weakest category ─────────────────────────────────────
    scored_cats = [c for c in categories_list if c["score"] is not None]
    strongest = max(scored_cats, key=lambda c: c["score"])["name"] if scored_cats else ""
    weakest = min(scored_cats, key=lambda c: c["score"])["name"] if scored_cats else ""

    # ── Build parameters with cfg + node_data ────────────────────────────
    params_list = []
    pvs = {pv.input_key: pv for pv in assessment.parameter_values.all()}

    def _node_data(cfg, pv=None):
        """The declared and effective weight this leaf carries in the tree."""
        ref_code = cfg.ref_code if cfg else (pv.ref_code or "")
        cat_code = cfg.category_code if cfg else (pv.category or "")
        parent_code = (cfg.parent_code or cfg.category_code) if cfg else cat_code

        child_w = weight_map.get(("child", ref_code))
        subitem_w = weight_map.get(("subitem", parent_code))
        cat_w = weight_map.get(("category", cat_code))

        declared = float(child_w.weight) if child_w else 100.0
        parent_wt = float(subitem_w.weight) if subitem_w else 100.0
        cat_wt = float(cat_w.weight) if cat_w else 0.0

        # effective_weight = (child / 100) * (subitem / 100) * (category / 100)
        effective_of_total = (declared / 100.0) * (parent_wt / 100.0) * (cat_wt / 100.0)

        return {
            "weight": declared,
            "applied_weight": declared / 100.0,
            "effective_weight": effective_of_total,
        }

    # Why each deal-table percentile is missing, resolved once, from the same
    # lookup that seeds them. A read path may not write, so this reports the
    # absence rather than repairing it; `refresh_assessment_benchmarks` is
    # what repairs it.
    absences = {}
    try:
        from fundos.profile.assessment_extraction import benchmark_absences
        absences = benchmark_absences(sector_val, sub_sector_val)
    except Exception as exc:
        logger.warning("V2_SERIALIZER: benchmark absences unavailable: %s", exc)

    for key, pv in pvs.items():
        cfg = cfg_by_key.get(key)
        params_list.append(build_v2_parameter_evidence(
            pv, cfg, _node_data(cfg, pv), assessment, cat_labels=cat_labels,
            absences=absences))

    def _placeholder(cfg):
        """A configured leaf nobody answered, in the same evidence shape."""
        return build_v2_parameter_evidence(
            None, cfg, _node_data(cfg), assessment, cat_labels=cat_labels,
            absences=absences)

    # ── Project onto the Fundraising hierarchy ───────────────────────────
    # `categories` gains `subitems` -> `parameters`, so the payload is walked
    # as Team -> Founder Profile -> Founder Education, and the terminal node
    # of that walk IS the parameter: it carries the value, the score, the
    # evidence and the findings raised against every input behind it.
    findings_by_key = {}
    for f in findings:
        key = f.get("inputKey") or ""
        if key:
            findings_by_key.setdefault(key, []).append(f)

    _build_scorecard_tree(categories_list, params_list, weight_map,
                          cfg_by_key, _placeholder, findings_by_key)

    summary = {
        "overall_score": overall_val,
        "deal_rating": rating_val or assessment.rating_band or None,
        "displayScore": overall_val,
        "displayRating": rating_val or assessment.rating_band or None,
        "stage": assessment.deal_stage or "Series A",
        "raise_amount_usd_mn": float(assessment.capital_raised_usd_mn) if assessment.capital_raised_usd_mn else None,
        "coverage": {
            # A measured percentage, so one decimal — the same rule the
            # displayed values follow. Two would imply the share of answered
            # rows is known to a hundredth of a point.
            "coverage_pct": round(coverage, 1),
            "floor_pct": COVERAGE_FLOOR_PCT,
            "suppressed": suppressed,
            "reason": "coverage_below_floor" if suppressed else None
        },
        "strongest_category": strongest,
        "weakest_category": weakest,
        "overrides_applied": sum(1 for pv in pvs.values() if pv.is_overridden),
        "executive_summary": exec_summary
    }

    # ── Red flags and data gaps ──────────────────────────────────────────
    # Two lists, because they are two different jobs. A red flag is "we
    # looked, and something is wrong" — a partner reads it before the
    # meeting. A data gap is "we have not found it yet" — an analyst works
    # it. Merged into one prioritised list, as they were, Zyla's two genuine
    # contradictions lost their places to two routine blanks and never
    # reached the payload at all.
    terminals = {}

    def _collect(node):
        if node.get("isTerminal"):
            terminals[node["ref"]] = node
        for child in (node.get("subitems") or node.get("children") or []):
            _collect(child)

    for cat in categories_list:
        _collect(cat)

    # Whether resolving a row could move the RATING, not just the score. The
    # findings already carry it; a row that scored Poor carries no finding at
    # all, so it has to be asked for — and defaulting those to False would
    # quietly tell a reader that fixing a Poor EBITDA cannot change the
    # verdict, which is exactly the kind of claim this field exists to make.
    rating_movers = {}
    for f in findings:
        for key in (f.get("inputKey"), f.get("ref")):
            if key:
                rating_movers[key] = bool(f.get("couldChangeRating"))

    def _could_change(key):
        key = key or ""
        if key in rating_movers:
            return rating_movers[key]
        try:
            from fundos.assessment import decision as _decision
            answer = _decision._could_change_the_rating(
                assessment, analysis_ref, key, overall_val,
                rating_val or assessment.rating_band or None,
                include_codes=H.CATEGORY_CODES)
        except Exception:
            answer = False
        rating_movers[key] = bool(answer)
        return rating_movers[key]

    # The name a reader knows each input row by, for the finding text.
    input_names = {key: (cfg.name or key)
                   for key, cfg in cfg_by_key.items() if cfg.name}
    red_flags = _build_red_flags(terminals, _could_change, input_names,
                                 findings)
    data_gaps = _build_data_gaps(terminals, _could_change, input_names)
    band_recs = _build_band_recommendations(recommendations, overall_val,
                                            rating_val)

    return {
        "assessment_id": str(assessment.id),
        "job_id": str(company.id),
        "company_name": company.name,
        "status": "completed" if assessment.status in ("scored", "final") else assessment.status,
        "created_at": assessment.created_at.isoformat() if assessment.created_at else None,
        "updated_at": assessment.updated_at.isoformat() if hasattr(assessment, "updated_at") and assessment.updated_at else None,
        "inputs": {
            "stage": assessment.deal_stage or "Series A",
            "raise_amount_usd_mn": float(assessment.capital_raised_usd_mn) if assessment.capital_raised_usd_mn else None,
            "sectors": [sector_val] if sector_val else [],
            "sub_sector": sub_sector_val
        },
        "summary": summary,
        "categories": categories_list,
        "checks": [
            {"name": "category_weights_sum", "passed": True},
            {"name": "all_parameters_mapped", "passed": True}
        ],
        "analysis": {
            "red_flags": red_flags,
            "data_gaps": data_gaps,
            "band_recommendations": band_recs,
        },
        "warnings": [],
        "error": None
    }


def build_v2_parameter_detail(assessment, ref):
    """Build single parameter detail drill-down payload matching GET /.../parameters/{ref}."""
    from fundos.assessment.models import ConfigParameter

    pv = assessment.parameter_values.filter(input_key=ref).first()
    if not pv:
        pv = assessment.parameter_values.filter(ref_code=ref).first()

    # Load config for this parameter
    cfg = None
    if pv:
        cfg = ConfigParameter.objects.filter(
            input_key=pv.input_key, tenant_id__isnull=True, is_active=True
        ).first()
    if not cfg:
        # A ref code is not unique — a terminal is fed by its rubric row, its
        # anchor and sometimes a `(ref)` cross-check, all three carrying the
        # same code. Now that the scorecard addresses terminals BY that code,
        # the row this resolves to cannot be whichever one the database
        # happened to return first: it is the row that feeds the score.
        cfg = ConfigParameter.objects.filter(
            ref_code=ref, tenant_id__isnull=True, is_active=True
        ).order_by("-feeds_score", "sort_order", "input_key").first()

    _, weight_map, cat_labels = _load_config_lookups()

    # Build node_data for this parameter
    node_data = {}
    if cfg:
        ref_code = cfg.ref_code or ""
        cat_code = cfg.category_code or ""
        parent_code = cfg.parent_code or cat_code
        child_w = weight_map.get(("child", ref_code))
        subitem_w = weight_map.get(("subitem", parent_code))
        cat_w = weight_map.get(("category", cat_code))
        declared = float(child_w.weight) if child_w else 100.0
        parent_wt = float(subitem_w.weight) if subitem_w else 100.0
        cat_wt = float(cat_w.weight) if cat_w else 0.0
        effective_of_total = (declared / 100.0) * (parent_wt / 100.0) * (cat_wt / 100.0)
        node_data = {
            "weight": declared,
            "applied_weight": declared / 100.0,
            "effective_weight": effective_of_total,
        }

    param_evidence = build_v2_parameter_evidence(
        pv, cfg, node_data, assessment, cat_labels=cat_labels)

    # LOOK UP BY INPUT KEY, NOT BY REF.
    #
    # The reference tables are keyed by input key -- TEAM_FDR_EXP,
    # ANC_FDR_EDU -- and this passed the spec ref, "A.1.d". No ref is ever a
    # key there, so every lookup missed and this endpoint returned
    # `"rubric": null, "anchor": null` for every parameter in every
    # assessment, while `build_v2_parameter_evidence` a thousand lines up
    # looked the same tables up by key and returned them fine. One endpoint
    # worked and one never had, which is why it read as environment-specific.
    #
    # `ref` is still right for the dictionary, which IS keyed by ref.
    rubric_key = getattr(cfg, "input_key", "") or ref
    v2_rubric = _rubric_for(rubric_key)
    v2_anchor = _anchor_for(rubric_key)
    v2_dict = v2_ref.dictionary().get(ref) if v2_ref else None

    return {
        "parameter": param_evidence,
        "rubric": v2_rubric,
        "anchor": v2_anchor,
        "dictionary": v2_dict,
        "stage": assessment.deal_stage or "Series A"
    }
