"""The evidence-quality half of the audit panel.

WHY THIS EXISTS
---------------
`run_integrity_checks` already asks whether the CONFIGURATION is sound —
weights sum to 100, the stage was chosen, nothing reached Exceptional without a
human. `review` already asks whether individual VALUES are sane — a percentage
recorded as basis points, a percentile drawn from four deals.

Neither asks the question a reader actually needs answered before quoting a
number: *how much of this score rests on something anybody can check?*

That question is not answerable from the score. An assessment can be complete,
internally consistent, weight-valid, and still be substantially the extractor's
opinion about a set of documents rather than a reading of them — and nothing on
the scorecard would say so. The checks here measure that gap and put a figure
on it.

WHAT EACH ONE CATCHES
---------------------
  * A headline with nothing behind it (:func:`_overall_produced`).
  * A cohort that did not resolve, so the six database-fed rows scored blank
    and their weight moved silently onto their siblings
    (:func:`_cohort_resolves`, :func:`_cohort_rows_populated`).
  * Evidence that was never tiered, never cited, or derived without showing
    the working (:func:`_untiered_values`, :func:`_uncited_values`,
    :func:`_derived_without_workings`).
  * The share of the score standing on Verified evidence, and the number of
    rows excluded from it altogether (:func:`_verified_share`,
    :func:`_excluded_rows`).
  * A sub-sector TAM larger than the sector containing it — arithmetically
    impossible, and both figures feed a Market Size row (:func:`_tam_sanity`).
  * A judgement scored above Fair with nothing cited behind it, which is the
    specific way a moat assessment inflates (:func:`_unevidenced_generosity`).
  * Real weight resting on a value the extractor itself doubted
    (:func:`_fragile_weight`), and the total weight resting on confidence
    rather than on provenance (:func:`_confidence_carried_weight`).
  * The size of the ask against what this cohort actually raises
    (:func:`_ask_against_cohort`) — context for reading the rating, which the
    scorecard alone never supplies.

SEVERITY, AND WHY IT IS NOT A STRAIGHT COPY
-------------------------------------------
The source model has three states: OK, REVIEW, FAIL. This system has three
too — info, warning, error — but they are NOT the same three, because here
`error` has a consequence: `run_scoring` sets ``audit_status = "failed"`` on
any error finding, and a failed audit is a claim that the score should not be
quoted at all.

So the mapping is by consequence rather than by name:

  error    Something that cannot be true of a sound assessment: no score at
           all, a value scored with no evidence tier, a niche larger than its
           own market. Each is a defect rather than a shortfall.

  warning  A real shortfall in the evidence that a person has to look at and
           consciously accept. Most of the source model's FAILs land here,
           because "the pack was thin" is not the same claim as "the
           arithmetic is wrong", and conflating them would make a failed audit
           mean nothing.

  info     Context with no action attached — counts, and the ask comparison
           when it lands inside the cohort's normal range.

A check reports only when it has something to say. A clean assessment adds no
findings here, which is what keeps the audit panel worth reading.
"""
import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# Share of scored rows that should rest on Verified evidence, and how many
# rows may drop out of scoring before the remainder stops representing the
# model. Both are the source model's own figures, and both are read through
# `constant()` so a product owner can move them without a deploy.
MIN_VERIFIED_SHARE = 0.50
MAX_EXCLUDED_ROWS = 15

# A doubted value carrying more than this share of the final score is where a
# second read pays for itself.
FRAGILE_WEIGHT_FLOOR = 0.03

# Above this share of the score standing on extractor confidence rather than
# on a cited source, the assessment is substantially an opinion about the
# documents rather than a reading of them.
CONFIDENCE_CARRY_CEILING = 0.20

# An ask this far from the cohort's average ticket is the first thing an
# investor asks about.
ASK_RATIO_LOW = 0.4
ASK_RATIO_HIGH = 2.5


