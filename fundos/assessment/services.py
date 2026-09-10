"""Deal Assessment services — config-driven scoring run.

Reads the versioned config (never hard-coded thresholds), bands each
parameter, rolls up the hierarchy and writes the result. Domain-agnostic:
nothing here knows what sector the company is in.
"""
import logging
from decimal import Decimal

from fundos.engines.deal_assessment import (
    BAND_SCORES, DEFAULT_STAGE, RATING_MEANING, band_monotonic, band_range,
    input_coverage, rating_for_score, validate_automated_band,
    weighted_rollup,
)

logger = logging.getLogger(__name__)


def _cfg(model, tenant_id, **filters):
    """Resolve a config row: tenant override first, else platform default.

    A tenant row is a deliberate fork off the platform scoring model, so it
    wins where present — but the NULL-tenant row is what almost every tenant
    uses, and a new tenant needs no seeding at all.
    """
    row = model.objects.filter(tenant_id=tenant_id, is_active=True,
                               **filters).order_by("-version").first()
    if row is not None:
        return row
    return model.objects.filter(tenant_id__isnull=True, is_active=True,
                                **filters).order_by("-version").first()


def resolve_stage(assessment):
    """§2.5 — stage must be set before anything is banded.

    Defaulting is allowed but must be LOUD: the same 14-month runway is Fair
    at Series A and Good at Growth, so a silently wrong stage re-bands every
    numeric parameter in the assessment.
    """
    if assessment.deal_stage:
        return assessment.deal_stage, False
    from fundos.assessment.tasks import resolve_initial_stage
    resolved = resolve_initial_stage(getattr(assessment, "deal", None))
    if resolved:
        assessment.deal_stage = resolved
        assessment.save(update_fields=["deal_stage"])
        return assessment.deal_stage, False
    logger.warning(
        "ASSESSMENT %s: deal stage was not set. Defaulting to %s — every "
        "numeric parameter is being banded against %s cut-points, which may "
        "be wrong. Set the stage and re-run.",
        assessment.id, DEFAULT_STAGE, DEFAULT_STAGE)
    return DEFAULT_STAGE, True


def constant(key, default=None, tenant_id=None):
    """A scalar from the workbook's own Deal Scorecard panel.

    Read from config rather than hardcoded because the sheet labels these
    "editable" and the product owner is expected to move them — the percentile
    cut-offs and the minimum sample size are tuning dials, not physics.
    """
    from fundos.assessment.models import ConfigConstant

    row = _cfg(ConfigConstant, tenant_id, key=key)
    if row is None:
        return default
    return row.value


def band_for_percentile(score, tenant_id=None):
    """Band a 0-10 deal-database percentile.

    THE SCORE IS NOT THE PERCENTILE. v23 stored the percentile itself as the
    score, so Fintech's ticket percentile of 10 scored 10 and Ecommerce's 2.5
    scored 2.5. The workbook bands it first — at the thresholds on Deal
    Scorecard L38:L40 — and then scores it 9/7/5/3 like every other row, so
    those two should score 9 and 3. Scoring the raw percentile also let a row
    reach 10, which is reserved for a human override.
    """
    if score is None:
        return None
    s = Decimal(str(score))
    if s >= Decimal(str(constant("percentile_excellent", 8, tenant_id))):
        return "Excellent"
    if s >= Decimal(str(constant("percentile_good", 6, tenant_id))):
        return "Good"
    if s >= Decimal(str(constant("percentile_fair", 4, tenant_id))):
        return "Fair"
    return "Poor"


def band_score(band, tenant_id=None):
    """The score a band is worth, from the workbook's scoring key."""
    if not band:
        return None
    key = f"score_{band.lower().replace(' ', '_')}"
    value = constant(key, None, tenant_id)
    if value is not None:
        return float(value)
    return BAND_SCORES.get(band)


def _attribution_is_evidenced(pv):
    """Does this row's evidence make the claim the row is scoring?

    Only the parameters listed in `decision.ATTRIBUTION_CLAIMS` are checked;
    every other row is evidenced by definition here and returns True. The
    table lives in `decision` because that module also has to explain the gap
    to a reader, and one table cannot drift from itself.
    """
    from fundos.assessment.decision import ATTRIBUTION_CLAIMS

    check = ATTRIBUTION_CLAIMS.get(pv.input_key)
    if check is None or not check.demotes:
        # Rows that only raise the question still score; `decision` reports
        # the gap without removing them from the roll-up.
        return True
    if pv.is_overridden:
        # A human has looked at it and taken responsibility for the number.
        return True
    return check.is_evidenced_by(
        " ".join(filter(None, [pv.justification or "", pv.source_detail or ""])))


