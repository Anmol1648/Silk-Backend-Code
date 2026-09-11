"""Investor identity resolution.

THE PROBLEM
-----------
"Accel", "Accel Partners" and "Accel India" are one investor or three,
depending on a judgement no string algorithm can make reliably. Getting it
wrong is quiet: deal counts change, deal counts drive the tier-1 ranking, and
a shortlist built on wrong counts looks entirely plausible. There is nothing
on screen that would appear wrong.

THE RULE (as decided by the business)
-------------------------------------
  score >= 100      Exact normalised match. Merged automatically.
  score >= 90       High-confidence fuzzy match. Merged automatically.
  score <  90       NOT merged. A candidate row is queued.

Queued candidates are adjudicated by an LLM with web search — because the
question ("is Accel India the same firm as Accel?") is answered by knowing
something about the world, not by comparing strings harder.

THE LLM RUN IS TRIGGERED MANUALLY BY A CONFIG ADMIN. It never runs as part of
a refresh. Two reasons: an automatic merge on a machine judgement is exactly
the silent failure described above, and web-search calls cost money that
should be spent deliberately. The LLM produces a verdict and a reason; an
admin still decides. The admin can also merge, split or edit by hand at any
point without involving the LLM at all.
"""
import logging
import re

from django.utils import timezone

logger = logging.getLogger(__name__)

AUTO_MERGE_THRESHOLD = 90

# Legal suffixes stripped before comparison. Deliberately conservative — these
# genuinely carry no distinguishing information. "India", "Asia", "Global" are
# NOT here: they are exactly the tokens that distinguish one arm of a firm from
# another, and stripping them would auto-merge the cases that most need a human.
_SUFFIXES = (
    r"\b(llp|llc|ltd|limited|inc|incorporated|corp|corporation|pvt|private|"
    r"plc|gmbh|sa|nv|bv|co|company|lp|l\.p\.|pte)\b"
)