def checks(assessment, result=None, tenant_id=None):
    """Every evidence-quality finding for one assessment. Never raises.

    A broken check must not cost the others their output, so each runs inside
    its own guard — the same contract `review` keeps, and for the same reason:
    these are diagnostics, and a diagnostic that can take down scoring is
    worse than no diagnostic.
    """
    tenant_id = tenant_id or getattr(assessment, "tenant_id", None)

    # Guarded like the checks themselves, and for the stronger reason: this
    # runs inside `run_scoring`, so an exception escaping here would turn a
    # diagnostic block into the thing that stops a company being scored at
    # all. Losing the audit panel is bad; losing the score is worse.
    try:
        ctx = _Context(assessment, result, tenant_id)
    except Exception as e:
        logger.warning("INTEGRITY %s: could not read the assessment: %s",
                       assessment.id, e, exc_info=True)
        return []

    findings = []
    for check in (_overall_produced, _cohort_resolves, _cohort_rows_populated,
                  _untiered_values, _uncited_values, _derived_without_workings,
                  _verified_share, _excluded_rows, _tam_sanity,
                  _unevidenced_generosity, _fragile_weight,
                  _confidence_carried_weight, _ask_against_cohort,
                  _perfect_scores_are_human, _complete_pack_gaps):
        try:
            findings.extend(check(ctx) or [])
        except Exception as e:
            logger.warning("INTEGRITY %s: %s failed: %s", assessment.id,
                           check.__name__, e, exc_info=True)
    return findings


# ---------------------------------------------------------------------------
# Shared reading
# ---------------------------------------------------------------------------

class _Context:
    """Everything the checks read, read once.

    Thirteen checks over eighty parameters is thirteen chances to walk the
    same three tables again. They are assembled here instead, because this
    runs inside `run_scoring` — on the write path, under a transaction, on
    every re-score — and a diagnostic block has no business being the
    expensive part of scoring.
    """

    def __init__(self, assessment, result, tenant_id):
        from fundos.assessment.models import ConfigParameter

        self.assessment = assessment
        self.result = result or {}
        self.tenant_id = tenant_id

        self.values = {pv.input_key: pv
                       for pv in assessment.parameter_values.all()}

        params = {p.input_key: p for p in ConfigParameter.objects.filter(
            tenant_id__isnull=True, is_active=True)}
        params.update({p.input_key: p for p in ConfigParameter.objects.filter(
            tenant_id=tenant_id, is_active=True)})
        self.params = params

        self._weights = None
        self._cohorts = None

    # -- lazily derived ----------------------------------------------------

    def effective_weights(self):
        """What each leaf is actually worth to the final score, 0-1.

        Not the declared weight. A parameter nominally worth 4% of its
        sub-item can carry three times that once its blank siblings drop out
        of the roll-up, and it is the post-redistribution figure that decides
        whether a doubted value matters. Computed by walking the same tree
        `run_scoring` scores, multiplying each node's applied share down the
        branch.
        """
        if self._weights is None:
            from fundos.assessment.services import _build_tree
            self._weights = {}
            _accumulate(_build_tree(self.assessment, self.tenant_id),
                        1.0, self._weights)
        return self._weights

    def cohorts(self):
        """The resolved sector and sub-sector rows, or None where unresolved.

        Returns (sector_row, sub_sector_row, benchmarks_loaded). The third
        value matters: with no deal database imported at all, an unresolved
        cohort is a deployment state rather than a finding about this
        assessment, and reporting it would put the same warning on every
        company forever.
        """
        if self._cohorts is None:
            from fundos.assessment.models import SectorDealData
            loaded = SectorDealData.objects.exists()
            sector = sub = None
            if loaded:
                sector = SectorDealData.objects.filter(
                    level="sector",
                    name__iexact=(self.assessment.sector or "")).first()
                sub = _resolve_sub(self.assessment.sub_sector)
            self._cohorts = (sector, sub, loaded)
        return self._cohorts

    # -- projections over the values --------------------------------------

    def scored(self):
        """Rows that carried a score into the roll-up."""
        return [pv for pv in self.values.values() if pv.score is not None]

    def tier_of(self, pv):
        from fundos.assessment.phase1 import evidence_tier
        return evidence_tier(pv)

    def is_cited(self, pv):
        """Whether this value says where it was read.

        A tier alone is not a citation — it says how good the source was
        supposed to be, not which source it was. `source_detail` is the field
        extraction writes the document and location into, so a blank one on a
        value that scored means the provenance chain stops here.
        """
        return bool((pv.source_detail or "").strip())