def band_parameter(pv, stage, tenant_id=None):
    """Band one parameter value from config. Returns (band, score).

    Anchor parameters are not banded here — they need an LLM call against
    the written definitions, which happens upstream and arrives already
    banded. This function guards that result rather than trusting it.
    """
    from fundos.assessment.models import ConfigParameter, ConfigRubric

    param = _cfg(ConfigParameter, tenant_id, input_key=pv.input_key)
    if param is None:
        logger.warning("ASSESSMENT: no config for parameter %s — skipping.",
                       pv.input_key)
        return None, None

    # Some rows measure a LINK rather than a count, and evidence for the
    # count does not evidence the link: thirteen named logos prove thirteen
    # clients, not that any of them came through the founder's network, which
    # is what A.1.b actually scores. Scoring the related fact is how a row
    # ends up carrying weight no source supports, so the row is held unscored
    # until the evidence states the link. Unscored — not Poor: an unproven
    # claim is an absent fact, and its weight redistributes to the rows that
    # were evidenced (§2.3.1) rather than counting against the company.
    if not _attribution_is_evidenced(pv):
        logger.info(
            "ASSESSMENT: %s held Not Evidenced — its evidence does not state "
            "the relationship the row scores.", pv.input_key)
        return None, None

    if param.scoring_type == "anchor":
        band = validate_automated_band(pv.band, input_key=pv.input_key)
        return band, band_score(band, tenant_id) if band else None

    if pv.raw_value in (None, ""):
        return None, None
    try:
        value = Decimal(str(pv.raw_value).replace(",", "").replace("%", ""))
    except Exception:
        logger.warning("ASSESSMENT: %s has non-numeric value %r — unscored.",
                       pv.input_key, pv.raw_value)
        return None, None

    # §2.7 — deal-database percentiles. Banded against the percentile
    # thresholds, then scored from the same band key as everything else.
    if param.scoring_type == "lookup":
        band = band_for_percentile(value, tenant_id)
        return band, band_score(band, tenant_id)

    rubric = _cfg(ConfigRubric, tenant_id, input_key=pv.input_key,
                  stage=stage)
    if rubric is None:
        logger.warning(
            "ASSESSMENT: no rubric for %s at stage %s. This parameter scores "
            "blank for every company at this stage — seed the missing "
            "cut-points.", pv.input_key, stage)
        return None, None

    if rubric.direction == "Range":
        band = band_range(value, rubric.ideal_min, rubric.ideal_max,
                          rubric.good_tolerance_pct,
                          rubric.fair_tolerance_pct)
    else:
        band = band_monotonic(value, rubric.direction, rubric.cut_excellent,
                              rubric.cut_good, rubric.cut_fair)
    band = validate_automated_band(band, input_key=pv.input_key)

    # E.1 / F.1 are scored on the WORSE of TAM size and TAM growth: the
    # workbook's own note says a large but stagnant market is not attractive,
    # and averaging the two would let size mask stagnation.
    if param.companion_key:
        band = _worse_band(band, _companion_band(pv, param, stage, tenant_id))
    return band, band_score(band, tenant_id) if band else None


_BAND_ORDER = ["Poor", "Fair", "Good", "Excellent", "Exceptional"]


def _worse_band(a, b):
    known = [x for x in (a, b) if x in _BAND_ORDER]
    if not known:
        return a or b
    return min(known, key=_BAND_ORDER.index)


def _companion_band(pv, param, stage, tenant_id):
    """Band the companion input on its own rubric, without scoring it."""
    from fundos.assessment.models import ConfigRubric

    companion = pv.assessment.parameter_values.filter(
        input_key=param.companion_key).first()
    if companion is None or companion.raw_value in (None, ""):
        return None
    try:
        value = Decimal(str(companion.raw_value).replace(",", "")
                        .replace("%", ""))
    except Exception:
        return None
    rubric = _cfg(ConfigRubric, tenant_id, input_key=param.companion_key,
                  stage=stage)
    if rubric is None:
        return None
    if rubric.direction == "Range":
        return band_range(value, rubric.ideal_min, rubric.ideal_max,
                          rubric.good_tolerance_pct, rubric.fair_tolerance_pct)
    return band_monotonic(value, rubric.direction, rubric.cut_excellent,
                          rubric.cut_good, rubric.cut_fair)


