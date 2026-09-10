"""Pipeline knobs, resolved from AppConfiguration at call time.

Every value here is admin-editable (Django admin → Application configuration)
and read fresh on each call — never cached at import — so a change takes
effect on the next run without a restart. Each accessor carries a safe default
so a missing config row, or a column that has not been migrated yet, degrades
to the shipped behaviour rather than raising.

This mirrors :mod:`fundos.config.feature_flags`, which is the established
pattern for the same problem; the accessors live here rather than there only
because they are pipeline-specific and would otherwise bloat a module every
request path imports.
"""
from fundos.config.feature_flags import get_flag

# Shipped defaults — the values the pipeline was tuned on, and the fallback
# whenever the admin has expressed no opinion.
#
# Concurrency 5: the live Zerodha run took ~11 minutes wall clock with the
# research stage dominating it, and the ten batches are independent, so this is
# the one lever that shortens a run without changing what it costs — Gemini
# bills search grounding per request, not per second. Five rather than ten
# because the ceiling worth respecting is the provider's per-minute quota, not
# the batch count: at ten the whole bank lands in one burst and a 429 costs a
# retry on every batch at once, which is slower than not having fanned out.
DEFAULT_RESEARCH_CONCURRENCY = 5
DEFAULT_DOCUMENT_TIMEOUT_S = 600

# LLMConfigProfile codes. Both are seeded pointing at gemini-2.5-flash; an
# admin repoints the whole pipeline at another model by editing those two rows
# (LLM → Config profiles), or swaps in different rows entirely by changing
# these settings.
DEFAULT_RESEARCH_PROFILE = "profile.research"
DEFAULT_SYNTHESIS_PROFILE = "profile.synthesis"


def _int(name, default, *, minimum=1, maximum=None):
    """An integer flag, clamped. A nonsense admin value must not take a run
    down, so an unparseable entry falls through to the shipped default and an
    out-of-range one is clamped rather than rejected."""
    try:
        value = int(get_flag(name, default))
    except (TypeError, ValueError):
        return default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _text(name, default):
    """A string flag, with blank treated as unset."""
    value = get_flag(name, default)
    return (str(value).strip() if value is not None else "") or default


def max_parallelism():
    """The most threads this deployment's database can support. 1 or many.

    SQLite serialises writers at the file level and raises "database table is
    locked" when two connections write at once. Every call the pipeline makes
    writes — an `LLMCallLog` row, a progress update, an activity event — so on
    SQLite the batches do not merely contend, they fail: measured as nine of
    ten batches lost to lock errors on the dev database.

    SQLite is the dev and test backend, so this is not a production
    constraint; it is the difference between a pipeline that can be exercised
    locally and one that only works after deployment. Concurrency here is a
    latency optimisation, never a correctness requirement, so giving it up on
    a backend that cannot take it costs wall-clock time and nothing else.

    It also makes the pipeline usable from Django's ``TestCase``, which wraps
    each test in an uncommitted transaction that a separate thread's
    connection cannot see.
    """
    try:
        from django.db import connection
        return 1 if connection.vendor == "sqlite" else 32
    except Exception:
        return 1


def configured_research_concurrency():
    """What the administrator asked for, before the database clamp.

    Separate from :func:`research_concurrency` so a diagnostic can report both
    and explain the difference. An operator who set 5 and observes batches
    running one at a time should be told the database is the reason, not left
    to infer it.
    """
    return _int("profile_research_concurrency",
                DEFAULT_RESEARCH_CONCURRENCY, maximum=10)


def research_concurrency():
    """How many research batches actually run at once.

    The real ceiling on outbound request volume, and the main cost/latency
    trade: the ten batches are independent, so this is pure wall-clock saving
    up to whatever the provider's per-minute quota tolerates. Capped at 10
    because there are only ten batches — a higher number buys nothing and
    would only invite a rate-limit storm — and clamped by what the database
    can actually support.
    """
    return min(configured_research_concurrency(), max_parallelism())


def document_model_enabled():
    """Global switch for reading documents with a model.

    Default ON. It degrades to native-only extraction with a recorded reason
    whenever the model cannot read a file — no key, rate limited, file too
    large — so leaving it on never costs a document its native text. Turn it
    off to skip the token cost outright, at the price of everything that
    exists only as pixels: scanned pages, and the numbers inside deck images.

    The stored flag keeps its `profile_ocr_enabled` name. It is the same
    switch answering the same question — "read what a text extractor cannot
    reach?" — and renaming the column would migrate live configuration to say
    something it already said.
    """
    return bool(get_flag("profile_ocr_enabled", True))


def document_timeout_s():
    """Per-file deadline for the document read. Native extraction is
    unaffected."""
    return _int("profile_document_timeout_s", DEFAULT_DOCUMENT_TIMEOUT_S,
                minimum=30)


def research_config_profile():
    """LLMConfigProfile code for the research batches (search ON)."""
    return _text("profile_research_config_profile", DEFAULT_RESEARCH_PROFILE)


def synthesis_config_profile():
    """LLMConfigProfile code for the synthesis call (search OFF)."""
    return _text("profile_synthesis_config_profile", DEFAULT_SYNTHESIS_PROFILE)


def max_dossier_chars():
    """Ceiling on the dossier text handed to synthesis.

    Enforced in :func:`fundos.profile.pipeline.synthesize.run`, not merely
    documented — a cap nothing reads is worse than no cap, because it
    advertises a bound the system does not actually have.

    Generous by default (gemini-2.5-flash takes a 1M-token input window), so it
    is a runaway guard rather than a routine truncation.
    """
    return _int("profile_max_dossier_chars", 600_000, minimum=10_000)
