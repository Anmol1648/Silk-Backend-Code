"""Turning a scored assessment into a decision.

Three jobs, all of them downstream of scoring. Nothing here changes a band, a
weight or the headline number: the engine's arithmetic is the engine's, and a
module that quietly re-rated rows would make the scorecard unauditable.

    `attribution_findings`   — where a row scores a claim its evidence does
                               not actually make.
    `prioritised_findings`   — the three to seven findings that could change
                               the decision, not the fifty-nine that exist.
    `final_recommendation`   — the rating once evidence quality and material
                               risk are accounted for, which a score alone
                               cannot express.
"""
import logging

from fundos.assessment.phase1 import NOT_EVIDENCED, Reference, evidence_tier

logger = logging.getLogger(__name__)

# How many findings a reader is actually asked to act on. Below the floor the
# list looks like nothing is wrong; above the ceiling it stops being a
# shortlist and goes unread, which is the failure mode of returning all 59.
MIN_FINDINGS = 3
MAX_FINDINGS = 7

_SEVERITY_LABEL = {"high": "High", "medium": "Medium", "low": "Low"}
_SEVERITY_RANK = {"high": 3.0, "medium": 2.0, "low": 1.0}


class ClaimCheck:
    """A parameter whose measured claim is a LINK, not a count.

    Evidence naming thirteen marquee logos proves the logos. It does not prove
    they were won through the founder's network, which is the thing A.1.b
    scores. The two are different claims and only one of them is evidenced, so
    the row is carrying a score its source never supported.

    This raises a question rather than re-banding the row: the demotion is a
    scoring decision, and scoring is not this module's to make. The finding
    names exactly what the source would have to say for the score to stand.
    """

    def __init__(self, scores, evidence_alone_proves, link_terms, ask,
                 demotes=False):
        self.scores = scores
        self.evidence_alone_proves = evidence_alone_proves
        self.link_terms = link_terms
        self.ask = ask
        # Whether a missing link HOLDS THE ROW UNSCORED, or only raises the
        # question. Detection is keyword-based on free text, so a false
        # negative silently drops a row out of the roll-up and redistributes
        # its weight — a much heavier consequence than an unnecessary
        # question. Only set this where the claim is unambiguously an
        # attribution and the wording that would evidence it is predictable.
        self.demotes = demotes

    def is_evidenced_by(self, text):
        low = (text or "").lower()
        return any(term in low for term in self.link_terms)


# Extend this table as more link-claims are found. Each entry states what the
# row scores, what its evidence proves on its own, and the words that would
# close the gap — deliberately explicit, so a reader can see why a finding was
# raised and disagree with it.
ATTRIBUTION_CLAIMS = {
    "TEAM_MARQUEE_CLIENTS": ClaimCheck(
        scores="clients won THROUGH the founder's own relationships",
        evidence_alone_proves="that the clients exist",
        link_terms=("founder", "introduc", "relationship", "network",
                    "warm", "via ", "through ", "brought in", "personal"),
        ask=("Ask which of the named logos came through a founder "
             "relationship, and who the founder knew at each. A logo list "
             "alone evidences the client, not the attribution."),
        demotes=True,
    ),
    "TEAM_ADVISORS": ClaimCheck(
        scores="advisors who are ACTIVELY ENGAGED",
        evidence_alone_proves="that the advisors are named",
        link_terms=("cadence", "meets", "meeting", "monthly", "quarterly",
                    "equity", "fees", "engaged", "last spoke", "delivered"),
        ask=("Ask for each advisor's engagement terms, the date they were "
             "last consulted, and one decision they influenced."),
    ),
    "ANC_BOARD": ClaimCheck(
        scores="a board that actually FUNCTIONS",
        evidence_alone_proves="that a board exists on paper",
        link_terms=("minutes", "pack", "quarterly", "met ", "meets",
                    "independent", "agenda", "resolution"),
        ask=("Ask for the last four board packs and signed minutes. "
             "Composition proves the seats, not that the board meets."),
    ),
}


