"""What to ask about a scorecard row, at whatever level you are looking at.

The assessment panel offers three questions -- what would settle this, how
could it be improved, what happens to the score -- and two of the three are
not opinions at all:

    "How can it be improved?"    The ANCHOR says what each band requires.
                                 Moving Poor to Good is a written definition,
                                 not a guess.

    "What happens to the score?" Arithmetic. The engine reruns its own
                                 roll-up with the row substituted, so the
                                 figure includes the redistribution nobody
                                 does in their head.

So the suggestions carry those answers with them. A founder gets the number
before asking, and the chat is left to do the part that actually needs
judgement.

THREE LEVELS, THREE DIFFERENT QUESTIONS. A grandchild is a row that scores,
and its questions are about evidence and bands. A child is a group, and its
question is which of its rows is holding it back. A category is a fifth of a
rating, and its question is where the largest single lever is. Asking a
category "what would settle this?" is a category error -- nothing settles a
category, its rows do.
"""
import logging

from fundos.assessment import hierarchy as H

logger = logging.getLogger(__name__)

#: Two suggestions per node keeps a panel readable; the third is added only
#: when the row is unevidenced, which is a different problem from a low score.
MAX_PER_NODE = 4

#: Bands worth asking about moving TO. Exceptional is override-only and
#: suggesting it would be suggesting a 10 with no human decision behind it.
_TARGET_BAND = "Good"

_WEAK_BANDS = ("Poor", "Fair", "poor", "fair")


def _terminals_under(ref):
    """Every scoring row beneath a node, itself included when it is one."""
    level = H.LEVELS.get(ref)
    if level == "grandchild" or ref in H.TERMINAL_REFS:
        return [ref]
    return [t for t in H.TERMINAL_REFS if _ancestor_of(ref, t)]


def _ancestor_of(ref, terminal):
    node = terminal
    while node:
        parent = H.PARENT_OF.get(node)
        if parent == ref:
            return True
        node = parent
    return False


def keys_by_ref():
    """``{ref_code: input_key}`` from config, which is the only place both
    live.

    The hierarchy speaks in refs (A.1.d); a ParameterValue is addressed by
    its input key (TEAM_ADVISORS) and its own `ref_code` column is often
    blank — the engine never needed it. Joining on that column silently
    matched nothing, so a panel asked about A.1.d saw no row and offered no
    questions.

    A ref is NOT unique: a terminal is fed by a rubric row, an anchor and
    sometimes a cross-check, all carrying the same code. The row that feeds
    the score wins, exactly as the parameter-detail endpoint resolves it.
    """
    from fundos.assessment.models import ConfigParameter

    out = {}
    rows = (ConfigParameter.objects.filter(is_active=True)
            .exclude(ref_code="")
            .order_by("-feeds_score", "sort_order", "input_key"))
    for cfg in rows:
        out.setdefault(cfg.ref_code, cfg.input_key)
    return out


def _values_by_ref(assessment):
    """``{ref_code: ParameterValue}`` for the rows that feed the score."""
    by_key = {pv.input_key: pv for pv in assessment.parameter_values.all()}
    out = {}
    for ref, key in keys_by_ref().items():
        pv = by_key.get(key)
        if pv is not None:
            out[ref] = pv
    # A row whose own ref_code IS filled in still answers to it.
    for pv in by_key.values():
        if pv.ref_code and pv.ref_code not in out:
            out[pv.ref_code] = pv
    return out


def _is_evidenced(pv):
    return bool(pv is not None and (pv.source_detail or "").strip())


def _name(ref):
    return H.NAMES.get(ref, ref)


def ref_for_key(key):
    """``TEAM_ADVISORS`` -> ``A.1.d``. "" when config has no ref for it."""
    from fundos.assessment.models import ConfigParameter

    cfg = (ConfigParameter.objects.filter(input_key=key, is_active=True)
           .exclude(ref_code="").order_by("-feeds_score").first())
    return (cfg.ref_code if cfg else "") or ""


def normalise_ref(ref):
    """A hierarchy ref, whichever address the caller used.

    A row has two names — the ref the scorecard displays (A.1.d) and the key
    the engine scores it by (TEAM_ADVISORS) — and a panel may hold either.
    Accepting only one would answer half the calls with an empty list, which
    reads as "nothing to ask" rather than "wrong address".
    """
    ref = (ref or "").strip()
    if not ref or ref in H.LEVELS:
        return ref
    return ref_for_key(ref) or ref


def for_ref(assessment, ref):
    """Suggestions for one node, most useful first.

    Returns ``[]`` for a ref the hierarchy does not know -- a panel showing
    questions about a parameter that does not exist is worse than one
    showing none.
    """
    ref = normalise_ref(ref)
    level = H.LEVELS.get(ref)
    if not level:
        return []
    if level == "grandchild" or (level == "child" and ref in H.TERMINAL_REFS):
        return _for_terminal(assessment, ref, level)
    return _for_group(assessment, ref, level)


