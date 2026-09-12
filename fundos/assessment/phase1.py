"""Stage 2 Phase 1 — the four objects the Deal Scorecard screen renders.

WHAT THIS MODULE IS
-------------------
A projection, not an engine. Every score in here is produced by
`fundos.engines.deal_assessment` and persisted by
`fundos.assessment.services.run_scoring`; this module reads what those two
wrote and reshapes it into the object the React screen consumes. Nothing here
bands a value, chooses a threshold or averages a category, and it must stay
that way: a number computed here would be a second source of truth that drifts
from the scorecard the rest of the system serves.

The roll-up is the one place that looks like arithmetic and is not. The engine
exposes `weighted_rollup` — the §2.3.1 primitive that averages scored children
on their weights and redistributes weight around blanks — and `score_assessment`
calls it recursively but returns only the top level, because the persisted
`CategoryScore` rows are all the rest of the system needed. The screen draws
the whole tree, so `scored_tree()` below calls the SAME primitive recursively
and keeps the nesting. It is the engine's arithmetic, read at a depth nothing
had asked for before.

THE FOUR BLOCKS
---------------
``executiveSummaryData``  the company in a paragraph, from its profile, plus
                          the cohort inputs every threshold switches on.
``dealScorecardData``     the nested category tree with the evidence behind
                          each leaf.
``diligenceFindingsData`` the rows a reader should not take at face value.
``bandRecommendationsData`` what would move a Fair or Good row up one band,
                          quoted from the published rubric or anchor.

THE FOUR DILIGENCE TRIGGERS, AND WHERE EACH ONE LIVES IN THIS SCHEMA
---------------------------------------------------------------------
The screen's contract names four reasons a row deserves a question. This
schema does not carry those four as columns, and no column was added for
them — an empty column populated by nothing would make the list look
implemented while returning nothing on real data. Each maps onto a field this
system already writes and already relies on:

  1. LOW CONFIDENCE
     Wanted: `confidence == "low"`.
     Stored: `ParameterValue.confidence`, a 0-1 decimal written by
     `extraction._ask` from the model's own self-report and carried through
     `profile_bridge.merge_value`.
     Used:   `confidence < extraction.CONFIDENCE_FLOOR`. That constant is the
     same one `extract_for_assessment` counts its own `low_confidence` tally
     against, so a row that reads low here is a row extraction already
     flagged. Equivalent because it is the same judgement on the same number,
     not a second opinion about it.

  2. NOT EVIDENCED
     Wanted: `evidence_tier == "Not Evidenced"`.
     Stored: provenance is a SOURCE TIER (1-4, `profile_bridge`), not the
     five-word vocabulary — and a parameter nothing answered has no
     `ParameterValue` row at all, which is the schema's way of saying it.
     Used:   `evidence_tier()` below. The tier ladder projects onto the
     vocabulary, and absence projects onto Not Evidenced:

         founder-confirmed, or tier 1 (the founder's own documents) -> Verified
         tier 2 (the company's own site, filings, LinkedIn)         -> Management
         tier 3-4 (third parties, or inferred)                      -> Estimate
         a value with no tier recorded at all                       -> Estimate
         no value, or no ParameterValue row at all             -> Not Evidenced

     Only the last line is load-bearing and it is the unambiguous one. It is
     also exactly the set `input_coverage()` counts as unanswered, so the
     diligence list and the coverage percentage can never disagree about which
     rows are missing.

  3. CONTRADICTIONS
     Wanted: a non-empty `contradictions` list.
     Stored: `Assessment.audit_findings`, written by
     `services._reference_contradictions` — the §2.4.5 cross-check rule, which
     surfaces a ref row scoring two or more bands below the scored sibling it
     cross-checks.
     Used:   `contradictions_by_key()` below indexes those findings onto the
     rows they name. Equivalent because it is the same concept the prototype
     means — two sources of truth about one parameter disagreeing — and this
     system already computes it, once, at scoring time. The rule is not
     re-applied here; only its output is redistributed, so the diligence list
     and the audit block cannot drift apart.

  4. DATA GAPS
     Wanted: a non-empty `data_gaps` list.
     Stored: `ParameterValue.notes`, written by `extraction` from the
     extraction prompt's `"ask": "what to request if unclear"` field. That is
     the same object: a statement, about a parameter, of what is missing.
     Used:   `data_gaps()` below returns those notes verbatim.

     A parameter with NO row has no notes to carry, and is deliberately not
     counted as a data gap here — it is already caught by trigger 2, and
     counting it twice would make the two triggers indistinguishable. It still
     reaches the screen, and `ask` still tells the reader which document would
     answer it, via `parameter_sources.source_for`. No trigger is dropped: the
     union of the four covers every row either the prototype or this schema
     would flag.

BANDS
-----
One vocabulary: the capitalised one `run_scoring` writes, from the engine's
`BAND_SCORES` keys. `normalise_band()` exists only to read rows written by an
older code path that stored them lower-cased; it introduces no third spelling.
"""
import copy
import logging
import re
from decimal import Decimal

from fundos.engines.deal_assessment import weighted_rollup

logger = logging.getLogger(__name__)

#: Below this, `extraction` already counts an answer as low confidence. Reused
#: rather than restated so "low" means one thing across the system.
from fundos.assessment.extraction import CONFIDENCE_FLOOR  # noqa: E402

#: Best first — the order a band is advanced along.
BAND_ORDER = ("Excellent", "Good", "Fair", "Poor")

#: The bands a recommendation is offered for. `Poor` is deliberately absent:
#: the screen presents these as one step from better, and a Poor row's problem
#: is rarely one step. `Excellent` has nothing above it.
RECOMMEND_FROM = frozenset({"Fair", "Good"})

NOT_EVIDENCED = "Not Evidenced"