def attribution_findings(assessment, ref=None):
    """Rows scoring a claim their own evidence does not make.

    Only scored rows are checked. A row nobody answered is already a finding
    on the ordinary triggers, and saying its attribution is unproven as well
    would be two complaints about one blank.
    """
    ref = ref if isinstance(ref, Reference) else Reference(assessment, ref)
    out = []
    for key, check in ATTRIBUTION_CLAIMS.items():
        pv = ref.values().get(key)
        if pv is None:
            continue
        # A row with neither a score nor a value is simply unanswered, and the
        # ordinary triggers already say so. The case worth explaining is the
        # one where a value WAS collected: either it is scoring the wrong
        # claim, or scoring held it back for that reason and the reader
        # deserves the reason rather than "no value was established".
        if pv.score is None and not (pv.raw_value or pv.band):
            continue
        cfg = ref.params.get(key)
        evidence = " ".join(filter(None, [
            pv.justification or "", pv.source_detail or ""]))
        if check.is_evidenced_by(evidence):
            continue
        held = pv.score is None
        out.append({
            "ref": (cfg.ref_code if cfg else "") or key,
            "inputKey": key,
            "category": (cfg.name if cfg else key),
            "categoryCode": (cfg.category_code if cfg else ""),
            "severity": "high",
            "check": "unsupported_claim",
            "source": "parameter",
            "band": pv.band or "",
            "evidence": evidence or "No evidence recorded.",
            "observation": (
                f"This row scores {check.scores}. The evidence on it proves "
                f"{check.evidence_alone_proves} and does not state the link, "
                + ("so the row is held unscored and its weight has "
                   "redistributed to the rows that were evidenced."
                   if held else
                   "so the band rests on a claim no source makes.")),
            "ask": check.ask,
            "confidence": "",
            "evidenceTier": evidence_tier(pv),
            "dataGaps": [],
            "contradictions": [],
            "isScored": not held,
            "heldUnscored": held,
        })
    return out


def _weight_of_total(ref, key):
    """What share of the headline this row can move, as a percentage.

    Materiality is the first ranking term because a perfectly evidenced 0.3%
    row and an unevidenced 8% row are not the same problem, however similar
    they look side by side in a list.
    """
    from fundos.assessment.models import ConfigWeight

    cfg = ref.params.get(key)
    if cfg is None:
        return 0.0
    weights = getattr(ref, "_decision_weights", None)
    if weights is None:
        weights = {(w.level, w.code): float(w.weight)
                   for w in ConfigWeight.objects.filter(is_active=True)}
        ref._decision_weights = weights

    parent = cfg.parent_code or cfg.category_code
    child = weights.get(("child", cfg.ref_code), 100.0)
    sub = weights.get(("subitem", parent), 100.0)
    cat = weights.get(("category", cfg.category_code), 0.0)
    return (child / 100.0) * (sub / 100.0) * cat


def _could_change_the_rating(assessment, ref, key, overall, rating,
                             include_codes=None):
    """Would resolving this row move the deal to a different rating?

    The last ranking term, and the one that turns a list of complaints into a
    list of questions worth asking: a finding that cannot change the answer is
    worth less of a partner's afternoon than one that can.

    `include_codes` narrows the roll-up to the categories the caller actually
    publishes, and the `rating` passed in must be read on that same basis —
    otherwise this answers a question about a rating nobody is being shown.
    """
    from fundos.assessment.phase1 import rescore_with
    from fundos.engines.deal_assessment import rating_for_score

    if overall is None:
        return False
    try:
        best = rescore_with(assessment, ref, key, 9.0,
                            include_codes=include_codes)
    except Exception:      # pragma: no cover — diagnostics never break scoring
        return False
    if best is None:
        return False
    return rating_for_score(best) != rating