def _build_tree(assessment, tenant_id):
    """Assemble the category -> sub-item -> child tree from config + values."""
    from fundos.assessment.models import ConfigParameter, ConfigWeight

    values = {pv.input_key: pv for pv in assessment.parameter_values.all()}
    params = list(ConfigParameter.objects.filter(
        tenant_id__isnull=True, is_active=True))
    tenant_params = {p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id=tenant_id, is_active=True)}
    by_key = {p.input_key: tenant_params.get(p.input_key, p) for p in params}
    by_key.update(tenant_params)

    weights = {}
    for w in ConfigWeight.objects.filter(is_active=True).order_by(
            "tenant_id"):   # NULL first, tenant rows overwrite
        weights[(w.level, w.code)] = w

    # Leaves grouped by their parent roll-up node.
    leaves = {}
    for key, p in by_key.items():
        if not p.feeds_score:
            continue
        pv = values.get(key)
        score = float(pv.score) if pv and pv.score is not None else None
        parent = p.parent_code or p.category_code
        child_w = weights.get(("child", p.ref_code))
        leaves.setdefault(parent, []).append({
            "code": p.ref_code or key,
            # Carried alongside `code` because a ref code is NOT unique: the
            # sector mirrors put SEC_TAM and SEC_TAM_CAGR both on E.1, so a
            # caller that needs to address one leaf exactly — the Phase 1
            # scorecard, an override — cannot do it by ref code. The engine
            # ignores this key; only readers of the tree use it.
            "input_key": key,
            "weight": float(child_w.weight) if child_w else 100.0,
            "score": score,
        })

    categories = []
    for cw in sorted([w for (lvl, _c), w in weights.items()
                      if lvl == "category"], key=lambda w: w.code):
        subitems = [w for (lvl, _c), w in weights.items()
                    if lvl == "subitem" and w.parent_code == cw.code]
        children = []
        for si in sorted(subitems, key=lambda w: w.code):
            kids = leaves.get(si.code, [])
            if kids:
                children.append({"code": si.code, "label": si.label,
                                 "weight": float(si.weight),
                                 "children": kids})
            else:
                direct = leaves.get(si.code, [])
                children.append({"code": si.code, "label": si.label,
                                 "weight": float(si.weight),
                                 "score": (direct[0]["score"] if direct
                                           else None)})
        # Leaves attached straight to the category (no sub-item level).
        children.extend(leaves.get(cw.code, []))
        categories.append({"code": cw.code, "label": cw.label,
                           "weight": float(cw.weight), "children": children})
    return categories


def _coverage_population(assessment, tenant_id):
    """Every parameter the model expects, answered or not.

    Reference rows are included in the list and flagged, because
    `input_coverage()` excludes them itself — a cross-check going unanswered
    is not a coverage failure, since it was never going to be scored.
    """
    from fundos.assessment.models import ConfigParameter

    values = {pv.input_key: pv for pv in assessment.parameter_values.all()}
    base = {p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id__isnull=True, is_active=True)}
    base.update({p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id=tenant_id, is_active=True)})

    out = []
    for key, cfg in base.items():
        pv = values.get(key)
        out.append({"score": pv.score if pv else None,
                    "is_reference_only": not cfg.feeds_score})
    # A value with no config row still counts if it scored — it is answered
    # evidence, even though nothing rolls it up.
    for key, pv in values.items():
        if key not in base:
            out.append({"score": pv.score,
                        "is_reference_only": pv.is_reference_only})
    return out


def coverage_by_category(assessment, tenant_id=None):
    """Answered vs expected per category.

    A single overall coverage number hides the shape of the gap: 60% overall
    can mean every category two-thirds answered, or four categories complete
    and Financials entirely empty. Those are different deals and they need
    different next actions, so the breakdown is reported alongside the total.
    """
    from fundos.assessment.models import ConfigParameter

    tenant_id = tenant_id or getattr(assessment, "tenant_id", None)
    values = {pv.input_key: pv for pv in assessment.parameter_values.all()}
    base = {p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id__isnull=True, is_active=True, feeds_score=True)}
    base.update({p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id=tenant_id, is_active=True, feeds_score=True)})

    out = {}
    for key, cfg in base.items():
        code = cfg.category_code or "?"
        row = out.setdefault(code, {"categoryCode": code, "expected": 0,
                                    "answered": 0, "missing": []})
        row["expected"] += 1
        pv = values.get(key)
        if pv is not None and pv.score is not None:
            row["answered"] += 1
        elif len(row["missing"]) < 10:
            row["missing"].append(cfg.name or key)
    for row in out.values():
        row["coveragePct"] = round(
            row["answered"] / row["expected"] * 100, 1) if row["expected"] else 0.0
    return [out[k] for k in sorted(out)]


