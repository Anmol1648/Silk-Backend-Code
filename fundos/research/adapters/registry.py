"""Adapter registry keyed by source_type, filtered by research_source_flags
and the extensions feature flag."""
from fundos.research.adapters.extensions import (
    AwardsAdapter, HiringAdapter, NewsAdapter, PatentsAdapter,
)
from fundos.research.adapters.linkedin import LinkedInAdapter
from fundos.research.adapters.market import (
    CompetitorAdapter, IndustryAdapter, MarketAdapter, PublicFundingAdapter,
)
from fundos.research.adapters.website import WebsiteAdapter
from fundos.research.models import CORE_SOURCES, EXTENSION_SOURCES

ADAPTERS = {
    "website": WebsiteAdapter,
    "linkedin": LinkedInAdapter,
    "market": MarketAdapter,
    "competitor": CompetitorAdapter,
    "industry": IndustryAdapter,
    "public_funding": PublicFundingAdapter,
    "patents": PatentsAdapter,
    "news": NewsAdapter,
    "awards": AwardsAdapter,
    "hiring": HiringAdapter,
}


# Client-facing aliases → canonical adapter keys. QA BUG-003: the UI sent
# "funding" but the adapter is registered as "public_funding", so the
# source silently vanished from every run.
SOURCE_ALIASES = {
    "funding": "public_funding",
    "funding_history": "public_funding",
}


def normalise_sources(requested):
    if not requested:
        return requested
    return [SOURCE_ALIASES.get(str(s).strip().lower(), str(s).strip().lower())
            for s in requested]


def enabled_sources(requested=None):
    """Resolve the runnable source set: per-adapter DB flags ∩ feature flag ∩
    (optional) request subset. Legal gating: enabled AND legal_cleared."""
    from fundos.config.feature_flags import enable_research_extensions
    from fundos.config.models import ResearchSourceFlags

    requested = normalise_sources(requested)
    flags = {f.source_type: f for f in ResearchSourceFlags.objects.all()}
    result = []
    universe = list(CORE_SOURCES) + (
        list(EXTENSION_SOURCES) if enable_research_extensions() else [])
    for source in universe:
        if requested and source not in requested:
            continue
        flag = flags.get(source)
        if flag is not None and not flag.enabled:
            continue
        if source == "linkedin" and (flag is None or not flag.legal_cleared):
            continue  # BR-M1-003
        result.append(source)
    return result
