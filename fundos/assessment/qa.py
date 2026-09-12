"""Questions about the scorecard, and overrides it may propose.

The profile chat answers from the company's own dossier. This one answers
from the SCORECARD -- the parameter, its band, the rubric it was scored
against, the anchor's written band definitions, and the citations behind it.
Asking the profile chat why Team scored 6.87 gets an answer about the
company; it has never seen a parameter.

THE CHAT NEVER CHANGES A SCORE. It proposes one, and a person applies it
through the override endpoint that already exists -- which demands a reason,
keeps `system_score` and `system_band` in their own columns so the machine's
answer is never destroyed, and re-rolls the totals. A chat that could move a
score on its own would be an override with nobody's name against it, which
is the single thing that whole path exists to prevent.

Every proposal is checked before it is shown: the parameter must exist on
this assessment, the score must be inside the scale, the band must be the one
that score actually belongs to, and there must be a reason. The impact is
computed by the engine, never taken from the model.
"""
import logging

logger = logging.getLogger(__name__)

#: The band a score falls in, by the engine's own table, so a card can never
#: read "Good 3.0".
_BAND_FLOORS = (("Excellent", 9), ("Good", 7), ("Fair", 5), ("Poor", 0))

MAX_PROPOSALS = 5
MAX_REASON_CHARS = 1000


def band_for_score(score):
    """The band a score belongs to. Exceptional is never produced here."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return ""
    for band, floor in _BAND_FLOORS:
        if value >= floor:
            return band
    return "Poor"


def _context(assessment, ref):
    """The scorecard material an answer has to be grounded in."""
    from fundos.assessment import hierarchy as H
    from fundos.assessment.v2_serializers import build_v2_parameter_detail

    node = {"ref": ref or "", "name": H.NAMES.get(ref or "", ""),
            "level": H.LEVELS.get(ref or "", "")}
    rows = []
    for terminal in _terminals(ref):
        pv = _value_for(assessment, terminal)
        if pv is None:
            rows.append({"ref": terminal, "name": H.NAMES.get(terminal, ""),
                         "score": None, "band": "", "scored": False})
            continue
        rows.append({
            "ref": terminal,
            "name": H.NAMES.get(terminal, ""),
            "inputKey": pv.input_key,
            "score": float(pv.score) if pv.score is not None else None,
            "band": pv.band or "",
            "scored": pv.score is not None,
            "isOverridden": bool(pv.is_overridden),
            "reasoning": (pv.justification or "")[:1200],
            "evidence": (pv.source_detail or "")[:1200],
        })

    detail = None
    if ref and H.LEVELS.get(ref) in ("grandchild", "child"):
        try:
            detail = build_v2_parameter_detail(assessment, ref)
        except Exception as exc:            # pragma: no cover - never fatal
            logger.debug("ASSESSMENT QA: detail for %s unavailable: %s",
                         ref, exc)

    return {
        "node": node,
        "rows": rows[:40],
        "overall": {
            "score": (float(assessment.overall_score)
                      if assessment.overall_score is not None else None),
            "rating": assessment.rating_band or "",
            "stage": assessment.deal_stage or "",
        },
        "rubric": (detail or {}).get("rubric"),
        "anchor": (detail or {}).get("anchor"),
    }


def _terminals(ref):
    from fundos.assessment import hierarchy as H
    from fundos.assessment.suggestions import _terminals_under

    if not ref:
        return list(H.TERMINAL_REFS)
    return _terminals_under(ref)


def _value_for(assessment, ref):
    """The row a ref addresses, however the caller spelled it.

    A ParameterValue's own `ref_code` is often blank — the engine addresses
    rows by input key — so a lookup on that column alone matches nothing for
    exactly the refs a panel asks about.
    """
    from fundos.assessment.suggestions import keys_by_ref

    return (assessment.parameter_values.filter(input_key=ref).first()
            or assessment.parameter_values.filter(ref_code=ref).first()
            or assessment.parameter_values.filter(
                input_key=keys_by_ref().get(ref, "")).first())


def answer(assessment, question, *, ref="", user=None):
    """Answer a scorecard question, with any override it wants to propose."""
    from fundos.assessment import suggestions
    from fundos.llm.adapter import llm_generate

    from fundos.assessment.suggestions import normalise_ref

    ref = normalise_ref(ref)
    context = _context(assessment, ref)
    result = llm_generate(
        role="assessment_qa",
        context={"scorecard": context, "question": question},
        section_context={"question": question, "ref": ref or ""},
        user=user, calling_context="assessment.qa")

    if not isinstance(result, dict):
        return result

    result["proposedOverrides"] = validate(
        assessment, result.get("proposedOverrides"))
    result["suggestions"] = (suggestions.for_ref(assessment, ref) if ref
                             else [])
    return result


def validate(assessment, raw):
    """Turn whatever came back into overrides a person could actually apply.

    Anything unapplyable is dropped and logged. A card someone clicks Apply
    on and watches fail is worse than no card.
    """
    from fundos.assessment import impact

    out = []
    if not isinstance(raw, list):
        return out

    for item in raw[:MAX_PROPOSALS]:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or item.get("parameter") or "").strip()
        pv = _value_for(assessment, ref) if ref else None
        if pv is None:
            logger.info("ASSESSMENT QA: dropped an override for %r — no such "
                        "row on this assessment.", ref)
            continue

        try:
            score = float(item.get("score"))
        except (TypeError, ValueError):
            logger.info("ASSESSMENT QA: dropped %r — no numeric score.", ref)
            continue
        if not 0 <= score <= 10:
            logger.info("ASSESSMENT QA: dropped %r — %s is outside the "
                        "scale.", ref, score)
            continue

        reason = str(item.get("reason") or "").strip()
        if not reason:
            # The override endpoint refuses one without a reason, so a card
            # without one could never be applied.
            logger.info("ASSESSMENT QA: dropped %r — no reason given.", ref)
            continue

        current = float(pv.score) if pv.score is not None else None
        if current is not None and abs(current - score) < 0.005:
            continue

        preview = impact.preview(assessment, {pv.input_key: score})
        # THE CARD IS READ BY A PERSON. A ParameterValue's own `ref_code` is
        # usually blank, so this showed "TEAM_ADVISORS" twice -- as the ref
        # and as the name -- where the panel beside it says "A.3.b Advisor
        # Quality". The scorecard's own address and label, or nothing.
        display_ref = normalise_ref(pv.ref_code or ref)
        out.append({
            "id": f"ovr_{len(out) + 1}",
            "ref": display_ref,
            "inputKey": pv.input_key,
            "name": _name_of(display_ref),
            "currentScore": current,
            "currentBand": pv.band or "",
            "proposedScore": round(score, 2),
            # DERIVED, never taken from the model: a card reading "Good 3.0"
            # is two answers to one question.
            "proposedBand": band_for_score(score),
            "reason": reason[:MAX_REASON_CHARS],
            "impact": preview,
            "impactSentence": impact.sentence(preview),
        })
    return out


def _name_of(ref):
    from fundos.assessment import hierarchy as H

    return H.NAMES.get(ref, ref)


def normalise_ref(ref):
    from fundos.assessment.suggestions import normalise_ref as _n

    return _n(ref)