#: Where the executive summary's prose comes from, in the order it reads: what
#: the company does, then why it is worth funding. Section key to the
#: `profile.schema` field names that hold narrative text — named explicitly so
#: a schema change surfaces here rather than silently emptying the block.
#: The profile prose the executive summary quotes, and how much of each it
#: takes. The profile writes at length on purpose — it is a founder-facing
#: document someone reads end to end. This block is the opposite: the line a
#: partner reads before deciding whether to open anything else, and two
#: thousand words of it is not a summary, it is the document again.
#:
#: Budgeted PER FIELD rather than per section. `investment_thesis` joins the
#: opportunity and the leadership assessment into one string, so a budget
#: applied after the join spends everything on the opportunity and drops the
#: team read entirely — which is half of what the section is for.
#: (section key, ((field, max sentences, max chars), ...))
NARRATIVE_SOURCES = (
    ("company_overview", (("description_of_business", 2, 300),)),
    ("investment_thesis", (("opportunity_explanation", 2, 300),
                           ("leadership_assessment", 1, 220))),
)

#: Sentence boundary: a full stop, question or exclamation mark followed by
#: whitespace and a capital. Deliberately not a bare "." — the prose is full
#: of "USD 28.5 billion", "2.1x" and "37.6% CAGR", and splitting on every
#: period cuts those in half.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"“(]?[A-Z0-9])")