def run_scoring(assessment, *, user=None):
    """Score an assessment end to end and persist the result.

    Idempotent: re-running recomputes from the stored parameter values. It
    does NOT create a new version — versioning is a caller decision
    (Part 8 #2), because not every re-score is a new opinion.
    """
    from fundos.assessment.models import CategoryScore, ConfigWeight

    tenant_id = getattr(assessment, "tenant_id", None)
    stage, defaulted = resolve_stage(assessment)

    # 1. Band every parameter from config.
    for pv in assessment.parameter_values.all():
        band, score = band_parameter(pv, stage, tenant_id)
        pv.system_band = band or ""
        pv.system_score = score
        if not pv.is_overridden:
            pv.band = band or ""
            pv.score = score
        pv.save(update_fields=["band", "score", "system_band", "system_score"])

    # 2. Roll up.
    categories = _build_tree(assessment, tenant_id)

    # COVERAGE IS MEASURED AGAINST THE WHOLE MODEL, NOT AGAINST ITSELF.
    #
    # This previously counted only the ParameterValue rows that existed, so a
    # deal with four answered parameters out of eighty reported 100% coverage
    # — the denominator was the numerator. That made the figure meaningless
    # and, worse, silently disabled the suppression gate that is supposed to
    # withhold a headline score built on thin evidence. The denominator is the
    # set of parameters the model says should be answered; a parameter with no
    # row is exactly what "unanswered" means.
    params = _coverage_population(assessment, tenant_id)

    from fundos.engines.deal_assessment import score_assessment
    result = score_assessment(categories=categories, parameters=params)

    # 3. Persist category scores with applied weight.
    applied = {a["code"]: a["applied_weight"]
               for a in (result.get("applied_weights") or [])}
    CategoryScore.objects.filter(assessment=assessment).delete()
    for c in result["categories"]:
        w = ConfigWeight.objects.filter(level="category", code=c["code"],
                                        is_active=True).first()
        CategoryScore.objects.create(
            assessment=assessment, category_code=c["code"],
            label=(w.label if w else ""),
            weight=Decimal(str(c.get("weight", 0))),
            applied_weight=Decimal(str(applied.get(c["code"], 0))),
            score=(Decimal(str(c["score"])) if c.get("score") is not None
                   else None),
            breakdown={"applied": c.get("applied", [])})

    # 4. Persist the assessment-level result.
    assessment.deal_stage = stage
    assessment.stage_was_defaulted = defaulted
    assessment.overall_score = (Decimal(str(result["overall_score"]))
                                if result["overall_score"] is not None
                                else None)
    assessment.rating_band = result["rating_band"] or ""
    assessment.input_coverage_pct = Decimal(str(result["input_coverage_pct"]))
    findings = run_integrity_checks(assessment, result)
    assessment.audit_findings = findings
    assessment.audit_status = "failed" if any(
        f["severity"] == "error" for f in findings) else "passed"
    assessment.status = "scored"
    assessment.save()

    result["rating_meaning"] = RATING_MEANING.get(result["rating_band"], "")
    result["audit_findings"] = findings
    result["stage"] = stage
    result["stage_was_defaulted"] = defaulted
    return result