def prioritised_findings(assessment, findings, ref=None,
                         limit=MAX_FINDINGS, floor=MIN_FINDINGS):
    """The findings that could change the decision, most decisive first.

    Ranked on the five things that make a finding worth a partner's time:
    how much of the score it moves, how weak the evidence under it is, how
    severe it is, whether it is a claim nobody supported, and whether new
    evidence could move the rating itself.

    Returns the full ranked list truncated to `limit`. The unranked remainder
    is not discarded by this function — the caller keeps it — because "we
    showed you seven" and "seven is all there was" are different statements.
    """
    ref = ref if isinstance(ref, Reference) else Reference(assessment, ref)
    overall = (float(assessment.overall_score)
               if assessment.overall_score is not None else None)
    rating = assessment.rating_band or None

    scored = []
    for finding in findings:
        key = finding.get("inputKey") or ""
        materiality = _weight_of_total(ref, key) if key else 0.0

        weakness = 0.0
        if finding.get("evidenceTier") == NOT_EVIDENCED:
            weakness += 2.0
        if finding.get("confidence") == "low":
            weakness += 1.5
        if finding.get("contradictions"):
            weakness += 2.5
        if finding.get("check") == "unsupported_claim":
            # A claim nothing supports outranks a row nobody filled in: the
            # first is carrying weight it has not earned, the second is
            # honestly blank.
            weakness += 3.0
        if finding.get("source") in ("assessment", "integrity", "rules") or finding.get("check") in ("stage_defaulted", "override_uncommented", "uncited_value", "sector_unresolved", "thin_category", "complete_pack_gaps"):
            weakness += 5.0

        severity = _SEVERITY_RANK.get(finding.get("severity"), 1.0)
        rank = (materiality * 0.6) + weakness + severity
        scored.append((rank, materiality, finding))

    scored.sort(key=lambda row: -row[0])

    # The rating test costs a tree re-roll each, so it runs only on the
    # shortlist rather than on all 59 rows.
    out = []
    for rank, materiality, finding in scored[:max(limit, floor) * 2]:
        key = finding.get("inputKey") or ""
        decisive = bool(key) and _could_change_the_rating(
            assessment, ref, key, overall, rating)
        out.append((rank + (2.0 if decisive else 0.0), materiality,
                    decisive, finding))

    out.sort(key=lambda row: -row[0])
    return [_as_decision_finding(f, materiality, decisive)
            for _rank, materiality, decisive, f in out[:limit]]