def _for_terminal(assessment, ref, level):
    from fundos.assessment import impact

    values = _values_by_ref(assessment)
    pv = values.get(ref)
    name = _name(ref)
    band = (pv.band if pv else "") or ""
    out = []

    if not _is_evidenced(pv):
        out.append(_item(
            ref, level, "evidence",
            f"Why is {name} not evidenced?",
            f"Why is {name} not evidenced, and what would count as evidence "
            f"for it?",
            "no source recorded for this row"))

    out.append(_item(
        ref, level, "settle",
        f"What would settle {name}?",
        f"What document or figure would settle {name}? Name what to ask the "
        f"company for.",
        _settle_why(ref, pv)))

    if band in _WEAK_BANDS or not band:
        out.append(_item(
            ref, level, "improve",
            f"How can {name} be improved?",
            f"{name} is scored {band or 'unscored'}. What does the anchor "
            f"require for a higher band, and what is missing here?",
            f"currently {band}" if band else "not scored"))

    preview, target = impact.if_settled_at(assessment, _key_of(pv, ref),
                                           _TARGET_BAND)
    line = impact.sentence(preview) if preview else ""
    if line:
        out.append(_item(
            ref, level, "impact",
            f"If {name} were settled, what happens to the score?",
            f"If {name} were settled at {_TARGET_BAND}, what happens to the "
            f"score and why?",
            f"at {_TARGET_BAND} ({target}/10), {line}",
            impact=preview))

    return out[:MAX_PER_NODE]


def _key_of(pv, ref):
    """The address the engine knows a row by: its input key, not its ref."""
    if pv is not None and pv.input_key:
        return pv.input_key
    return keys_by_ref().get(ref) or ref


def _settle_why(ref, pv):
    """The workbook's own line about what to produce, where it has one."""
    from fundos.assessment.models import ConfigParameter

    key = _key_of(pv, ref)
    cfg = (ConfigParameter.objects.filter(input_key=key, is_active=True)
           .first()
           or ConfigParameter.objects.filter(ref_code=ref, is_active=True)
           .order_by("-feeds_score").first())
    for attribute in ("if_missing", "where_to_find"):
        text = (getattr(cfg, attribute, "") or "").strip() if cfg else ""
        if text:
            return text[:200]
    return "no source recorded for this row" if not _is_evidenced(pv) else ""


def _for_group(assessment, ref, level):
    """A child or a category: which row beneath it is worth attention."""
    from fundos.assessment import impact

    values = _values_by_ref(assessment)
    terminals = _terminals_under(ref)
    name = _name(ref)
    if not terminals:
        return []

    scored = [(t, values.get(t)) for t in terminals]
    unevidenced = [t for t, pv in scored if not _is_evidenced(pv)]
    blank = [t for t, pv in scored if pv is None or pv.score is None]
    weakest = _weakest(scored)

    out = []
    if weakest:
        out.append(_item(
            ref, level, "weakest",
            f"What is holding {name} back?",
            f"Which rows are holding {name} back, and by how much? Start "
            f"with the heaviest.",
            f"{_name(weakest)} is the lowest scoring row here"))

    if blank:
        out.append(_item(
            ref, level, "gaps",
            f"{len(blank)} of {len(terminals)} rows in {name} are unscored",
            f"Which rows in {name} were never scored, and what would each "
            f"one need?",
            f"a blank row does not score zero -- its weight moves to its "
            f"siblings, so {len(terminals) - len(blank)} rows carry all of "
            f"{name}"))
    elif unevidenced:
        out.append(_item(
            ref, level, "evidence",
            f"{len(unevidenced)} rows in {name} have no source",
            f"Which rows in {name} are scored without a citation, and what "
            f"evidence would each need?",
            "scored, but with no source recorded"))

    if weakest:
        pv = values.get(weakest)
        preview, target = impact.if_settled_at(
            assessment, _key_of(pv, weakest), _TARGET_BAND)
        line = impact.sentence(preview) if preview else ""
        if line:
            out.append(_item(
                ref, level, "impact",
                f"What would settling {_name(weakest)} be worth?",
                f"If {_name(weakest)} moved to {_TARGET_BAND}, what happens "
                f"to {name} and to the overall score?",
                f"at {_TARGET_BAND} ({target}/10), {line}",
                impact=preview))

    return out[:MAX_PER_NODE]


def _weakest(scored):
    """The lowest-scoring row beneath a node, or None when none scored."""
    have = [(t, pv) for t, pv in scored if pv is not None
            and pv.score is not None]
    if not have:
        return None
    return min(have, key=lambda pair: float(pair[1].score))[0]


def _item(ref, level, kind, label, question, why, impact=None):
    out = {"ref": ref, "level": level, "kind": kind, "label": label,
           "question": question, "why": why}
    if impact is not None:
        out["impact"] = impact
    return out


def for_assessment(assessment, refs=None):
    """``{ref: [suggestion]}`` for every node asked for.

    With no refs, every category -- the panel's top level, and the cheapest
    useful answer for a client that has not drilled in yet.
    """
    wanted = list(refs or H.CATEGORY_CODES)
    return {ref: for_ref(assessment, ref) for ref in wanted}