def _accumulate(nodes, share, out):
    """Push each node's applied share down its branch onto the leaves.

    Blank siblings are excluded from the denominator exactly as
    `weighted_rollup` excludes them, so the shares this produces sum to 1
    across the scored leaves and match what the engine actually did.
    """
    live = [n for n in nodes if _node_scored(n)]
    total = sum(float(n.get("weight") or 0) for n in live)
    if total <= 0:
        return
    for node in live:
        part = share * float(node.get("weight") or 0) / total
        kids = node.get("children")
        if kids:
            _accumulate(kids, part, out)
        elif node.get("input_key"):
            out[node["input_key"]] = out.get(node["input_key"], 0.0) + part


def _node_scored(node):
    kids = node.get("children")
    if kids:
        return any(_node_scored(k) for k in kids)
    return node.get("score") is not None


def _resolve_sub(label):
    from fundos.assessment.models import SectorDealData
    from fundos.profile.assessment_extraction import resolve_sub_sector

    group, _method = resolve_sub_sector(label)
    if not group:
        return None
    return (SectorDealData.objects.filter(
        level="sub_sector", clubbed_group__iexact=group).first()
        or SectorDealData.objects.filter(
            level="sub_sector", name__iexact=group).first())


def _num(raw):
    try:
        return Decimal(str(raw).replace(",", "").replace("%", "").strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None


def _tuning(key, default, tenant_id):
    from fundos.assessment.services import constant
    value = constant(key, None, tenant_id)
    return float(value) if value is not None else default


#: The highest score an automated path may produce. The bands run
#: Poor 3, Fair 5, Good 7, Excellent 9, and Exceptional 10 is override-only
#: (2.4.4) -- so anything above 9 without an override record means the guard
#: in `validate_automated_band` did not run.
AUTOMATED_CEILING = 9.0


def _finding(severity, code, message, **extra):
    out = {"source": "integrity", "severity": severity, "check": code,
           "message": message}
    out.update(extra)
    return out


def _names(ctx, keys, limit=6):
    """Parameter refs and names, for a message a reader can act on."""
    out = []
    for key in list(keys)[:limit]:
        cfg = ctx.params.get(key)
        ref = (cfg.ref_code if cfg else "") or key
        name = (cfg.name if cfg else "") or key
        out.append(f"{ref} {name}")
    return ", ".join(out)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def _overall_produced(ctx):
    """Nothing scored at all.

    Distinct from thin coverage: coverage says the pack was incomplete, this
    says the roll-up produced no number, which means either extraction never
    ran or no parameter survived to the top of the tree.
    """
    if ctx.assessment.overall_score is not None:
        return []
    return [_finding(
        "error", "no_overall_score",
        "The roll-up produced no overall score. Either nothing was extracted "
        "or every scored row was excluded, so there is no rating to quote.")]


def _cohort_resolves(ctx):
    """The sector and sub-sector must find a row in the deal database.

    Without them the six database-fed rows in categories E and F score blank
    and their weight redistributes onto whatever else is answered — the score
    still comes out, and nothing on it says a third of the model went missing.
    """
    sector, sub, loaded = ctx.cohorts()
    if not loaded:
        return []
    out = []
    if sector is None:
        out.append(_finding(
            "warning", "sector_unresolved",
            f"Sector {ctx.assessment.sector or '(blank)'!s} does not match a "
            f"row in the deal database, so the sector percentile rows cannot "
            f"score and their weight has moved to their siblings."))
    if sub is None:
        out.append(_finding(
            "warning", "sub_sector_unresolved",
            f"Sub-sector {ctx.assessment.sub_sector or '(blank)'!s} does not "
            f"match a clubbed group, so the sub-sector percentile rows cannot "
            f"score. Category F carries the heaviest weight in the model."))
    if sector is not None and sub is not None and _same_cohort(sector, sub):
        out.append(_finding(
            "info", "cohort_overlap",
            f"The sector and sub-sector both resolve to {sector.name}, so "
            f"categories E and F are reading the same rows of the deal "
            f"database. A narrower sub-sector would make them independent."))
    return out


def _same_cohort(sector, sub):
    return (sector.name or "").strip().lower() == (
        (sub.clubbed_group or sub.name or "").strip().lower())


def _cohort_rows_populated(ctx):
    """Database-fed rows that did not populate.

    A cohort below the minimum sample bands blank BY DESIGN, so this reports
    rather than fails — but a reader comparing two deals needs to know that
    one of them scored without its percentile rows.
    """
    lookups = [k for k, p in ctx.params.items()
               if p.scoring_type == "lookup" and p.feeds_score]
    if not lookups:
        return []
    blank = [k for k in lookups
             if ctx.values.get(k) is None or ctx.values[k].score is None]
    if not blank:
        return []
    return [_finding(
        "warning", "cohort_rows_blank",
        f"{len(blank)} of {len(lookups)} deal-database rows did not populate: "
        f"{_names(ctx, sorted(blank))}. A cohort below the minimum sample "
        f"blanks by design; the effect either way is that their weight moved "
        f"to the rows that did score.")]


def _untiered_values(ctx):
    """A value that scored without saying how good its source was.

    This should not be reachable — extraction assigns a tier to everything it
    reports — so a hit here is a defect in the write path rather than a
    shortfall in the pack, and it is an error for that reason: the row is
    scoring, and nothing downstream can weigh it.
    """
    bad = sorted(pv.input_key for pv in ctx.scored()
                 if pv.source_tier is None and not (pv.source_type or "").strip())
    if not bad:
        return []
    return [_finding(
        "error", "untiered_value",
        f"{len(bad)} value(s) scored with no evidence tier and no source "
        f"type: {_names(ctx, bad)}. These are being counted at full weight "
        f"with nothing recording where they came from.")]


def _uncited_values(ctx):
    """A value that scored without naming the document it came from.

    The tier says the source was, say, Management quality. It does not say
    which page of which model. Without that, the figure cannot be checked by
    anyone who did not run the extraction, and "trust the pipeline" is not a
    defence a scorecard can make to an investor.
    """
    bad = sorted(pv.input_key for pv in ctx.scored()
                 if not ctx.is_cited(pv) and not pv.is_overridden)
    if not bad:
        return []
    return [_finding(
        "warning", "uncited_value",
        f"{len(bad)} scored value(s) name no source document: "
        f"{_names(ctx, bad)}. Each is carrying weight that cannot be traced "
        f"back to anything.")]


def _derived_without_workings(ctx):
    """A figure that was built rather than read, with no working shown.

    An Estimate is legitimate — runway derived from cash and burn is the
    normal case. It is only legitimate while the derivation is written down,
    because otherwise the number cannot be reproduced and a change in it
    cannot be explained.
    """
    bad = sorted(pv.input_key for pv in ctx.scored()
                 if ctx.tier_of(pv) == "Estimate"
                 and not (pv.justification or "").strip())
    if not bad:
        return []
    return [_finding(
        "warning", "derived_without_workings",
        f"{len(bad)} derived figure(s) show no working: {_names(ctx, bad)}. "
        f"A figure that was computed rather than read has to be reproducible "
        f"from what is recorded beside it.")]


def _verified_share(ctx):
    """How much of the scored model rests on first-tier evidence."""
    scored = ctx.scored()
    if not scored:
        return []
    floor = _tuning("min_verified_share", MIN_VERIFIED_SHARE, ctx.tenant_id)
    verified = [pv for pv in scored if ctx.tier_of(pv) == "Verified"]
    share = len(verified) / len(scored)
    if share >= floor:
        return []
    return [_finding(
        "warning", "thin_verified_share",
        f"{share:.0%} of scored rows rest on Verified evidence, below the "
        f"{floor:.0%} this methodology asks for. The rating is carried mostly "
        f"by management-supplied and derived figures.")]


def _excluded_rows(ctx):
    """Rows the model expects that dropped out of scoring entirely.

    "Blank is not zero" stops an absence being counted against the company.
    This is its counterweight: past a point, enough absences mean the score
    describes the answered fraction rather than the company.
    """
    expected = [k for k, p in ctx.params.items() if p.feeds_score]
    excluded = [k for k in expected
                if ctx.values.get(k) is None or ctx.values[k].score is None]
    limit = int(_tuning("max_excluded_rows", MAX_EXCLUDED_ROWS, ctx.tenant_id))
    if len(excluded) <= limit:
        return []
    return [_finding(
        "warning", "excluded_rows",
        f"{len(excluded)} of {len(expected)} scored rows were excluded, above "
        f"the {limit} this methodology tolerates. The weight of every one of "
        f"them has redistributed onto the rows that did score.")]


def _tam_sanity(ctx):
    """A sub-sector cannot be larger than the sector containing it.

    Both figures feed a Market Size row, so one of them being wrong moves
    categories E and F. The inversion is arithmetically impossible rather than
    merely unlikely, which is what makes it an error.
    """
    sector = _num((ctx.values.get("SEC_TAM") or _Blank()).raw_value)
    sub = _num((ctx.values.get("SEC_TAM_SUB") or _Blank()).raw_value)
    if sector is None or sub is None:
        return []
    if sub > sector:
        return [_finding(
            "error", "tam_inverted",
            f"The sub-sector TAM ({sub:g}) exceeds the sector TAM "
            f"({sector:g}). A niche cannot be larger than the market "
            f"containing it, so one of the two figures is wrong and both feed "
            f"a Market Size row.",
            parameters=["SEC_TAM", "SEC_TAM_SUB"])]
    if sub == sector:
        return [_finding(
            "warning", "tam_identical",
            f"The sector and sub-sector TAM are both {sub:g}. That usually "
            f"means one figure was reused for the other, or the sub-sector "
            f"has been drawn as wide as the sector.",
            parameters=["SEC_TAM", "SEC_TAM_SUB"])]
    return []


class _Blank:
    raw_value = ""


def _unevidenced_generosity(ctx):
    """A judgement scored above Fair with nothing cited behind it.

    Anchor rows have no number to contradict them. That is what makes them the
    easiest place for an assessment to drift generous without anything looking
    wrong — and the moat sub-items, where the honest answer for most companies
    is Poor, are where it happens.
    """
    bad = sorted(
        pv.input_key for pv in ctx.scored()
        if (ctx.params.get(pv.input_key) is not None
            and ctx.params[pv.input_key].scoring_type == "anchor"
            and pv.band in ("Good", "Excellent")
            and not pv.is_overridden
            and not (pv.justification or "").strip()
            and not ctx.is_cited(pv)))
    if not bad:
        return []
    return [_finding(
        "warning", "unevidenced_judgement",
        f"{len(bad)} judgement row(s) scored above Fair with nothing cited "
        f"and no reason written: {_names(ctx, bad)}. An anchor scored above "
        f"Fair has to say what it was judged on.")]


def _fragile_weight(ctx):
    """Real weight resting on a value the extractor itself doubted.

    Effective weight is the post-redistribution figure, so this is what the
    row is genuinely worth to the headline rather than what it was nominally
    allocated. A doubted value above the floor is where re-reading one
    document changes the rating.
    """
    from fundos.assessment.extraction import CONFIDENCE_FLOOR

    weights = ctx.effective_weights()
    floor = _tuning("fragile_weight_floor", FRAGILE_WEIGHT_FLOOR, ctx.tenant_id)
    fragile = [(weights.get(pv.input_key, 0.0), pv.input_key)
               for pv in ctx.scored()
               if pv.confidence is not None
               and float(pv.confidence) < CONFIDENCE_FLOOR
               and weights.get(pv.input_key, 0.0) >= floor]
    if not fragile:
        return []
    fragile.sort(reverse=True)
    detail = " · ".join(
        f"{_names(ctx, [k], limit=1)} carries {w:.1%}" for w, k in fragile[:6])
    return [_finding(
        "warning", "fragile_weight",
        f"{len(fragile)} low-confidence value(s) each carry at least "
        f"{floor:.0%} of the score: {detail}. Re-reading these is the cheapest "
        f"way to firm up the rating.")]


def _confidence_carried_weight(ctx):
    """How much of the score stands on confidence rather than on provenance.

    Rows that are derived or unevidenced still score when the extractor was
    sure of the value anyway. That recovers figures a stricter rule would
    throw away, and it is the intended behaviour — but it moves part of the
    score off documented provenance and onto the model's own certainty, and
    the size of that part is exactly what a reader cannot infer from the
    headline.
    """
    weights = ctx.effective_weights()
    ceiling = _tuning("confidence_carry_ceiling", CONFIDENCE_CARRY_CEILING,
                      ctx.tenant_id)
    rested = [(weights.get(pv.input_key, 0.0), pv.input_key)
              for pv in ctx.scored()
              if ctx.tier_of(pv) in ("Estimate", "Not Evidenced")
              and not pv.is_overridden]
    carried = sum(w for w, _ in rested)
    if not rested or carried < ceiling:
        return []
    rested.sort(reverse=True)
    detail = " · ".join(
        f"{_names(ctx, [k], limit=1)} {w:.1%}" for w, k in rested[:5])
    return [_finding(
        "warning", "confidence_carried_weight",
        f"{carried:.0%} of the score rests on rows that scored on extractor "
        f"confidence rather than on a cited source, above the {ceiling:.0%} "
        f"that is ordinary: {detail}. To that extent the rating is an opinion "
        f"about the documents rather than a reading of them.")]


def _ask_against_cohort(ctx):
    """The size of the ask, against what this cohort actually raises.

    Not a scored parameter — context for reading the score. A Very Good
    company asking twice the cohort's average ticket is a harder conversation
    than a Very Good company asking the median, and the scorecard alone will
    never say so.
    """
    from fundos.profile.current_raise import for_company

    company_id = getattr(getattr(ctx.assessment, "deal", None),
                         "company_id", None)
    ask, _basis = for_company(company_id) if company_id else (None, "")
    if ask is None or float(ask) <= 0:
        return []
    sector, sub, loaded = ctx.cohorts()
    if not loaded:
        return []
    row = sub if (sub and sub.avg_ticket_usd_mn) else sector
    if row is None or not row.avg_ticket_usd_mn:
        return []
    average = float(row.avg_ticket_usd_mn)
    if average <= 0:
        return []
    ratio = float(ask) / average
    low = _tuning("ask_ratio_low", ASK_RATIO_LOW, ctx.tenant_id)
    high = _tuning("ask_ratio_high", ASK_RATIO_HIGH, ctx.tenant_id)
    if low <= ratio <= high:
        return [_finding(
            "info", "ask_in_context",
            f"The ask of US$ {float(ask):g}Mn is {ratio:.1f}x the average "
            f"ticket in {row.name} (US$ {average:g}Mn) — inside the range "
            f"this cohort normally sees.")]
    direction = "above" if ratio > 1 else "below"
    return [_finding(
        "warning", "ask_outside_cohort",
        f"The ask of US$ {float(ask):g}Mn is {ratio:.1f}x the average ticket "
        f"in {row.name} (US$ {average:g}Mn) — well {direction} what this "
        f"cohort typically sees. That does not make it wrong, but it is the "
        f"first thing an investor will ask about and the scorecard does not "
        f"capture it.")]


def _perfect_scores_are_human(ctx):
    """Nothing may score a perfect 10 unless a person put it there.

    The scale runs to 10 and the automated bands stop at 9. That gap is
    deliberate: a machine reading a document can establish that a company
    clears the Excellent cut-point, but it cannot establish that a company is
    better than the rubric was built to describe. That is a judgement about
    the rubric, and only someone accountable for it may make it -- which is
    why Exceptional is override-only and `validate_automated_band` downgrades
    it wherever an automated path returns it.

    So a 10 with no override is not a finding about the company, it is a bug
    in the guard, and it is checked for precisely because a silent one would
    be indistinguishable from a very good company. A reviewer-awarded 10 is
    reported at info, so a reader knows one is there and can go and read the
    reason given for it.
    """
    machine = [pv for pv in ctx.values.values()
               if pv.score is not None and float(pv.score) > AUTOMATED_CEILING
               and not pv.is_overridden]
    if machine:
        return [_finding(
            "error", "machine_perfect_score",
            f"{len(machine)} row(s) scored above {AUTOMATED_CEILING:g} with no "
            f"reviewer override. Nothing scored automatically may reach a "
            f"perfect 10 — that band is a human verdict on the rubric itself, "
            f"and a row holding one without an override record means the guard "
            f"did not run: {_names(ctx, [pv.input_key for pv in machine])}",
            parameters=[pv.input_key for pv in machine])]

    awarded = [pv for pv in ctx.values.values()
               if pv.score is not None and float(pv.score) > AUTOMATED_CEILING
               and pv.is_overridden]
    if not awarded:
        return []
    uncommented = [pv for pv in awarded if not (pv.override_comment or "").strip()]
    if uncommented:
        return [_finding(
            "warning", "perfect_score_uncommented",
            f"{len(uncommented)} row(s) were awarded a perfect score with no "
            f"reason recorded. The band exists so a reviewer can say the "
            f"rubric understates this company; without the sentence saying "
            f"why, the score cannot be defended: "
            f"{_names(ctx, [pv.input_key for pv in uncommented])}",
            parameters=[pv.input_key for pv in uncommented])]
    return [_finding(
        "info", "perfect_score_awarded",
        f"{len(awarded)} row(s) carry a reviewer-awarded perfect score, above "
        f"anything the published bands can reach. The reason is on each row: "
        f"{_names(ctx, [pv.input_key for pv in awarded])}",
        parameters=[pv.input_key for pv in awarded])]


def _ref_of(ctx, key):
    """A parameter's ref code, for a stable ordering. Falls back to the key."""
    cfg = ctx.params.get(key)
    return ((cfg.ref_code if cfg else "") or key)


def _complete_pack_gaps(ctx):
    """What the company did not supply, named so it can be asked for.

    The counterweight to "blank is not zero". That rule stops an absence being
    counted as a failure, and it is right -- but on its own it lets an absence
    go unnoticed, and a company benefit from not having produced the number.

    `_excluded_rows` already reports HOW MANY rows dropped out. This reports
    WHICH, with the workbook's own `if_missing` line beside each, because the
    useful next step is going back to the founder for six named line items
    rather than re-running the extraction and hoping.

    Reference rows are left out: they do not score by design, so listing them
    as gaps would bury the rows that actually cost the assessment something.
    """
    gaps = []
    for key, cfg in ctx.params.items():
        if not cfg.feeds_score:
            continue
        pv = ctx.values.get(key)
        if pv is not None and pv.score is not None:
            continue
        gaps.append(key)
    if not gaps:
        return []

    expected = [k for k, p in ctx.params.items() if p.feeds_score]
    share = len(gaps) / float(len(expected) or 1)

    # Weighted by what the gaps were WORTH, not just how many there were. Six
    # blank rows carrying 2% between them is a different assessment from two
    # carrying 30%, and the count alone cannot tell them apart.
    weights = ctx.effective_weights()
    lost = sum(weights.get(k, 0.0) for k in gaps)

    # Heaviest first, then by ref code. The tie-break is not cosmetic: on an
    # assessment where nothing scored, EVERY gap weighs the same, and without
    # it the ten rows named here are whichever ten the database happened to
    # return -- so the same assessment reported a different list run to run.
    asks = []
    for key in sorted(gaps, key=lambda k: (-weights.get(k, 0.0),
                                           _ref_of(ctx, k)))[:10]:
        cfg = ctx.params.get(key)
        ref = (cfg.ref_code if cfg else "") or key
        name = (cfg.name if cfg else "") or key
        # The workbook's own line about what to do when the row is missing,
        # where the dictionary has one. It is what makes the ask specific --
        # a named document rather than "please send more information".
        ask = (cfg.if_missing if cfg else "") or ""
        asks.append(f"{ref} {name}" + (f" ({ask.strip()})" if ask else ""))

    severity = "warning" if share > 0.25 or lost > 0.20 else "info"
    param_list = ", ".join(asks)
    more = f" and {len(gaps) - 10} more" if len(gaps) > 10 else ""
    return [_finding(
        severity, "complete_pack_gaps",
        f"{len(gaps)} of {len(expected)} scored rows were never evidenced, "
        f"carrying {lost * 100:.0f}% of the rating between them. "
        f"Missing: {param_list}{more}.",
        parameters=gaps[:20])]