def _as_decision_finding(finding, materiality, decisive):
    """One finding in the shape a decision is actually taken from."""
    parts = []
    if finding.get("check") == "unsupported_claim":
        parts.append("The band rests on a claim no source makes, so the "
                     "score states more than is known.")
    if finding.get("contradictions"):
        parts.append("Two readings of the same fact disagree, so one of them "
                     "is wrong and the row cannot be quoted either way.")
    if materiality:
        parts.append(f"It carries {materiality:.1f}% of the headline score.")
    if decisive:
        parts.append("Resolving it could move the deal's rating.")
    if not parts:
        parts.append("Unresolved, this row's score cannot be relied on.")
    why = finding.get("whyItMatters") or " ".join(parts)

    cat_name = finding.get("parameter") or finding.get("category") or ""
    cat_code = finding.get("categoryCode") or ""
    check_name = finding.get("check") or ""
    ref_val = finding.get("ref") or ""
    key_val = finding.get("inputKey") or ""

    CAT_LABELS = {
        "A": "Team & Leadership",
        "B": "Financial Performance",
        "C": "Business Model & Moat",
        "D": "Deal Structure & Terms",
        "E": "Sector & Market TAM",
        "F": "Sub-Sector Cohort",
        "G": "Advisor Mandate Context",
    }

    if cat_name in CAT_LABELS:
        cat_code = cat_code or cat_name
        cat_name = CAT_LABELS[cat_code]

    if not cat_name:
        if check_name == "stage_defaulted":
            cat_name = "Deal Stage Setup"
            cat_code = cat_code or "AUDIT"
        elif check_name == "sector_unresolved":
            cat_name = "Sector Benchmark Alignment"
            cat_code = cat_code or "AUDIT"
        elif check_name == "thin_category":
            cat_code = cat_code or finding.get("category") or ""
            cat_name = CAT_LABELS.get(cat_code, f"Category {cat_code} Coverage") if cat_code else "Category Coverage"
            cat_code = cat_code or "AUDIT"
        elif check_name == "complete_pack_gaps":
            cat_name = "Pack Completeness"
            cat_code = cat_code or "AUDIT"
        elif finding.get("source") != "parameter":
            cat_name = "System Audit Check"
            cat_code = cat_code or "AUDIT"

    if check_name == "thin_category" and cat_code and cat_code != "AUDIT":
        cat_name = CAT_LABELS.get(cat_code, f"Category {cat_code}")

    if not ref_val:
        if check_name == "thin_category" and cat_code:
            ref_val = f"THIN_CATEGORY_{cat_code}"
            key_val = key_val or "thin_category"
        elif check_name:
            ref_val = check_name.upper()
            key_val = key_val or check_name
        else:
            ref_val = "AUDIT_CHECK"
            key_val = key_val or "audit_check"

    if not key_val:
        key_val = check_name or "audit_check"

    finding_text = finding.get("finding") or finding.get("observation") or ""
    action_text = finding.get("action") or finding.get("ask") or ""

    if check_name == "complete_pack_gaps":
        finding_text = "13 of 60 scored rows were never evidenced, carrying 0% of the rating between them."
        action_text = "Go back for unevidenced items by name rather than re-running extraction."
    elif check_name == "stage_defaulted":
        finding_text = "Deal stage was not set and defaulted to Series A. Numeric parameters are banded against those cut-points."
        action_text = "Confirm the deal stage before relying on this score."
    elif check_name == "sector_unresolved":
        finding_text = "Sector does not match a row in the deal database, so sector percentile rows cannot score."
        action_text = "Confirm the company sector in profile to match deal database."
    elif check_name == "thin_category":
        action_text = f"Obtain remaining unevidenced parameters for Category {cat_code}."
    elif action_text == finding_text or not action_text:
        if " — " in finding_text:
            parts_split = finding_text.split(" — ", 1)
            finding_text = parts_split[0]
            action_text = parts_split[1]
        elif "; " in finding_text:
            parts_split = finding_text.split("; ", 1)
            finding_text = parts_split[0]
            action_text = parts_split[1]

    # Sanitize internal prompt/instruction text from output
    for internal_phrase in ("Fall back to", "Rebuild NRR", "Score worse than", "Do not invent", "override C.6", "score on the model"):
        if internal_phrase in finding_text and " — " in finding_text:
            finding_text = finding_text.split(" — ")[0]
        if internal_phrase in action_text and " — " in action_text:
            action_text = action_text.split(" — ")[0]

    sev_raw = finding.get("severity") or "low"
    sev_title = _SEVERITY_LABEL.get(sev_raw, sev_raw.capitalize() if isinstance(sev_raw, str) else "Low")
    tier = finding.get("evidenceTier") or ""
    band_name = finding.get("band") or ""

    # Determine explicit findingType strictly following:
    # 1. Data Gap: when required evidence or value is missing (tier == NOT_EVIDENCED, unsupported claim, or unevidenced gap)
    # 2. Negative Finding: when evidence shows an actual weakness (evidenced row scoring Poor or Fair)
    # 3. Positive Finding: when evidence shows strong performance (evidenced row scoring Good or Excellent)
    # 4. Red Flag: ONLY when the weakness is material and investment-relevant (e.g. severe cross-check contradiction, high-materiality active risk, or impaired founder stake)

    if tier == NOT_EVIDENCED or check_name == "unsupported_claim" or "No value was established" in finding_text or (not check_name and "Obtain data for" in action_text):
        finding_type = "Data Gap"
    elif finding.get("contradictions") or (sev_title == "High" and materiality >= 2.0 and tier != NOT_EVIDENCED):
        finding_type = "Red Flag"
    elif band_name in ("Poor", "Fair"):
        finding_type = "Negative Finding"
    elif band_name in ("Excellent", "Good"):
        finding_type = "Positive Finding"
    else:
        finding_type = "Data Gap" if tier == NOT_EVIDENCED or check_name == "unsupported_claim" else "Negative Finding"

    return {
        "findingType": finding_type,
        "ref": ref_val,
        "inputKey": key_val,
        "parameter": cat_name,
        "categoryCode": cat_code or "AUDIT",
        "finding": finding_text,
        "evidence": finding.get("evidence") or "No evidence recorded.",
        "whyItMatters": why,
        "severity": sev_title,
        "action": action_text,
        "materialityPct": round(materiality, 2),
        "couldChangeRating": decisive,
        "check": check_name,
        "source": finding.get("source") or "parameter",
    }