def condense(text, max_sentences, max_chars):
    """The opening sentences of `text`, whole, within both budgets.

    Never cuts mid-sentence and never appends an ellipsis: a summary that
    stops in the middle of a clause reads as a bug, and the full prose is one
    click away on the profile. The first sentence is always kept even when it
    alone exceeds the character budget — a summary of nothing is worse than a
    long first line.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    sentences = _SENTENCE_END.split(text)
    kept = []
    for sentence in sentences[:max_sentences]:
        candidate = " ".join(kept + [sentence])
        if kept and len(candidate) > max_chars:
            break
        kept.append(sentence)
    return " ".join(kept).strip()


class Reference:
    """Every config row this screen needs, read ONCE per request.

    WHY THIS EXISTS
    ---------------
    The four blocks are built from the same handful of config tables, and the
    obvious shape — look each row up where it is needed — made the cost of one
    page load scale with the size of the scoring model. Measured before this
    class: **412 SQL queries** for a 62-leaf scorecard. Two of those queries
    per leaf were the rubric and the anchor; three more per rolled-up node were
    the scoring key, re-read to label a band; and each band recommendation
    rebuilt the entire parameter tree from scratch to re-roll it.

    None of that is per-parameter data. The rubric table, the anchor table, the
    parameter dictionary and the scoring key are the SAME for every row on the
    page, and the tree is the same for every recommendation. Read once, held
    for the life of one request, discarded with it.

    Deliberately NOT a process-level cache. Config is versioned and editable in
    Admin, and a scorecard that kept serving a threshold the product owner has
    already changed would be exactly the drift the versioning exists to
    prevent. Per-request is the longest a copy may safely live.
    """

    def __init__(self, assessment, tenant_id=None):
        from fundos.assessment.models import (ConfigAnchor, ConfigParameter,
                                              ConfigRubric)
        from fundos.assessment.services import band_score

        self.assessment = assessment
        self.tenant_id = (tenant_id if tenant_id is not None
                          else getattr(assessment, "tenant_id", None))
        version = assessment.config_version
        stage = assessment.deal_stage

        # Platform rows first, tenant rows overwrite — the same precedence
        # `services._cfg` applies one row at a time, applied here in bulk.
        self.params = {}
        for row in ConfigParameter.objects.filter(
                is_active=True, tenant_id__isnull=True):
            self.params[row.input_key] = row
        for row in ConfigParameter.objects.filter(
                is_active=True, tenant_id=self.tenant_id):
            self.params[row.input_key] = row

        self.rubrics = {}
        for row in ConfigRubric.objects.filter(
                version=version, stage=stage).order_by("tenant_id"):
            self.rubrics[row.input_key] = row

        self.anchors = {}
        for row in ConfigAnchor.objects.filter(
                version=version).order_by("tenant_id"):
            self.anchors[row.input_key] = row

        # The scoring key, read once. `band_from_score` is still the one place
        # that decides which band a number lands in; it is handed the cuts it
        # would otherwise re-read for every node on the page.
        self.band_cuts = []
        for band in ("Excellent", "Good", "Fair"):
            cut = band_score(band, self.tenant_id)
            if cut is not None:
                self.band_cuts.append((band, float(cut)))

        self._tree = None
        self._values = None
        self._contradictions = None
        self._obtainable = None

    # -- lazily built, then held ----------------------------------------

    def tree(self):
        """The parameter tree as `services._build_tree` assembles it.

        Built at most once. Callers that need to modify it (the recommendation
        re-roll) must take their own copy — see `rescore_with`.
        """
        if self._tree is None:
            from fundos.assessment.services import _build_tree
            self._tree = _build_tree(self.assessment, self.tenant_id)
        return self._tree

    def values(self):
        if self._values is None:
            self._values = {pv.input_key: pv
                            for pv in self.assessment.parameter_values.all()}
        return self._values

    def contradictions(self):
        if self._contradictions is None:
            self._contradictions = contradictions_by_key(self.assessment)
        return self._contradictions

    def obtainable(self):
        """The keys step 1 asks for — read once, handed to `source_for`."""
        if self._obtainable is None:
            try:
                from fundos.profile.assessment_extraction import obtainable_keys
                self._obtainable = obtainable_keys(self.tenant_id)
            except Exception:      # pragma: no cover — import-order safety
                self._obtainable = set()
        return self._obtainable

    def source_for(self, cfg):
        from fundos.assessment.parameter_sources import source_for
        return source_for(cfg, obtainable=self.obtainable())

    def band_score(self, band):
        for name, cut in self.band_cuts:
            if name == band:
                return cut
        from fundos.assessment.services import band_score as lookup
        return lookup(band, self.tenant_id)

    def display_band(self, score):
        from fundos.assessment.services import band_from_score
        return band_from_score(score, self.tenant_id, cuts=self.band_cuts)


# What each category is actually asking, in the two lines a screen has room
# for. Copy, not configuration: these describe the scoring model itself, which
# is the same for every tenant, so a row in the database would only invite
# seven tenants to disagree about what "Business Quality" means.
CATEGORY_DESCRIPTIONS = {
    "A": ["Assesses founder credibility, domain track record and the "
          "leadership seats already filled.",
          "Judges whether this team can execute the plan at the next stage "
          "of scale."],
    "B": ["Examines revenue quality, growth, margin trajectory, burn and "
          "runway.",
          "Tests whether the numbers support the raise and the valuation "
          "sought."],
    "C": ["Measures durability: retention, pricing power, unit economics and "
          "defensibility.",
          "Separates a structurally advantaged business from one that is "
          "merely growing."],
    "D": ["Covers the terms on offer — valuation, structure, use of proceeds "
          "and seller motivation.",
          "Determines whether the entry point is attractive independently of "
          "company quality."],
    "E": ["Benchmarks the broad sector on deal velocity, ticket size and "
          "active investor depth.",
          "Indicates how freely capital and exits move in this market."],
    "F": ["Benchmarks the company's specific niche against its clubbed peer "
          "cohort.",
          "Narrows the sector view to the population this deal will actually "
          "be compared with."],
    "G": ["Tests fit against the fund's own mandate: stage, cheque size, "
          "geography and thesis.",
          "Decides whether a good company is a deal this fund can actually "
          "do."],
}


def category_description(code):
    """The two-line description for a category code, or an empty list."""
    return list(CATEGORY_DESCRIPTIONS.get((code or "").strip().upper(), []))


def normalise_band(band):
    """Read a stored band into the engine's own capitalisation.

    `run_scoring` writes the capitalised vocabulary — the keys of
    `BAND_SCORES` — and that is the only spelling this module emits. Rows
    written by an older override path stored them lower-cased, and those rows
    are still in live databases: a `"good"` left unread would drop a real
    parameter out of the recommendations for no reason a user could see.

    This is a read-side normalisation, not a second representation. Anything
    unrecognised comes back untouched rather than being guessed at.
    """
    text = str(band or "").strip()
    if not text:
        return ""
    for known in BAND_ORDER:
        if known.casefold() == text.casefold():
            return known
    return text


# ---------------------------------------------------------------------------
# Reference resolution — the published rule at the version that was applied
# ---------------------------------------------------------------------------

def _rubric(ref, input_key):
    """The numeric cut-points this parameter was banded against.

    Read at the assessment's own `config_version` and stage, never live: a
    threshold may have moved since this score was produced, and a screen that
    quoted today's rule beside yesterday's number would be describing a
    scorecard that does not exist. `Reference` reads the whole table once and
    this picks a row out of it.
    """
    return ref.rubrics.get(input_key)


def _anchor(ref, input_key):
    return ref.anchors.get(input_key)


def _fmt(value, unit=""):
    """Render a cut-point for a sentence, keeping the unit legible."""
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value)).normalize()
    except Exception:
        return str(value)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if unit == "%":
        return f"{text}%"
    return f"{text} {unit}".strip()


def band_definitions(ref, input_key, unit=""):
    """The four band definitions for one parameter, in the reader's words.

    An anchor states them; a numeric rubric states cut-points, which are
    phrased here at the stage the assessment was scored at, in the rubric's own
    direction. Returns an empty dict for a lookup parameter — a deal-database
    percentile has thresholds but they are not a claim about the company, and a
    threshold shown beside a score it did not produce is worse than none.
    """
    assessment = ref.assessment
    anchor = _anchor(ref, input_key)
    if anchor is not None:
        return {
            "Excellent": anchor.excellent_def,
            "Good": anchor.good_def,
            "Fair": anchor.fair_def,
            "Poor": anchor.poor_def,
        }

    rubric = _rubric(ref, input_key)
    if rubric is None:
        return {}

    unit = rubric.unit or unit
    metric = rubric.metric_name or input_key
    if rubric.direction == "Range":
        if rubric.ideal_min is None or rubric.ideal_max is None:
            return {}
        ideal = f"{_fmt(rubric.ideal_min, unit)} to {_fmt(rubric.ideal_max, unit)}"
        good = rubric.good_tolerance_pct or 0
        fair = rubric.fair_tolerance_pct or 0
        return {
            "Excellent": f"{metric} inside the ideal band of {ideal}.",
            "Good": (f"{metric} outside the ideal band of {ideal} but within "
                     f"{_fmt(good)}% of it."),
            "Fair": (f"{metric} outside the ideal band of {ideal} but within "
                     f"{_fmt(fair)}% of it."),
            "Poor": f"{metric} further than {_fmt(fair)}% from {ideal}.",
        }

    reach = "at least" if rubric.direction == "Higher" else "at most"
    miss = "below" if rubric.direction == "Higher" else "above"
    stage = assessment.deal_stage
    return {
        "Excellent": f"{metric} of {reach} {_fmt(rubric.cut_excellent, unit)} at {stage}.",
        "Good": f"{metric} of {reach} {_fmt(rubric.cut_good, unit)} at {stage}.",
        "Fair": f"{metric} of {reach} {_fmt(rubric.cut_fair, unit)} at {stage}.",
        "Poor": f"{metric} {miss} {_fmt(rubric.cut_fair, unit)} at {stage}.",
    }


def next_band(band):
    """The band one step above this one, or None at the top of the scale."""
    if band not in BAND_ORDER or band == BAND_ORDER[0]:
        return None
    return BAND_ORDER[BAND_ORDER.index(band) - 1]


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def evidence_tier(pv):
    """Project a stored source tier onto the scorecard's evidence vocabulary.

    See the module docstring for the mapping and for why only Not Evidenced
    carries weight.
    """
    if pv is None:
        return NOT_EVIDENCED
    if not (pv.raw_value or "").strip() and not (pv.band or "").strip():
        return NOT_EVIDENCED
    if (pv.source_type or "") == "founder":
        return "Verified"
    tier = pv.source_tier
    if tier == 1:
        return "Verified"
    if tier == 2:
        return "Management"
    return "Estimate"


def confidence_label(pv):
    """low / high, on the one threshold this system already applies.

    Confidence is stored as a 0-1 number; the screen reads a word. The cut is
    `extraction.CONFIDENCE_FLOOR` and nothing else — the same number
    `extract_for_assessment` counts its own low-confidence tally against, so a
    row that reads low here is a row extraction already flagged.

    There is deliberately no middle band. A third level would need a second
    threshold, and this system does not have one to read: inventing a number
    here would put a cut-point on the screen that no configuration owns.
    """
    if pv is None or pv.confidence is None:
        return ""
    return "low" if float(pv.confidence) < CONFIDENCE_FLOOR else "high"


def contradictions_by_key(assessment):
    """Recorded contradictions, indexed by the parameter each one concerns.

    Read from the assessment's own `audit_findings`, which is where
    `run_integrity_checks` put them — the cross-check rule (§2.4.5) is applied
    once, at scoring time, and this only redistributes its output onto the rows
    it names. Applying the rule a second time here would let the diligence list
    and the audit block disagree about the same deal.
    """
    out = {}
    for finding in (assessment.audit_findings or []):
        # Only the cross-check rule states that two readings of one fact
        # disagree. Other checks name parameters too — `tam_inverted` carries
        # SEC_TAM and SEC_TAM_SUB — and reading every such row as a
        # contradiction put "sources disagree" on rows where nothing did.
        if finding.get("check") != "ref_contradiction":
            continue
        keys = finding.get("parameters") or []
        if not keys and finding.get("parameter"):
            keys = [finding["parameter"]]
        for key in keys:
            out.setdefault(key, []).append(
                finding.get("message") or finding.get("observation") or "")
    return out


def data_gaps(pv):
    """Stated gaps on this row — the extraction's own "what to request".

    `ParameterValue.notes` is written by `extraction` from the prompt's
    `"ask": "what to request if unclear"` field, which is the same object the
    prototype calls a data gap: a statement, about a parameter, of what is
    missing. Returned verbatim; nothing is synthesised.

    An unanswered parameter has no notes and produces nothing here. That is
    deliberate — it is already caught as Not Evidenced, and counting it in both
    places would make the two triggers indistinguishable. `missing_value_ask`
    is what gives such a row something actionable to show.
    """
    if pv is None:
        return []
    note = (pv.notes or "").strip()
    return [note] if note else []


def missing_value_ask(pv, cfg, ref=None):
    """What to request for a row nothing answered, naming the document.

    Not a data gap — a derived next action for one. "Coverage is low" is not a
    task; "the financial model carries this" is, which is the whole reason
    `parameter_sources` records who is responsible for each parameter.

    :param ref: the request's `Reference`, so the source lookup reuses the
        key set it already read rather than re-reading it per row.
    """
    if pv is not None and pv.score is not None:
        return ""
    name = (getattr(cfg, "name", "")
            or getattr(pv, "input_key", "") or "This parameter")
    if cfg is None:
        where = ""
    elif ref is not None:
        where = ref.source_for(cfg)
    else:
        from fundos.assessment.parameter_sources import source_for
        where = source_for(cfg)
    if where:
        return f"Obtain data for {name} (source: {where})."
    return f"Obtain documentation or data for {name}."


# ---------------------------------------------------------------------------
# The scored tree
# ---------------------------------------------------------------------------

def scored_tree(assessment, tenant_id=None):
    """The full nested roll-up, using the engine's own §2.3.1 primitive.

    Returns ``(overall, categories)``. Each node carries `score`, its declared
    `weight` and the `applied_weight` it actually carried once unscored
    siblings dropped out — the two differ exactly where part of the branch was
    not evidenced, which is the thing a founder most needs to see and the
    reason `weighted_rollup` reports it at all.

    The overall figure this produces is the same number `run_scoring` persisted
    on the assessment, because it is the same function over the same tree. It
    is recomputed rather than read so the nested levels between the leaves and
    the categories — which nothing persists — are consistent with the headline
    rather than being a second, looser calculation.
    """
    ref = tenant_id if isinstance(tenant_id, Reference) else Reference(
        assessment, tenant_id)
    categories = copy.deepcopy(ref.tree())

    def resolve(node):
        kids = node.get("children") or []
        if not kids:
            # A node with no children and no input key is a sub-item nothing
            # was collected for — an empty branch, not a parameter. Rendering
            # it as a leaf would put a nameless row on the scorecard for every
            # sub-item the company has not been asked about yet.
            return {"code": node.get("code"), "input_key": node.get("input_key"),
                    "label": node.get("label", ""),
                    "weight": node.get("weight", 0), "score": node.get("score"),
                    "applied_weight": 0.0, "children": [],
                    "is_leaf": bool(node.get("input_key"))}

        resolved = [resolve(k) for k in kids]
        score, applied = weighted_rollup(resolved)
        share = {}
        for row in applied:
            share.setdefault(row["code"], []).append(row["applied_weight"])
        # A ref code can repeat among siblings (the sector mirrors put two
        # inputs on E.1), so shares are popped in order rather than looked up
        # by code — which would hand both siblings the first one's share.
        for row in resolved:
            bucket = share.get(row["code"]) or []
            row["applied_weight"] = bucket.pop(0) if bucket else 0.0
        return {"code": node.get("code"), "input_key": None,
                "label": node.get("label", ""),
                "weight": node.get("weight", 0),
                "score": float(score) if score is not None else None,
                "applied_weight": 0.0, "children": resolved, "is_leaf": False}

    roots = [resolve(c) for c in categories]
    overall, applied = weighted_rollup(roots)
    share = {row["code"]: row["applied_weight"] for row in applied}
    for row in roots:
        row["applied_weight"] = share.get(row["code"], 0.0)
    return (float(overall) if overall is not None else None), roots


def rescore_with(assessment, tenant_id, input_key, score, include_codes=None):
    """The overall score this deal would carry if one leaf scored differently.

    `include_codes` restricts the roll-up to those category codes. It exists
    because the Fundraising payload reports a headline over A-F while this
    function's default is the whole configured model, and a "you would reach
    6.84" printed beside a headline of 6.97 is worse than no number at all.
    The roll-up itself is unchanged — the same `weighted_rollup` over the same
    tree, with a shorter list of categories going into it.

    Passing an `input_key` that matches no leaf returns the baseline on that
    basis, which is how the caller gets a comparable "before".
    """
    ref = tenant_id if isinstance(tenant_id, Reference) else Reference(
        assessment, tenant_id)
    categories = copy.deepcopy(ref.tree())
    if include_codes is not None:
        wanted = set(include_codes)
        categories = [c for c in categories if c.get("code") in wanted]

    def patch(node):
        for kid in node.get("children") or []:
            patch(kid)
        if node.get("input_key") == input_key:
            node["score"] = score

    for category in categories:
        patch(category)

    def resolve(node):
        kids = node.get("children") or []
        if not kids:
            return {"code": node.get("code"), "weight": node.get("weight", 0),
                    "score": node.get("score")}
        resolved = [resolve(k) for k in kids]
        value, _ = weighted_rollup(resolved)
        return {"code": node.get("code"), "weight": node.get("weight", 0),
                "score": float(value) if value is not None else None}

    overall, _ = weighted_rollup([resolve(c) for c in categories])
    return float(overall) if overall is not None else None


# ---------------------------------------------------------------------------
# The four blocks
# ---------------------------------------------------------------------------

def _param_config(tenant_id):
    from fundos.assessment.models import ConfigParameter
    base = {p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id__isnull=True, is_active=True)}
    base.update({p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id=tenant_id, is_active=True)})
    return base


TIER_MEANING = {
    1: "Read directly from a primary source — the company's own document or an official filing.",
    2: "Read from a reliable secondary source, such as the company's website or a named publication.",
    3: "Derived or read from the consolidated research corpus rather than from one identified document.",
    4: "Weak or indirect evidence. Treat as indicative only.",
}


def provenance(pv, cfg):
    """Where this figure came from and how it became a score.

    The drawer already showed WHAT the value was and WHICH band it met. It
    did not show where the number came from, so a reader looking at
    "Excellent" had no way to tell a figure read out of the founder's own
    financial model from one inferred off a press page — and that difference
    is the whole basis on which a scorecard is defended.

    Everything here is read from the stored ParameterValue. Nothing is
    inferred, and when a field is empty it is reported as unrecorded rather
    than filled in with a plausible sentence.
    """
    if pv is None:
        return {
            "source": "", "sourceUrl": "", "sourceKind": "",
            "tier": None, "tierMeaning": "",
            "confidence": None, "confidenceLabel": "",
            "method": "", "statement": "This parameter was not answered, so "
                                       "it is excluded from the score rather "
                                       "than counted as zero.",
        }

    detail = (pv.source_detail or "").strip()
    url = ""
    # `profile_bridge._detail` joins a URL and a document reference with an
    # em-dash, so the two arrive here in one string. Split them back out so
    # the screen can render the URL as a link.
    for part in detail.split(" — "):
        if part.startswith("http://") or part.startswith("https://"):
            url = part
            break
    label = detail or "Not recorded"

    kind = {"document": "Uploaded document / research corpus",
            "profile": "Company research",
            "benchmark": "Sector deal database",
            "founder": "Confirmed by the founder"}.get(
                (pv.source_type or "").strip(), pv.source_type or "")

    scoring = (getattr(cfg, "scoring_type", "") or "").strip()
    method = {
        "rubric": "Banded against the published cut-points for this deal "
                  "stage. The thresholds are configuration, not a judgement "
                  "made at scoring time.",
        "anchor": "Banded against the written anchor definitions. The band "
                  "shown is the one the evidence actually satisfies.",
        "lookup": "Read as a percentile from the imported sector deal table. "
                  "The company cannot move this row; only a different cohort "
                  "can.",
        "composite": "Rolled up from its component rows rather than measured "
                     "directly.",
    }.get(scoring, "")

    where = f"{kind}: {label}" if kind else label
    if pv.is_overridden:
        statement = (
            f"A reviewer set this score by hand. The rule-based result "
            f"({normalise_band(pv.system_band) or 'unscored'}) is kept beside "
            f"it. Reason given: "
            f"{pv.override_comment or 'no reason recorded'}")
    else:
        statement = f"Value taken from {where}."
        if method:
            statement = f"{statement} {method}"

    return {
        "source": label,
        "sourceUrl": url,
        "sourceKind": kind,
        "tier": pv.source_tier,
        "tierMeaning": TIER_MEANING.get(pv.source_tier, ""),
        "confidence": (float(pv.confidence)
                       if pv.confidence is not None else None),
        "confidenceLabel": confidence_label(pv),
        "method": method,
        "statement": statement,
    }


def _leaf(ref, node, pv, cfg):
    """One scored leaf: the roll-up node and the evidence behind it, joined.

    The engine keeps these apart — the roll-up has no use for citations — and
    the screen shows them as one row, so they are joined here rather than being
    left for the client to zip by ref code.
    """
    key = node.get("input_key") or ""
    band = normalise_band(pv.band if pv is not None else "")
    unit = (pv.unit if pv is not None else "") or getattr(cfg, "unit", "") or ""
    return {
        "inputKey": key,
        "ref": node.get("code") or key,
        "name": getattr(cfg, "name", "") or (pv.ref_code if pv else "") or key,
        "unit": unit,
        "isReference": bool(cfg is not None and not cfg.feeds_score),
        "scoringType": getattr(cfg, "scoring_type", ""),
        "value": (pv.raw_value if pv is not None else ""),
        "valueDisplay": (pv.raw_value if pv is not None else "") or band,
        "band": band,
        "score": float(pv.score) if pv is not None and pv.score is not None else None,
        "systemBand": normalise_band(pv.system_band if pv is not None else ""),
        "systemScore": (float(pv.system_score)
                        if pv is not None and pv.system_score is not None
                        else None),
        "isOverridden": bool(pv is not None and pv.is_overridden),
        "overrideScore": (float(pv.score)
                          if pv is not None and pv.is_overridden
                          and pv.score is not None else None),
        "overrideComment": (pv.override_comment if pv is not None else "") or "",
        "evidenceTier": evidence_tier(pv),
        "confidence": confidence_label(pv),
        "confidenceValue": (float(pv.confidence)
                            if pv is not None and pv.confidence is not None
                            else None),
        "sourceType": (pv.source_type if pv is not None else "") or "",
        "sourceDetail": (pv.source_detail if pv is not None else "") or "",
        "sourceTier": (pv.source_tier if pv is not None else None),
        "reasoning": (pv.justification if pv is not None else "") or "",
        # Where the figure came from and how it became a score, in words the
        # drawer can show directly. `sourceDetail` above is the raw string;
        # this is the same evidence expanded into something a reader can act
        # on without knowing what a source tier is.
        "provenance": provenance(pv, cfg),
        "basis": getattr(cfg, "definition", "") or "",
        "whereToFind": getattr(cfg, "where_to_find", "") or "",
        "dataGaps": data_gaps(pv),
        "missingValueAsk": missing_value_ask(pv, cfg, ref),
        "scoringCriteria": {
            "basis": getattr(cfg, "scoring_method", "")
                     or getattr(cfg, "scoring_type", ""),
            "bands": band_definitions(ref, key, unit),
        },
        "weights": {
            # The declared share and the share actually carried. They differ
            # exactly where a sibling could not be assessed, and the screen
            # shows both so the redistribution is visible rather than being an
            # invisible adjustment to the arithmetic.
            "weightWithinParent": node.get("applied_weight", 0.0),
            "declaredWeight": node.get("weight", 0),
        },
        "children": [],
    }


def _branch(ref, node):
    if node.get("is_leaf"):
        key = node.get("input_key") or ""
        return _leaf(ref, node, ref.values().get(key), ref.params.get(key))
    code = node.get("code") or ""
    out = {
        "ref": code,
        "name": node.get("label") or code,
        "weight": node.get("applied_weight", 0.0),
        "declaredWeight": node.get("weight", 0),
        "score": node.get("score"),
        "band": ref.display_band(node.get("score")),
        "children": [_branch(ref, kid) for kid in node.get("children") or []],
    }
    # Only the seven top-level categories carry one; a sub-item's name
    # already says what it covers.
    description = category_description(code)
    if description:
        out["description"] = description
    return out


def rating_gap(score, rating):
    """How far the deal is from the next rating up, and from slipping down.

    The rating ladder is a step function, so the same 0.1 of score is worth
    everything at 6.95 and nothing at 6.2 — and the scorecard shows only the
    label, which is exactly the part that hides the distance. A reader
    deciding whether to chase two more answers needs the number.

    Read from the engine's ladder rather than restated here: the cut-points
    are the same ones `rating_for_score` applied to produce the label, so the
    gap can never describe a ladder the rating did not come from.

    Returns None when nothing scored — a deal with no score has no gap.
    """
    from fundos.engines.deal_assessment import RATING_LADDER, RATING_FLOOR

    if score is None:
        return None
    s = float(score)

    # Best-first, so the first cut at or below the score is the one this
    # rating stands on, and the one before it is the next rung up.
    ladder = [(float(cut), label) for cut, label in RATING_LADDER]

    above = [(cut, label) for cut, label in ladder if cut > s]
    nxt = min(above, key=lambda r: r[0]) if above else None

    at_or_below = [(cut, label) for cut, label in ladder if cut <= s]
    current = max(at_or_below, key=lambda r: r[0]) if at_or_below else None

    return {
        "rating": rating or (current[1] if current else RATING_FLOOR),
        # The rung above, and what it costs to reach it.
        "nextRating": nxt[1] if nxt else None,
        "nextRatingAt": nxt[0] if nxt else None,
        "pointsToNext": round(nxt[0] - s, 2) if nxt else None,
        # The floor of the rung the deal is standing on. Losing this much
        # drops the label, which is the half a founder never sees coming.
        "heldAt": current[0] if current else None,
        "pointsToLose": round(s - current[0], 2) if current else None,
    }


def deal_scorecard(assessment, tenant_id=None):
    """The scorecard block: headline numbers plus the nested category tree."""
    from fundos.assessment.views import COVERAGE_FLOOR_PCT

    ref = tenant_id if isinstance(tenant_id, Reference) else Reference(
        assessment, tenant_id)
    overall, roots = scored_tree(assessment, ref)
    coverage = float(assessment.input_coverage_pct or 0)

    return {
        # The persisted figure, not the recomputed one. They agree — the test
        # suite asserts it — and quoting the stored number keeps this screen
        # reading the same scorecard every other endpoint serves.
        "system_score": (float(assessment.overall_score)
                         if assessment.overall_score is not None else None),
        "system_rating": assessment.rating_band or None,
        "coverage": f"{coverage:.0f}%",
        "coveragePct": coverage,
        "coverageFloorPct": COVERAGE_FLOOR_PCT,
        # Carried, not enforced. The scorecard endpoint suppresses a headline
        # built on thin evidence (D-09); this screen is handed the same fact so
        # it can make the same choice rather than quietly showing a number the
        # rest of the product withholds.
        "scoreSuppressed": coverage < COVERAGE_FLOOR_PCT,
        "dealStage": assessment.deal_stage,
        "stageConfirmed": bool(assessment.deal_stage
                               and not assessment.stage_was_defaulted),
        "configVersion": assessment.config_version,
        "auditStatus": assessment.audit_status,
        # Pure arithmetic over the ladder the rating already came from — no
        # extra query, and it turns a label back into a distance.
        "ratingGap": rating_gap(assessment.overall_score,
                                assessment.rating_band),
        "categories": [_branch(ref, node) for node in roots],
    }


def diligence_findings(assessment, tenant_id=None):
    """The rows a reader should not take at face value.

    Four triggers, any one of which is enough: the extractor was not confident,
    nothing evidenced the value, the sources contradict each other, or
    something needed is missing. The first two are statements about how well a
    row is known; the last two are statements the system made explicitly. All
    four belong in one list because a partner asks the founder the same kind of
    question either way, and splitting them across two screens means one goes
    unread.

    Every parameter the MODEL declares is walked, not every parameter that has
    a row — a parameter nothing answered has no `ParameterValue` at all, and it
    is precisely the one worth asking about.
    """
    ref = tenant_id if isinstance(tenant_id, Reference) else Reference(
        assessment, tenant_id)
    values = ref.values()
    contradictions = ref.contradictions()

    findings = []
    for key, cfg in ref.params.items():
        if not cfg.feeds_score:
            continue
        pv = values.get(key)
        tier = evidence_tier(pv)
        confidence = confidence_label(pv)
        gaps = data_gaps(pv)
        against = contradictions.get(key) or []

        # The four triggers, in the order the contract states them. Any one is
        # enough; see the module docstring for what each maps onto here.
        if not (confidence == "low" or tier == NOT_EVIDENCED or against
                or gaps):
            continue

        # For a row nothing answered there is no stated gap to quote, so the
        # ask is the derived one naming the document that would answer it.
        ask = (" ".join(gaps + against).strip()
               or missing_value_ask(pv, cfg, ref))
        observation = (" ".join(gaps + against).strip()
                       or f"No value was established for {cfg.name or key}.")

        finding = {
            "ref": (cfg.ref_code or key),
            "inputKey": key,
            "category": cfg.name or key,
            "categoryCode": cfg.category_code or "",
            "observation": observation,
            "ask": ask,
            "whyItMatters": "",
            "evidence": ((pv.justification if pv is not None else "")
                         or (pv.source_detail if pv is not None else "")
                         or ""),
            "evidenceTier": tier,
            "band": normalise_band(pv.band if pv is not None else ""),
            "dataGaps": gaps,
            "contradictions": against,
            "confidence": confidence or "",
            "severity": ("high" if against
                         else "medium" if tier == NOT_EVIDENCED
                         else "low"),
            "isScored": bool(pv is not None and pv.score is not None),
            "source": "parameter",
        }
        findings.append(finding)

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["ref"]))
    return findings


# Categories E and F score the MARKET, not the company: how many deals close
# in the sector, at what ticket, how many investors are active. A founder
# cannot move any of it, and neither can a diligence action.
STRUCTURAL_CATEGORIES = {"E", "F"}


def is_controllable(cfg):
    """Can the company actually move this row before the next raise?

    A recommendation list that ranks "grow the sector's TAM" beside "hire a
    CFO" is not a to-do list, and the second gets read with the same shrug as
    the first. The distinction is already in the config rather than needing a
    new flag: a `lookup` row is read as a percentile from the imported deal
    table, and categories E and F describe the market itself.
    """
    if cfg is None:
        return True
    if (getattr(cfg, "scoring_type", "") or "") == "lookup":
        return False
    return (getattr(cfg, "category_code", "") or "") not in STRUCTURAL_CATEGORIES


def band_recommendations(assessment, tenant_id=None, include_codes=None):
    """What would move each Fair or Good row up one band, and what it is worth.

    The description is the PUBLISHED rule for the band being aimed at — the
    anchor's written definition where an anchor decides the score, the rubric's
    cut-point at this stage where a number does. Inventing advice here would
    mean inventing a threshold, which is the one thing a rubric exists to
    prevent.

    The impact is a full re-roll of the tree with that leaf lifted, so the
    figure beside each row is the score the deal would actually carry rather
    than a weight-times-delta estimate that ignores redistribution.
    """
    ref = tenant_id if isinstance(tenant_id, Reference) else Reference(
        assessment, tenant_id)
    # The baseline every impact below is measured from. It MUST be the same
    # basis the caller shows as the headline: an impact of +0.07 against a
    # baseline the reader never sees is not a number they can use.
    overall = (rescore_with(assessment, ref, "", None,
                            include_codes=include_codes)
               if include_codes is not None else
               (float(assessment.overall_score)
                if assessment.overall_score is not None else None))

    out = []
    for pv in ref.values().values():
        band = normalise_band(pv.band)
        if band not in RECOMMEND_FROM or pv.score is None:
            continue
        cfg = ref.params.get(pv.input_key)
        if cfg is None or not cfg.feeds_score:
            continue
        target = next_band(band)
        if target is None:
            continue

        # What the target band is worth comes from the configured scoring key,
        # via the same `band_score` the engine bands every leaf with. Reading
        # the module constant instead would put a second copy of the scoring
        # key on the screen, which a product owner editing ConfigConstant
        # would never see move.
        target_score = ref.band_score(target)
        if target_score is None:
            continue
        after = rescore_with(assessment, ref, pv.input_key, target_score,
                             include_codes=include_codes)
        description = band_definitions(
            ref, pv.input_key, pv.unit or "").get(target, "")
        if not description and cfg.scoring_type == "lookup":
            # A percentile is read from the deal database, not from the
            # company. Naming a threshold would imply the founder can move it.
            description = ("Fixed by the sector deal database — only a "
                           "different cohort changes this row.")

        out.append({
            "ref": cfg.ref_code or pv.input_key,
            "inputKey": pv.input_key,
            "parameter": cfg.name or pv.ref_code or pv.input_key,
            "category": cfg.category_code or pv.category or "",
            "currentBand": band,
            "targetBand": target,
            "description": description,
            "currentValue": str(pv.raw_value) if (pv.raw_value is not None and str(pv.raw_value).strip() != "") else (band or ""),
            "unit": pv.unit or cfg.unit or "",
            "currentScore": float(pv.score),
            "targetScore": float(target_score),
            "overallIfReached": round(after, 2) if after is not None else None,
            "overallImpact": (round(after - overall, 3)
                              if after is not None and overall is not None
                              else None),
            # Whether this is a diligence ACTION or a fact about the market.
            # Carried rather than filtered here so both readers get the same
            # list and each decides how to present it; V2 splits on it.
            "controllable": is_controllable(cfg),
        })

    # Ordered by what it is worth, not by category: the row that moves the
    # headline most comes first, which is the order a founder should work the
    # list in. Structural rows sort below every actionable one however large
    # their impact — a big number nobody can act on belongs at the bottom.
    out.sort(key=lambda r: (not r["controllable"], -(r["overallImpact"] or 0)))
    return out


def category_extremes(categories):
    """The strongest and weakest SCORED categories, as the summary reads them.

    Mirrors the reference behaviour exactly: only categories that actually
    scored are eligible, `max` and `min` over the score, and the label carries
    the score to one decimal so "Team 8.2" is readable without a second lookup.
    A tie resolves to the first in tree order, which is what `max`/`min` do and
    what the reference does — deliberately not re-broken by any rule of ours.

    Both are empty strings when nothing scored. A deal with no evidence has no
    strongest category, and naming one would be an invention.
    """
    live = [c for c in categories if c.get("score") is not None]
    if not live:
        return "", ""
    strongest = max(live, key=lambda c: c["score"])
    weakest = min(live, key=lambda c: c["score"])
    return (f"{strongest['name']} {strongest['score']:.1f}",
            f"{weakest['name']} {weakest['score']:.1f}")


def _current_raise_mn(assessment):
    """The ask in US$ millions, from wherever it was actually entered."""
    from fundos.profile.current_raise import for_company

    company_id = getattr(getattr(assessment, "deal", None), "company_id", None)
    if not company_id:
        return None
    amount, _basis = for_company(company_id)
    return float(amount) if amount is not None else None


def _money(value, ccy="USD", unit=""):
    """Render a money figure for the summary strip, or "" when unknown.

    Blank rather than zero. An unentered raise is not a raise of nothing, and
    the summary must not turn a missing input into a claim.
    """
    if value is None:
        return ""
    try:
        number = Decimal(str(value)).normalize()
    except Exception:
        return str(value)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{ccy} {text}{unit}".strip()


def deal_terms(assessment):
    """The founder's headline terms for this raise.

    `DealTargets` is where a founder actually enters the ask (C6), so it is
    read first and the assessment's own copy is the fallback — the assessment
    records what it BANDED category D against, which is the same number only
    when the targets were set before it ran.
    """
    ask = ""
    if assessment.deal_id:
        try:
            from fundos.profile.targets import DealTargets
            row = (DealTargets.objects.filter(deal_id=assessment.deal_id,
                                              is_deleted=False)
                   .order_by("-updated_at").first())
            if row is not None and row.target_raise_value is not None:
                ask = _money(row.target_raise_value, row.target_raise_ccy)
        except Exception as exc:      # never break the scorecard over a label
            logger.debug("PHASE1: deal targets unavailable: %s", exc)
    if not ask:
        ask = _money(_current_raise_mn(assessment), "USD", "M")
    return {
        "ask_amount": ask,
        # NOT the resolver. This is the figure the assessment was banded
        # against -- category D read it when the score was produced -- so it
        # has to keep saying what was scored, not what the deal's terms say
        # today. `ask_amount` above is the live one; these are two different
        # questions and were briefly collapsed into one.
        "capital_raised": _money(assessment.capital_raised_usd_mn, "USD", "M"),
    }


def executive_summary(assessment):
    """The company in a paragraph, plus the cohort the score was produced against.

    Read from the company's profile rather than written here. The tags name the
    inputs every threshold in the model switches on — stage, sector,
    sub-sector — because a score is only meaningful beside them, and a reader
    who cannot see which population a deal was compared against cannot audit
    the number.
    """
    company = assessment.company
    profile = getattr(company, "profile", None)

    parts = []
    if profile is not None:
        sections = {s.section_key: s for s in profile.sections.filter(
            is_active=True)}
        for key, fields in NARRATIVE_SOURCES:
            section = sections.get(key)
            if section is None:
                continue
            # Object sections keep their prose in `structured`, keyed by the
            # field names in `profile.schema`; only narrative-kind sections
            # fill `content`. Reading `content` alone left this block empty on
            # every real company, because the two sections worth quoting are
            # both object sections.
            budget = sum(chars for _f, _s, chars in fields)
            longest = max(count for _f, count, _c in fields)
            text = condense((section.content or "").strip(), longest, budget)
            if not text:
                data = section.structured or {}
                text = " ".join(
                    condense(data[field], count, chars)
                    for field, count, chars in fields
                    if isinstance(data.get(field), str) and data[field].strip()
                ).strip()
            if text:
                parts.append(text)

    tags = []
    if assessment.sector:
        tags.append({"label": f"Sector: {assessment.sector}", "color": "blue"})
    if assessment.sub_sector:
        tags.append({"label": f"Sub-sector: {assessment.sub_sector}",
                     "color": "indigo"})
    if assessment.deal_stage:
        tags.append({"label": f"Stage: {assessment.deal_stage}",
                     "color": "purple"})
    # The stored column is written by nothing; the founder's figure lives in
    # the deal's headline terms. One resolver answers for every consumer.
    raise_mn = _current_raise_mn(assessment)
    if raise_mn is not None:
        tags.append({"label": f"Raise: USD {raise_mn:g}M", "color": "green"})
    # DERIVE THE RATING, DO NOT READ THE STORED COLUMN.
    #
    # `rating_band` is written when an assessment is scored and not cleared
    # when it is rescored, so a re-run left the tag showing the PREVIOUS
    # run's verdict. A live Zyla payload carried "deal_rating": "Average"
    # (computed from 6.08) beside a tag reading "Rating: Challenging" -- two
    # different answers to the same question, in one response, with no way
    # for a reader to tell which was current.
    #
    # The ladder is the single definition, so deriving it here cannot drift
    # from the summary. The stored column is the fallback for an assessment
    # that has a band but no score.
    from fundos.engines.deal_assessment import rating_for_score

    rating_label = ""
    if assessment.overall_score is not None:
        rating_label = rating_for_score(assessment.overall_score) or ""
    rating_label = rating_label or assessment.rating_band
    if rating_label:
        tags.append({"label": f"Rating: {rating_label}", "color": "amber"})

    return {
        "companyName": company.name,
        "website": getattr(profile, "website_url", "") or "",
        # ONE paragraph, not a list of them. The sources it is drawn from are
        # an implementation detail of where the prose lives; a reader wants a
        # summary, and handing them an array to reassemble makes every client
        # decide separately how to join it.
        "narrative": " ".join(parts).strip(),
        "tags": tags,
    }
