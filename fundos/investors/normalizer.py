"""Sector string normalisation — DB-backed port of sector_normalizer.py.

WHAT CHANGED FROM THE SUPPLIED MODULE, AND WHY
----------------------------------------------
The algorithm is unchanged and was already right: exact lookup first, then
rapidfuzz token_sort_ratio against the full alias pool, then return the raw
string unchanged if nothing clears the threshold. Never guess, never mutate
the source.

Three things moved:

  * CANONICAL_ALIASES was a module-level dict of 30 sectors. It is now
    CanonicalSector + SectorAlias rows. The supplied module already merged a
    sector_config.json over the built-ins at import time, which proves the
    pattern was wanted — this finishes the job.

  * MATCH_THRESHOLD = 78 was a constant. It is now an AppConfiguration value,
    tunable with a preview that shows how many queued items a new value would
    clear before it is saved.

  * sector_unknowns.log was an append-only file. It is now SectorReviewItem
    rows. A log file records a problem; a queue lets someone fix it, and one
    click creates the alias that stops it recurring.

The threshold behaviour is worth restating because it is the safety property:
below it, the ORIGINAL STRING IS RETURNED UNCHANGED. A sector we cannot map is
visibly unmapped, not silently mapped to the nearest plausible thing.
"""
import logging

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 78

_EXACT = None       # alias(lower) → CanonicalSector.id
_POOL = None        # list of alias strings for the fuzzy pool
_BY_ID = None       # id → CanonicalSector


def _threshold():
    from fundos.config.models import AppConfiguration
    cfg = AppConfiguration.objects.first()
    return int(getattr(cfg, "sector_match_threshold", DEFAULT_THRESHOLD)
               or DEFAULT_THRESHOLD)


def build_index(force=False):
    """Build the in-process lookup tables. Cheap; called on first use."""
    global _EXACT, _POOL, _BY_ID
    if _EXACT is not None and not force:
        return
    from fundos.investors.models import CanonicalSector, SectorAlias

    _BY_ID = {str(s.id): s for s in CanonicalSector.objects.filter(is_active=True)}
    _EXACT = {}
    for s in _BY_ID.values():
        _EXACT[s.name.strip().lower()] = str(s.id)
    for a in SectorAlias.objects.select_related("sector"):
        if str(a.sector_id) in _BY_ID:
            _EXACT[a.alias] = str(a.sector_id)
    _POOL = list(_EXACT.keys())


def invalidate():
    """Call after editing sectors or aliases in admin."""
    global _EXACT, _POOL, _BY_ID
    _EXACT = _POOL = _BY_ID = None
    from fundos.investors import matching
    matching.clear_caches()


def normalize(raw, *, record_misses=True, level=None):
    """Return (CanonicalSector | None, matched_exactly: bool, score: float).

    A None sector means the string could not be mapped above the threshold.
    Callers store the raw string regardless — normalisation is a derived view,
    never a replacement for the source value.
    """
    if not raw or not str(raw).strip():
        return None, False, 0.0

    build_index()
    clean = str(raw).strip()
    lower = clean.lower()

    sid = _EXACT.get(lower)
    if sid:
        return _BY_ID.get(sid), True, 100.0

    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        logger.warning("NORMALIZER: rapidfuzz unavailable — exact match only. "
                       "Fuzzy fallback is disabled and %r was not mapped.", clean)
        if record_misses:
            _record_miss(clean, "", 0.0)
        return None, False, 0.0

    if not _POOL:
        if record_misses:
            _record_miss(clean, "", 0.0)
        return None, False, 0.0

    hit = process.extractOne(lower, _POOL, scorer=fuzz.token_sort_ratio)
    if not hit:
        if record_misses:
            _record_miss(clean, "", 0.0)
        return None, False, 0.0

    alias, score, _ = hit
    if score >= _threshold():
        return _BY_ID.get(_EXACT[alias]), False, float(score)

    best = _BY_ID.get(_EXACT.get(alias))
    if record_misses:
        _record_miss(clean, best.name if best else alias, float(score))
    return None, False, float(score)


def _record_miss(raw, best, score):
    """Upsert a review item, counting repeats.

    Occurrence count is the prioritisation signal — a string appearing 40 times
    is worth an alias, one appearing once probably is not.
    """
    from django.db.models import F
    from fundos.investors.models import SectorReviewItem
    obj, created = SectorReviewItem.objects.get_or_create(
        raw_value=raw[:255],
        defaults={"best_candidate": best[:255], "best_score": round(score, 2)})
    if not created:
        SectorReviewItem.objects.filter(pk=obj.pk).update(
            occurrences=F("occurrences") + 1,
            best_candidate=best[:255], best_score=round(score, 2))


def preview_threshold(new_threshold):
    """How many open review items a proposed threshold would clear.

    Shown live beside the slider in admin so a threshold change is a decision
    with a visible consequence rather than a guess.
    """
    from fundos.investors.models import SectorReviewItem
    open_items = SectorReviewItem.objects.filter(status="open")
    would_clear = open_items.filter(best_score__gte=new_threshold).count()
    return {"openItems": open_items.count(), "wouldClear": would_clear,
            "wouldRemain": open_items.count() - would_clear}