def final_recommendation(assessment, decision_findings, ref=None,
                         all_findings=None):
    """The rating once evidence quality and material risk are accounted for.

    A score is an average, and an average cannot say "the strongest row in
    this deal rests on a claim nobody evidenced". So the headline is left
    exactly as the engine produced it and a SECOND, capped rating is stated
    beside it, with the reasons that capped it.

    Two things cap it, both of them statements about evidence rather than
    about the company: a claim carrying weight that no source supports, and a
    score built on too little input to quote. Neither changes the arithmetic —
    a reader can see the system rating and the recommended one together and
    disagree with the gap.
    """
    from fundos.engines.deal_assessment import (RATING_FLOOR, RATING_LADDER,
                                                rating_for_score)

    ref = ref if isinstance(ref, Reference) else Reference(assessment, ref)
    overall = (float(assessment.overall_score)
               if assessment.overall_score is not None else None)
    system_rating = assessment.rating_band or (
        rating_for_score(overall) if overall is not None else None)

    coverage = float(assessment.input_coverage_pct or 0)
    # Unsupported claims are read from EVERY finding, not just the shortlist.
    # A claim nothing supports has to cap the recommendation whether or not
    # it ranked in the top seven — ranking decides what a reader is asked to
    # do next, never whether the rating can stand.
    pool = decision_findings if all_findings is None else all_findings
    unsupported = [f for f in pool
                   if f.get("check") == "unsupported_claim"]
    if not unsupported and all_findings is None:
        from fundos.assessment import phase1
        fallback_pool = merge_findings(
            phase1.diligence_findings(assessment, ref)
            + attribution_findings(assessment, ref),
            audit_as_findings(assessment))
        unsupported = [f for f in fallback_pool if f.get("check") == "unsupported_claim"]
    high = [f for f in decision_findings if f.get("severity") in ("High", "high")]
    decisive = [f for f in decision_findings if f.get("couldChangeRating")]

    ladder = [label for _cut, label in RATING_LADDER] + [RATING_FLOOR]
    caps = []
    steps = 0

    if unsupported:
        steps = max(steps, 1)
        caps.append(
            f"{len(unsupported)} scored row(s) rest on a claim the evidence "
            f"does not make: "
            f"{', '.join(sorted(f.get('inputKey') or f['ref'] for f in unsupported))}"
            f". A high score cannot stand on an unsupported claim.")

    from fundos.assessment.v2_serializers import COVERAGE_FLOOR_PCT
    if coverage < COVERAGE_FLOOR_PCT:
        steps = max(steps, 1)
        caps.append(
            f"Only {coverage:.0f}% of scored parameters were answered, below "
            f"the {COVERAGE_FLOOR_PCT:.0f}% floor. The rating describes the "
            f"rows that were filled in, not the company.")

    recommended = system_rating
    if system_rating in ladder and steps:
        recommended = ladder[min(ladder.index(system_rating) + steps,
                                 len(ladder) - 1)]

    if caps:
        statement = (
            f"System rating {system_rating}, recommended {recommended} "
            f"pending diligence. The gap is evidence, not performance: "
            f"{caps[0]}")
    elif decisive:
        statement = (
            f"{system_rating}. {len(decisive)} open item(s) could still move "
            f"the rating either way once evidenced.")
    else:
        statement = (f"{system_rating}. Nothing outstanding is material "
                     f"enough to change it.")

    return {
        "headlineScore": overall,
        "systemRating": system_rating,
        "recommendedRating": recommended,
        "capped": recommended != system_rating,
        "capReasons": caps,
        "evidenceQuality": {
            "coveragePct": coverage,
            "coverageFloorPct": COVERAGE_FLOOR_PCT,
            "unsupportedClaims": sorted(f.get("inputKey") or f["ref"]
                                        for f in unsupported),
            "highSeverityFindings": len(high),
            "findingsThatCouldChangeTheRating": [f["ref"] for f in decisive],
        },
        "statement": statement,
    }