def normalise_name(raw):
    """Casefold, strip punctuation and legal suffixes, collapse whitespace."""
    if not raw:
        return ""
    s = str(raw).strip().lower()
    s = re.sub(r"[&]", " and ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(_SUFFIXES, " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


#: Below this, two names have nothing to do with each other and queueing the
#: pair would only give a human noise to clear.
REVIEW_FLOOR = 60


def _token_sort_ratio(a, b):
    """A ratio in 0-100 with no third-party dependency.

    `difflib` is in the standard library, so this exists on every machine.
    Tokens are sorted first for the same reason rapidfuzz sorts them: word
    order is not identity.
    """
    import difflib

    left = " ".join(sorted(str(a or "").split()))
    right = " ".join(sorted(str(b or "").split()))
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio() * 100


def _rank(key, candidates):
    """Score `key` against every known investor.

    :returns: ``(best, best_score, runners, scorer)``.

    The scorer is named because it decides what may be done with the score.
    `rapidfuzz` was absent on a development box and this function returned
    nothing at all: no match, no near miss, and no candidate queued -- so a
    name one letter off an existing investor was created as a second firm
    with nobody told. A near miss nobody is told about is the failure this
    whole module exists to prevent, so the fallback keeps the QUEUE working
    even where it is not trusted to merge.
    """
    if not candidates:
        return None, 0.0, [], "none"

    pool = [c[2] for c in candidates]
    try:
        from rapidfuzz import fuzz, process

        hits = [(idx, float(score)) for _alias, score, idx in
                process.extract(key, pool, scorer=fuzz.token_sort_ratio,
                                limit=4)]
        scorer = "rapidfuzz"
    except ImportError:
        logger.warning("IDENTITY: rapidfuzz unavailable — scoring with the "
                       "standard library instead. Near misses are still "
                       "queued for review; nothing is auto-merged on this "
                       "scorer.")
        scored = sorted(((i, _token_sort_ratio(key, name))
                         for i, name in enumerate(pool)),
                        key=lambda pair: pair[1], reverse=True)
        hits = scored[:4]
        scorer = "difflib"

    best, best_score, runners = None, 0.0, []
    for idx, score in hits:
        cid, cname, _ = candidates[idx]
        if best is None:
            best, best_score = candidates[idx], score
        else:
            runners.append({"investorId": str(cid), "name": cname,
                            "score": round(score, 2)})
    return best, best_score, runners, scorer


def resolve(raw_name, *, tenant_id=None, create_if_missing=True):
    """Resolve a raw investor name to an Investor.

    Returns (investor, action) where action is one of:
        "exact"    — matched an existing normalised name or alias
        "fuzzy"    — matched at or above the auto-merge threshold
        "created"  — no candidate cleared the bar; a new investor was created
                     and a review candidate queued if there was a near miss
    """
    from fundos.investors.models import Investor, InvestorAliasCandidate

    clean = str(raw_name or "").strip()
    if not clean:
        return None, "empty"

    key = normalise_name(clean)
    if not key:
        return None, "empty"

    qs = Investor.objects.all()
    if tenant_id:
        qs = qs.filter(tenant_id=tenant_id)

    exact = qs.filter(normalised_name=key).first()
    if exact:
        _remember_alias(exact, clean)
        return exact, "exact"

    candidates = list(qs.values_list("id", "name", "normalised_name"))
    best, best_score, runners, scorer = _rank(key, candidates)

    # A FALLBACK SCORER MAY QUEUE BUT MAY NOT MERGE. Merging is the
    # irreversible half of this decision, and it was made by a library that
    # is not installed here; a stand-in agreeing with it is not something
    # this module can check.
    if best and best_score >= AUTO_MERGE_THRESHOLD and scorer == "rapidfuzz":
        inv = qs.get(id=best[0])
        _remember_alias(inv, clean)
        logger.info("IDENTITY: auto-merged %r into %r at %.1f",
                    clean, inv.name, best_score)
        return inv, "fuzzy"

    if not create_if_missing:
        return None, "unresolved"

    inv = Investor.objects.create(name=clean, normalised_name=key,
                                  tenant_id=tenant_id, aliases=[])

    # Near miss below the threshold: queue it. The new investor still exists
    # so the refresh completes and the data is usable; the queue records that
    # this one deserves a second look.
    if best and best_score > REVIEW_FLOOR:
        cand, created = InvestorAliasCandidate.objects.get_or_create(
            raw_name=clean[:255],
            defaults={"candidate_id": best[0],
                      "fuzzy_score": round(best_score, 2),
                      "alternatives": runners})
        if not created:
            cand.occurrences += 1
            cand.save(update_fields=["occurrences"])
        logger.info("IDENTITY: queued %r (best %r @ %.1f) for review.",
                    clean, best[1], best_score)

    return inv, "created"


def _remember_alias(investor, raw):
    """Record a spelling we have seen resolve to this investor."""
    if raw and raw != investor.name and raw not in (investor.aliases or []):
        investor.aliases = list(investor.aliases or []) + [raw]
        investor.save(update_fields=["aliases"])


# ---------------------------------------------------------------------------
# LLM adjudication — MANUAL TRIGGER ONLY
# ---------------------------------------------------------------------------

ADJUDICATION_SYSTEM = (
    "You determine whether two investor names refer to the same investing "
    "entity. Search the web to check.\n\n"
    "Answer 'same' only if they are the same legal entity or the same fund "
    "family operating under one investment decision process. Answer "
    "'different' if they are separate funds, separate entities, or one is a "
    "regional arm that raises and deploys its own capital. If the evidence "
    "does not settle it, answer 'unclear' — do not guess.\n\n"
    'Return JSON only: {"verdict":"same"|"different"|"unclear",'
    '"confidence":0.0-1.0,"reasoning":"one or two sentences",'
    '"sources":["url"]}'
)


def adjudicate(candidate, *, user=None):
    """Ask the LLM whether a queued candidate is the same entity.

    Called from the admin action. Never from the pipeline. Writes the verdict
    onto the candidate and leaves the merge decision to a human — the verdict
    is evidence for the admin, not an instruction to the system.
    """
    from fundos.llm.adapter import llm_generate

    prompt = (
        f'Name A: "{candidate.candidate.name}"\n'
        f'Name B: "{candidate.raw_name}"\n\n'
        "Are these the same investing entity?"
    )

    try:
        data = llm_generate(
            role="investor_identity",
            system=ADJUDICATION_SYSTEM,
            prompt=prompt,
            context={"nameA": candidate.candidate.name,
                     "nameB": candidate.raw_name},
        ) or {}
    except Exception as e:
        logger.error("IDENTITY: adjudication failed for %s: %s",
                     candidate.id, e, exc_info=True)
        raise

    if isinstance(data, dict) and "content" in data and isinstance(
            data.get("content"), dict):
        data = data["content"]

    verdict = str(data.get("verdict", "")).lower()
    if verdict not in ("same", "different", "unclear"):
        verdict = "unclear"

    candidate.llm_verdict = verdict
    try:
        candidate.llm_confidence = round(float(data.get("confidence") or 0), 2)
    except (TypeError, ValueError):
        candidate.llm_confidence = None
    candidate.llm_reasoning = str(data.get("reasoning", ""))[:2000]
    candidate.llm_sources = data.get("sources") or []
    candidate.llm_run_at = timezone.now()
    candidate.llm_run_by = user
    candidate.status = "llm_done"
    candidate.save()

    logger.info("IDENTITY: adjudicated %s → %s (%.2f)",
                candidate.id, verdict, candidate.llm_confidence or 0)
    return candidate


def merge(candidate, *, user=None):
    """Merge the raw name into the candidate investor.

    Moves every InvestorDeal row across, absorbs the alias, deletes the
    duplicate shell and recomputes aggregates. Relationship fields on the
    surviving investor are untouched.
    """
    from django.db import transaction
    from fundos.investors.models import Investor, InvestorDeal

    target = candidate.candidate
    if not target:
        raise ValueError("Candidate has no target investor to merge into.")

    dup = Investor.objects.filter(
        normalised_name=normalise_name(candidate.raw_name)).exclude(
        id=target.id).first()

    with transaction.atomic():
        if dup:
            InvestorDeal.objects.filter(investor=dup).update(investor=target)
            # Keep any relationship data the duplicate accumulated rather than
            # discarding it — a human typed it somewhere and it is not
            # recoverable from the source.
            for f in Investor.RELATIONSHIP_FIELDS:
                incoming = getattr(dup, f, None)
                if incoming and not getattr(target, f, None):
                    setattr(target, f, incoming)
            target.aliases = list(dict.fromkeys(
                list(target.aliases or []) + [dup.name] + list(dup.aliases or [])))
            target.save()
            dup.delete()
        else:
            _remember_alias(target, candidate.raw_name)

        candidate.status = "merged"
        candidate.resolved_by = user
        candidate.resolved_at = timezone.now()
        candidate.save(update_fields=["status", "resolved_by", "resolved_at"])

    from fundos.investors.pipeline import recompute_investor_aggregates
    recompute_investor_aggregates(investor_ids=[target.id])
    logger.info("IDENTITY: merged %r into %r", candidate.raw_name, target.name)
    return target


def keep_separate(candidate, *, user=None):
    candidate.status = "separate"
    candidate.resolved_by = user
    candidate.resolved_at = timezone.now()
    candidate.save(update_fields=["status", "resolved_by", "resolved_at"])
    return candidate