def apply_parameter_override(assessment, pv, *, score, reason, user=None):
    """Record a reviewer's score for one parameter and re-roll the totals.

    The whole cascade lives here rather than in a view, because there is more
    than one way into it — the parameter drawer's override and the Phase 1
    scorecard's — and two copies of "write the score, log it, re-score" would
    be two chances for the audit trail to be written only one of them.

    Three things happen in order, and the order is load-bearing:

      1. The score is written to `score` / `band` while `system_score` and
         `system_band` are left alone. Those two columns are what keeps an
         overridden scorecard auditable: the rule's own verdict survives
         beside the human's, so the disagreement is visible rather than
         being a number somebody changed.

      2. A ReviewLog row is created, closed. The override IS the trail entry —
         it is the one action that moves a score against the model's own
         judgement, so it is precisely what a later reader needs to find.
         Closed on creation because the decision has already been made and
         acted on; it is a record, not a request.

      3. `run_scoring` re-runs, which re-bands every parameter from config,
         rolls the sub-items and categories up through `weighted_rollup` —
         redistributing weight around anything unscored — and rewrites the
         overall score and rating ladder. The parent, category and headline
         numbers are therefore never computed by the caller.

    :returns: the refreshed assessment.
    """
    from fundos.assessment.models import ReviewLog

    # Captured before the write: once the override lands these columns hold
    # the new value, and the trail needs what it replaced.
    system_before = pv.raw_value or (
        f"{float(pv.score):g}" if pv.score is not None else "")
    band_before = pv.band

    pv.score = score
    pv.band = band_from_score(score, getattr(assessment, "tenant_id", None))
    pv.is_overridden = True
    pv.override_comment = reason
    pv.overridden_by = user
    pv.save(update_fields=["score", "band", "is_overridden",
                           "override_comment", "overridden_by"])

    ReviewLog.objects.create(
        assessment=assessment, field_or_topic=pv.input_key[:128],
        previous_value=f"{system_before} [{band_before or 'unassessed'}]",
        founder_value=f"{score:g} [{pv.band}]",
        founder_reason=reason[:2000],
        agent_action=("Override applied. The rule-based result is retained "
                      "in system_band / system_score."),
        status="closed", raised_by=user)

    run_scoring(assessment, user=user)
    assessment.refresh_from_db()
    logger.info("ASSESSMENT %s: override %s → %.2f by %s", assessment.id,
                pv.input_key, score, getattr(user, "email", "?"))
    return assessment


def band_from_score(score, tenant_id=None, cuts=None):
    """Display band for a score a human typed, or for a rolled-up average.

    Banded at the SCORING KEY'S OWN VALUES, read from config through
    `band_score` — not at numbers written here. The key is what the product
    owner edits when they retune the model, and a copy of it in this function
    would be a threshold they could move without the band beside the number
    ever moving with it.

    This never feeds back into scoring: the engine bands leaves from config and
    rolls the numbers up. It is a label on a result, not an input to one.

    A 10 bands as Excellent. There is no band above it — the band set is the
    model's, and a reviewer awarding 10 is saying "better than the rubric can
    express", not "a band we forgot to define". Reaching `Exceptional` stays a
    deliberate act recorded against the row, never a side effect of arithmetic.

    :param cuts: optional pre-read ``[(band, cut), …]``, best first, for a
        caller that is labelling many scores in one pass and has already read
        the scoring key. It is the same config by a shorter path — the ordering
        rule stays here, so there is still one place that decides which band a
        number lands in.
    """
    if score is None:
        return ""
    s = float(score)
    if cuts is None:
        cuts = [(band, band_score(band, tenant_id))
                for band in ("Excellent", "Good", "Fair")]
    for band, cut in cuts:
        if cut is not None and s >= float(cut):
            return band
    return "Poor"