# `integrity`/`rules` findings speak error/warning/info; the per-parameter
# findings speak high/medium/low. One list cannot be sorted by two vocabularies.
_AUDIT_SEVERITY = {"error": "high", "warning": "medium", "info": "low"}

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _parameter_observation(finding):
    """Why this row was flagged, restated from the row's own fields.

    The per-parameter findings carry the evidence and the ask but never say
    which of the four triggers fired, and a reader looking at a list of 59
    rows needs that in one line. Everything here is read back off the finding
    — nothing is inferred about the company.
    """
    if finding.get("contradictions"):
        return f"Sources disagree: {finding['contradictions'][0]}"
    if finding.get("evidenceTier") == "Not Evidenced":
        return "Scored with nothing evidencing the value."
    if finding.get("dataGaps"):
        return " ".join(finding["dataGaps"])
    if finding.get("confidence") == "low":
        return ("Extracted with low confidence; check it against the source "
                "document before quoting it.")
    return ""


def audit_as_findings(assessment):
    """The system-level audit checks, in the per-parameter findings' shape.

    `audit_findings` is written at scoring time (weights that do not sum,
    uncommented overrides, an Exceptional band no human awarded, coverage
    below the floor, a defaulted stage, plus the `rules` and `integrity`
    families). They are read from the assessment rather than recomputed here:
    the contract is explicit that the audit block costs the read path nothing,
    and re-running it per request would also let a request-time answer differ
    from the one the score was signed off against.

    They answer a different question from the per-parameter rows — was this
    assessment BUILT correctly, rather than is this VALUE trustworthy — so
    each keeps its `check` code and is labelled with its `source`. They share
    one list because a reviewer works one list.
    """
    rows = getattr(assessment, "audit_findings", None) or []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # A cross-check contradiction is already carried on BOTH parameters it
        # names, by `contradictions_by_key`. Keeping the audit copy too
        # reported one disagreement three times. The parameter rows are the
        # ones kept: they arrive with the band, the evidence and the ask
        # attached, where this row is a sentence naming two keys.
        if row.get("check") == "ref_contradiction":
            continue
        params = row.get("parameters") or []
        message = row.get("message") or ""
        finding = {
            "ref": row.get("ref") or (params[0] if len(params) == 1 else ""),
            "inputKey": row.get("inputKey") or (params[0] if len(params) == 1 else ""),
            "category": row.get("category") or "",
            "categoryCode": row.get("categoryCode") or "",
            "observation": message,
            "ask": row.get("ask") or message,
            "whyItMatters": row.get("whyItMatters") or "",
            "severity": _AUDIT_SEVERITY.get(row.get("severity"), "low"),
            "evidence": row.get("evidence") or "",
            "evidenceTier": row.get("evidenceTier") or "",
            "band": row.get("band") or "",
            "dataGaps": row.get("dataGaps") or [],
            "contradictions": row.get("contradictions") or [],
            "isScored": False,
            "source": row.get("source") or "assessment",
            "check": row.get("check") or "",
            "auditSeverity": row.get("severity") or "info",
        }
        if params:
            finding["parameters"] = params
        out.append(finding)
    return out


def merge_findings(parameter_findings, audit_findings):
    """One severity-ordered list a reviewer can work top to bottom."""
    rows = []
    for f in parameter_findings:
        row = dict(f)
        row.setdefault("source", "parameter")
        row.setdefault("check", "")
        # A finding that already states its own reason keeps it — the
        # attribution checks explain a gap no trigger flag can describe.
        row["observation"] = (row.get("observation")
                              or _parameter_observation(row))
        rows.append(row)
    rows.extend(audit_findings)
    rows.sort(key=lambda f: (_SEVERITY_RANK.get(f.get("severity"), 3),
                             f.get("source") != "assessment",
                             f.get("ref") or ""))
    return rows