def run_integrity_checks(assessment, result=None):
    """Automated checks (§5.3) — run before any result is shown as final.

    Returns findings rather than raising: a failing check should be VISIBLE
    on the scorecard, not a 500. A founder is better served by a score
    marked "audit failed, here is why" than by an error page.
    """
    from fundos.assessment.models import ConfigWeight

    findings = []

    def add(severity, code, message):
        findings.append({"severity": severity, "check": code,
                         "message": message})

    # 1. Category weights sum to 100.
    total = sum((w.weight for w in ConfigWeight.objects.filter(
        level="category", is_active=True)), Decimal("0"))
    if total != 100:
        add("error", "weights_sum",
            f"Category weights sum to {total}, not 100. Every score built on "
            f"this config is wrong.")

    # 2. Stage must be explicit.
    if assessment.stage_was_defaulted:
        add("warning", "stage_defaulted",
            f"Deal stage was not set and defaulted to {assessment.deal_stage}."
            f" Numeric parameters are banded against those cut-points; "
            f"confirm the stage before relying on this score.")

    # 3. Every override needs a written justification (§2.4.4).
    missing = assessment.parameter_values.filter(
        is_overridden=True, override_comment="").values_list(
            "input_key", flat=True)
    if missing:
        add("error", "override_uncommented",
            f"{len(missing)} override(s) with no written justification: "
            f"{', '.join(list(missing)[:5])}. The source model requires this "
            f"counter to read zero before sign-off.")

    # 4. Exceptional must only ever come from an override.
    bad = assessment.parameter_values.filter(
        band="Exceptional", is_overridden=False).values_list(
            "input_key", flat=True)
    if bad:
        add("error", "exceptional_without_override",
            f"Exceptional band set without an override on: "
            f"{', '.join(list(bad)[:5])}. 10 is reachable only through a "
            f"human decision.")

    # 5. Coverage floor — a confident-looking score on thin input.
    if assessment.input_coverage_pct and assessment.input_coverage_pct < 50:
        add("warning", "low_coverage",
            f"Only {assessment.input_coverage_pct:.0f}% of scored parameters "
            f"were answered. The rating is directionally weak until coverage "
            f"improves.")

    # 6. Reference-row contradictions (§2.4.5).
    findings.extend(_reference_contradictions(assessment))

    # 7. Evidence quality — how much of this score rests on something a
    #    reader can check. Separate module because these ask a different
    #    question from the five above: those test whether the assessment was
    #    BUILT correctly, these test whether it is WORTH quoting.
    from fundos.assessment import integrity
    findings.extend(integrity.checks(assessment, result))
    return findings


def _reference_contradictions(assessment):
    """§2.4.5 — ref rows exist to catch contradictions, not to be ignored.

    A ref row is compared ONLY against the scored row it actually cross-checks.
    The pairing is declared in the config, in one of two shapes:

      Leaf-level: the ref row shares its `ref_code` with the parameter it
        checks — A.1.b is both TEAM_MARQUEE_CLIENTS and ANC_FDR_NETWORK, which
        is why `clean_ref` is careful to land a "(ref)" row on the same code.
      Node-level: the ref row's `ref_code` IS its parent node, e.g.
        TEAM_CXO_SEATS on A.2, which cross-checks the four CXO seats A.2.a-d
        as a group.

    This used to compare every ref row against every scored row sharing a
    parent, which is a cross product, not a cross-check: ANC_FDR_NETWORK — the
    declared partner of TEAM_MARQUEE_CLIENTS alone — also contradicted
    ANC_FDR_EDU and TEAM_COFDR_YRS, purely for sitting in A.1 with them. A
    parameter with no declared partner must never produce a finding: the whole
    point of the rule is that two readings of the SAME fact disagree.
    """
    order = ["Poor", "Fair", "Good", "Excellent", "Exceptional"]
    from fundos.assessment.models import ConfigParameter
    cfg = {p.input_key: p for p in ConfigParameter.objects.filter(
        is_active=True)}

    rows = []
    for pv in assessment.parameter_values.all():
        p = cfg.get(pv.input_key)
        if not p or not pv.band:
            continue
        rows.append((p, pv))

    scored_by_ref, scored_by_parent = {}, {}
    for p, v in rows:
        if not p.feeds_score:
            continue
        if p.ref_code:
            scored_by_ref.setdefault(p.ref_code, []).append((p, v))
        scored_by_parent.setdefault(p.parent_code or p.category_code,
                                    []).append((p, v))

    out = []
    for rp, rv in [(p, v) for p, v in rows if not p.feeds_score]:
        if not rp.ref_code:
            # Nothing declares what this row cross-checks, so it checks
            # nothing. Guessing a partner is how the cross product got here.
            continue
        if rp.ref_code == (rp.parent_code or ""):
            partners = scored_by_parent.get(rp.ref_code, [])
        else:
            partners = scored_by_ref.get(rp.ref_code, [])

        for sp, sv in partners:
            if sp.input_key == rp.input_key:
                continue
            try:
                gap = order.index(sv.band) - order.index(rv.band)
            except ValueError:
                continue
            if gap >= 2:
                out.append({
                    "severity": "warning", "check": "ref_contradiction",
                    # The keys are carried structurally as well as in the
                    # sentence. A reader that needs to show this finding
                    # against the row it concerns — the Phase 1 diligence
                    # list — should not have to parse the message back into
                    # the two parameters it names.
                    "parameters": [sp.input_key, rp.input_key],
                    "message": (
                        f"{sp.input_key} scored {sv.band} but its "
                        f"cross-check {rp.input_key} is {rv.band}. Worth "
                        f"reconciling before this goes to an investor.")})
    return out
