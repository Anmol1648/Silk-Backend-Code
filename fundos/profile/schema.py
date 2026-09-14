"""The 17-section company profile schema (spec sections 8.1 – 8.17).

This module is the single definition of the profile's output contract. Every
consumer of a generated profile — the synthesis prompt, the response
normalizer, the API serializer — reads its shape from here, so a field never
needs to be defined and kept in sync in more than one place. It is used three
ways:

1. :func:`schema_prompt_block` renders the field list into the synthesis prompt.
2. :func:`empty_profile` produces a fully-keyed skeleton with the
   ``{sectionKey, isComplete, lastUpdatedAt, data}`` envelope.
3. :func:`normalize_profile` coerces whatever the model returned into that
   envelope so downstream consumers always see the same shape.

Design decision: schema-as-data
-------------------------------
Sections are plain data, not typed models. Deliberate: the schema must be
simultaneously (a) renderable as human-readable prompt text field-by-field and
(b) tolerant of whatever loosely-typed JSON an LLM actually returns, which a
strict typed model would reject outright rather than coerce. Plain data makes
both straightforward without fighting a validation layer that assumes
well-formed input.

Where the schema lives
----------------------
The authoritative definition is ``platformcfg.ProfileSectionConfig`` — one row
per section carrying ``container_kind``, ``spec_ref``, ``field_spec`` and
``storage_key`` — seeded from :data:`SHIPPED_SECTIONS` below. Because
:func:`schema_prompt_block` renders from those rows, adding a field to a
section is an admin edit: the prompt asks for it and the normalizer accepts it
on the next run, with no deploy.

:data:`SHIPPED_SECTIONS` stays as the seed *and* the runtime fallback, matching
how prompts and the research bank behave — an unseeded or unmigrated install
must still produce a profile.

Every section shares one envelope regardless of its own shape::

    {"sectionKey": …, "isComplete": bool, "lastUpdatedAt": …, "data": …}

``data`` is an object or an array of objects per the section's kind. The
uniform envelope is what lets a client render all 17 sections generically
rather than needing bespoke handling for each.
"""
import logging
import re

from django.utils import timezone

from fundos.profile import sanitize

logger = logging.getLogger("fundos.profile")

# Each section: wire key, storage key, spec reference, container kind, fields.
#
# `storage_key` is the section key this profile's data is actually stored
# under. It differs from the wire key for five sections whose internal names
# predate this contract; keeping the storage names means no data migration and
# no rewrite of the record tables behind them.
SHIPPED_SECTIONS = [
    {
        "key": "company_profile",
        "storage_key": "company_overview",
        "ref": "8.1 Company Overview",
        "kind": "object",
        "fields": {
            "description_of_business": "string - 2-4 paragraph investment-grade business description",
            "website": "string - primary website URL",
            "country": "string - ISO-2 country code of headquarters, e.g. IN, US, SG",
            "macro_sector": "string - e.g. Technology, Healthcare, Financial Services",
            # The examples here used to be "SaaS, Digital Health, Payments".
            # Only one of those three is a real benchmark group, so the
            # guidance was actively steering the model off the list it has to
            # hit. The list itself is appended to the prompt by
            # `taxonomy_block()`, because it is loaded from the benchmark
            # table rather than written down twice.
            "sub_sector": ("string - MUST be copied exactly from the "
                           "SUB-SECTOR list given below; it selects the peer "
                           "group this company is benchmarked against"),
            "funding_status_name": (
                "string - one of: Bootstrapped, Private, Angel Funded, Seed Funded, "
                "VC Funded, Private Equity Funded, Corporate Backed, Public, Acquired, "
                "Subsidiary/Group Owned"),
            "revenue_size_name": (
                "string - one of: < USD 100k, USD 100k to 1M, USD 1M to 5M, "
                "USD 5M to 10M, USD 10M to 25M, > USD 25M"),
            "currency_id": "string - ISO currency code of the HQ country, e.g. INR, USD",
            "total_funding_raised_usd_mn": "number|null - total raised, in USD millions",
            "last_funding_round_date": "string|null - YYYY-MM-DD",
            "latest_pre_money_usd_mn": "number|null - USD millions",
            "latest_post_money_usd_mn": "number|null - USD millions",
        },
    },
    {
        "key": "founders",
        "storage_key": "founders",
        "ref": "8.2 Founders & Key People",
        "kind": "array",
        "fields": {
            "name": "string - full name",
            "role": "string - title at the target company",
            "background": "string - 2-3 sentences: prior companies, education, achievements",
            "linkedin_url": "string - verified LinkedIn profile URL, empty string if not found",
            "is_full_time": "boolean - true if working on this company full-time",
            "is_founder": "boolean - true for founders, false for other key people",
        },
    },
    {
        "key": "products_services",
        "storage_key": "products_services",
        "ref": "8.3 Products & Services",
        "kind": "array",
        "fields": {
            "name": "string - product or service name",
            "category": "string - product line or category",
            "description": "string - what it does and who it is for",
        },
    },
    {
        "key": "customers_markets",
        "storage_key": "customers_markets",
        "ref": "8.4 Customers & Markets",
        "kind": "array",
        "fields": {
            "market": "string - market or vertical served",
            "customer_type": "string - e.g. Enterprise, SMB, D2C consumers, Government",
            "geography": "string - city, region or country",
        },
    },
    {
        "key": "competitive_advantages",
        "storage_key": "competitive_advantages",
        "ref": "8.5 Competitive Advantages",
        "kind": "array",
        "fields": {
            "title": "string - short label for the advantage",
            "description": "string - why it is defensible, with evidence",
        },
    },
    {
        "key": "business_model",
        "storage_key": "business_model",
        "ref": "8.6 Business Model",
        "kind": "object",
        "fields": {
            "business_model_types": "array of strings - e.g. B2B SaaS, Marketplace, D2C",
            "customer_type": "string - primary customer type",
            "value_proposition": "string - core value proposition",
            "delivery_model": "string - how the product reaches the customer",
            "pricing_model": "string - e.g. subscription, usage-based, commission",
            "sales_model": "string - e.g. inside sales, self-serve, enterprise field sales",
            "distribution_channels": "array of strings - channels used to reach customers",
        },
    },
    {
        "key": "revenue_model",
        "storage_key": "revenue_model",
        "ref": "8.7 Revenue Model",
        "kind": "array",
        "fields": {
            "stream": "string - revenue stream name",
            # NULL AND ZERO ARE DIFFERENT ANSWERS. A source that names the
            # revenue streams without breaking the split down has said
            # nothing about the shares, and every stream reported at 0%
            # reads as a business earning nothing from any of them.
            "share_percent": (
                "number|null - percentage of total revenue. Give a number "
                "ONLY if a source states or clearly implies the split; the "
                "stated shares should sum to ~100. Use null when the split "
                "is not given -- 0 means a stream that genuinely earns "
                "nothing, which is a claim, not a blank"),
        },
    },
    {
        "key": "company_metrics",
        "storage_key": "company_metrics",
        "ref": "8.8 Company Metrics",
        "kind": "array",
        "fields": {
            "metric": "string - KPI name, e.g. ARR, Active Customers, Net Retention",
            "value": "string - the value as reported",
            "unit": "string - unit of the value, e.g. USD mn, %, customers",
        },
    },
    {
        "key": "financial_summary",
        "storage_key": "financial_summary",
        "ref": "8.9 Financial Summary",
        "kind": "object",
        "fields": {
            # REPORT THE FIGURES AS THE SOURCE STATES THEM.
            #
            # `revenue_m` meant "millions of `currency`" and nothing enforced
            # it. A run converted INR 56.9 crore to 6.828 USD millions and
            # then labelled the row `"currency": "INR"`. Read as written,
            # FY2031's 415.8 means 415.8 million rupees -- about $5M. It
            # means $415.8M. An 83x misread on the row a reader cares most
            # about, and the exchange rate that produced it was invented in a
            # sentence and discarded.
            #
            # `denomination` is what closes it: a company reporting in crore
            # can now say so instead of being forced through a conversion to
            # fit the field name.
            "financials": (
                "array of objects with: financial_year (string, e.g. "
                "'FY 2024'), is_estimate (boolean), revenue (number|null), "
                "ebitda (number|null), pat (number|null), currency (string "
                "ISO code of THESE figures: INR, USD, EUR, GBP), "
                "denomination (string - the scale as written: Cr (crore), L "
                "(lakh), Mn, Bn, or \"\" for whole units), "
                "yoy_revenue_growth_pct (number|null), "
                "ev_revenue_multiple (number|null). NEVER convert between "
                "currencies -- state the figure and the currency the source "
                "used"),
            "observations": "array of strings - concise financial observations",
        },
    },
    {
        "key": "funding_history",
        "storage_key": "funding_history",
        "ref": "8.10 Funding History",
        "kind": "array",
        "fields": {
            "date": "string - YYYY-MM-DD or YYYY-MM if the day is unknown",
            "round": "string - e.g. Seed, Series A, Series C",
            # Report the figure AS THE SOURCE STATES IT. A deck saying
            # "INR 20 crore" is amount 20, currency INR, denomination Cr --
            # not 2.4. Converting before writing is what discarded the deck's
            # own words, buried the rate inside a sentence, and rounded 12
            # crore to $1.6M when it is $1.45M.
            "amount": ("number|null - the figure exactly as the source "
                       "states it, with no conversion"),
            "currency": ("string - ISO code of THAT figure: INR, USD, EUR, "
                         "GBP"),
            "denomination": ("string - the scale as written: Cr (crore), L "
                             "(lakh), K, Mn, Bn, or \"\" for whole units"),
            "pre_money": "number|null - as stated, same currency rules",
            "post_money": "number|null - as stated, same currency rules",
            "valuation_currency": "string - ISO code for the two above",
            "valuation_denomination": "string - scale for the two above",
            "investors": "array of strings - all participating investors",
            "lead_investors": "array of strings - lead investors only",
        },
    },
    {
        "key": "competitors",
        "storage_key": "competitors",
        "ref": "8.11 Competitors & Market Positioning",
        "kind": "array",
        "fields": {
            "name": "string - competitor name",
            # Emitted by the serializer and writable through the records
            # endpoint since CR-12, and never actually asked for — so every
            # competitor on a live profile came back with both empty. CR-12's
            # own reasoning is why that matters: without a plain description of
            # what a competitor does, neither a reader nor a later model can
            # judge whether a listed company is a genuine comparable.
            "description": "string - what this company does, in one or two sentences",
            "website": "string - the competitor's primary website URL",
            "fy_year": "number|null - financial year the revenue figure refers to",
            "revenue": "number|null - revenue in USD millions",
            "funding_usd_mn": "number|null - total funding raised, USD millions",
            "status": "string - e.g. Private, Public, Acquired",
            "investors": "array of strings",
            "business_model": "string",
            "market_positioning": "string",
            "latest_valuation_usd_mn": "number|null",
            "revenue_growth_pct": "number|null",
            "market_share_pct": "number|null",
            "relative_scale": "string - e.g. Larger, Comparable, Smaller vs the target",
            "key_differentiators": "array of strings",
            "strengths": "array of strings",
            "weaknesses": "array of strings",
            "ev_revenue_multiple": "number|null",
            "ev_ebitda_multiple": "number|null",
            "recent_activity": (
                "array of objects with: activity_type (string), date (string), "
                "investors_or_acquirers (array of strings), target_company (string), "
                "deal_value_usd_mn (number|null), implied_valuation_multiple (number|null)"),
        },
    },
    {
        "key": "news",
        "storage_key": "recent_news",
        "ref": "8.12 Recent News & Media",
        "kind": "array",
        "fields": {
            "title": "string - headline",
            "date": "string - YYYY-MM-DD",
            "description": "string - 1-2 sentence summary",
            "source": "string - publication name",
            "link": "string - full URL to the article",
        },
    },
    {
        "key": "investors_cap_table",
        "storage_key": "cap_table",
        "ref": "8.13 Investors & Cap Table",
        "kind": "object",
        "fields": {
            "cap_table_summary": (
                "object with: total_investors (number|null), ownership (array of objects "
                "with stakeholder_category (string) and ownership_pct (number))"),
            "investors_list": (
                "array of objects with: investor_name (string), investor_type (string), "
                "funding_amount_usd_mn (number|null), valuation_usd_mn (number|null), "
                "dilution_pct (number|null), date (string), round (string)"),
        },
    },
    {
        "key": "company_story",
        "storage_key": "company_story",
        "ref": "8.14 Company Story & USP",
        "kind": "object",
        "fields": {
            "origin_story": "string - founding narrative",
            "brand_evolution": "string - how the brand/positioning evolved",
            "usp": "string - unique selling proposition",
            "milestones": (
                "array of objects with: date (string), title (string), description (string)"),
        },
    },
    {
        "key": "industry_research",
        "storage_key": "market_research",
        "ref": "8.15 Industry & Market Research",
        "kind": "object",
        "fields": {
            "industry_evolution": "string - how the industry has developed",
            "market_sizing": (
                "object with: tam (string), sam (string), som (string) - each including "
                "the figure, currency and source year"),
            "performance_trends": "array of strings - performance and technology trends",
            "regulatory_developments": "array of strings",
        },
    },
    {
        "key": "investment_thesis",
        "storage_key": "investment_thesis",
        "ref": "8.16 Investment Thesis",
        "kind": "object",
        "fields": {
            "opportunity_explanation": "string - the core investment rationale",
            "leadership_assessment": "string - assessment of the founding/leadership team",
            "risks_and_concerns": "array of strings - key risks and diligence concerns",
        },
    },
    {
        "key": "document_center",
        "storage_key": "document_center",
        "ref": "8.17 Document Center",
        "kind": "object",
        "fields": {
            "documents": (
                "array of objects with: id (string), filename (string), category (string), "
                "status (string), handler (string)"),
        },
    },
]

SHIPPED_BY_KEY = {section["key"]: section for section in SHIPPED_SECTIONS}

# Sections the model must not invent — the pipeline fills them from its own
# records. Currently just the Document Center: the model is never asked to
# produce it (it is filtered out of the prompt), and
# :func:`apply_document_center` overwrites it from the run's own extraction
# results regardless of what came back. So this set doubles as "exclude from
# the prompt" and "always overwrite after normalization".
PIPELINE_OWNED_SECTIONS = {"document_center"}


def _now():
    return timezone.now().isoformat()


# --- schema resolution ------------------------------------------------------


def sections():
    """The active section definitions, admin rows first.

    Falls back to :data:`SHIPPED_SECTIONS` when the config table is unseeded or
    unmigrated. A row missing ``field_spec`` inherits the shipped spec for that
    key rather than contributing an empty section to the prompt — a
    half-configured row should degrade to the shipped behaviour, not silently
    ask the model for nothing.
    """
    try:
        from fundos.platformcfg.models import ProfileSectionConfig
        rows = list(ProfileSectionConfig.objects.filter(is_active=True)
                    .order_by("sort_order", "section_key"))
    except Exception as exc:
        logger.info("PIPELINE: section config unavailable (%s) — using the "
                    "shipped schema.", exc)
        return list(SHIPPED_SECTIONS)

    by_storage = {s["storage_key"]: s for s in SHIPPED_SECTIONS}

    resolved = []
    for row in rows:
        # A config row is keyed by its STORAGE key, which for five sections
        # differs from the wire key the contract uses.
        shipped = (SHIPPED_BY_KEY.get(row.section_key)
                   or by_storage.get(row.section_key) or {})
        spec = getattr(row, "field_spec", None) or shipped.get("fields") or {}
        if not spec:
            # Not necessarily a fault. ProfileSectionConfig also carries
            # sections other subsystems own and generation never writes —
            # readiness, the knowledge base — and those legitimately have no
            # field spec. Only a section the contract KNOWS about but cannot
            # resolve a spec for is worth a warning; the rest are simply not
            # part of the generated profile.
            if row.section_key in SHIPPED_BY_KEY or \
                    row.section_key in by_storage:
                logger.warning("PIPELINE: section %r is in the profile "
                               "contract but has no field spec; skipped.",
                               row.section_key)
            continue
        resolved.append({
            # The contract speaks WIRE keys. A row keyed by its storage name
            # still contributes its wire name, so renaming a section on the
            # API surface never required renaming it in storage.
            "key": shipped.get("key") or row.section_key,
            "storage_key": (getattr(row, "storage_key", "") or
                            shipped.get("storage_key") or row.section_key),
            "ref": (getattr(row, "spec_ref", "") or shipped.get("ref") or
                    row.label),
            "kind": (getattr(row, "container_kind", "") or
                     shipped.get("kind") or "object"),
            "fields": spec,
        })
    return resolved or list(SHIPPED_SECTIONS)


def section_keys():
    return [section["key"] for section in sections()]


def sections_by_key():
    return {section["key"]: section for section in sections()}


def storage_key_for(wire_key):
    """Wire section key -> the key the section is actually stored under."""
    section = sections_by_key().get(wire_key)
    if section:
        return section["storage_key"]
    shipped = SHIPPED_BY_KEY.get(wire_key)
    return shipped["storage_key"] if shipped else wire_key


def wire_key_for(storage_key):
    """Storage key -> wire key. The inverse of :func:`storage_key_for`."""
    for section in sections():
        if section["storage_key"] == storage_key:
            return section["key"]
    return storage_key


# --- prompt rendering -------------------------------------------------------


#: What the model is told about citing its work.
#:
#: Asked of synthesis rather than reconstructed afterwards, because synthesis
#: is the only party that knows. It has the dossier in front of it and writes
#: the value; nothing downstream can recover which of forty pages a sentence
#: came from without guessing, and a guessed citation is worse than none — it
#: survives review by looking exactly like a real one.
#:
#: The labels it may use are the dossier's own headings, so a citation names
#: something a reader can actually turn to.
SOURCES_INSTRUCTION = """
### sources  (required alongside every section)

For each section you fill, also return a `sources` object beside its `data`,
mapping each field you filled to where you read it:

    "company_profile": {
      "data": { "website": "https://example.com" },
      "sources": {
        "website": {
          "source": "TyrePlex Investor Deck.pdf",
          "locator": "Page 3",
          "quote": "www.example.com"
        }
      }
    }

For an ARRAY section, key `sources` by the item's position as a string --
"0", "1", "2" -- matching the order you return the items in.

RULES:
  * `source` MUST be copied EXACTLY from the list of sources below. Not a
    description of it, not the channel it arrived through -- the string
    itself, character for character. A `source` that is not on that list is
    discarded, and the value loses its citation.
  * PREFER AN UPLOADED DOCUMENT over web research whenever both say the same
    thing. The company's own deck and financial model are what a reader
    trusts; a search result repeating the same fact is weaker evidence for
    exactly the same claim.
  * CHECK THE COMPANY'S UPLOADED FILES FIRST, for every value. Web research is
    written as ready-made answers and is easier to quote -- that is not a
    reason to cite it. If a document states the value (a name on a slide, a
    figure in a sheet), cite THE DOCUMENT, with its slide, page or sheet as
    the locator. You may add the web research as a second source. Cite web
    research ALONE only for what no uploaded document states.
  * When the value came from what the COMPANY ITSELF supplied -- a founder
    named in their own records, their website -- cite that, even though those
    records are unverified. For "who is a founder here", the company saying so
    is the primary evidence; research is what you cite for what you then
    LEARNED about that person.
  * `locator` is where inside that source you read it, in the form the
    dossier itself uses: "Page 12" or "Slide 4" for a document, the sheet
    name for a spreadsheet, "" when the dossier states no position.
  * `quote` is a SHORT verbatim extract from the dossier that supports the
    value -- never a paraphrase, never your own words. Under 200 characters.
  * If the extract itself contains a double-quote character, use a SINGLE
    quote in its place. An unescaped `"` inside a value ends the string
    early and breaks the whole response, not just that one citation.
  * If you cannot point to a specific place in the dossier for a field, leave
    that field out of `sources` entirely. An omitted citation is honest; an
    invented one is not.
"""


def sources_block(labels, document_count=0):
    """The citation instruction, with the sources it is allowed to name.

    The list is the point. Told only to "name a dossier heading", the model
    wrote the channel instead -- 64 citations reading "Web Research
    (search-grounded)" for a company whose investor deck was sitting in the
    dossier, and one of them spelled "search-groundd", which is what writing
    from memory looks like. A model cannot reliably copy a string it has not
    been shown; given the strings, it can.
    """
    if not labels:
        return SOURCES_INSTRUCTION

    # Which labels are the uploaded documents is stated, not left implied by
    # the ordering. "Prefer an uploaded document" is not an instruction the
    # model can follow against a flat list it cannot tell apart.
    from fundos.profile.pipeline.dossier import FOUNDER_SOURCE_LABEL

    documents = list(labels[:document_count])
    research = [label for label in labels[document_count:]
                if label != FOUNDER_SOURCE_LABEL]
    parts = ["\nTHE ONLY VALID VALUES FOR `source` -- copy one exactly:\n"]
    if FOUNDER_SOURCE_LABEL in labels:
        # First, because it outranks both: for a name the company typed into
        # its own onboarding form, no research result is better evidence.
        parts.append("THE COMPANY ITSELF (use for a value THEY supplied — a "
                     "founder's name, the website):")
        parts.append(f"  - {FOUNDER_SOURCE_LABEL}")
        parts.append("")
    if documents:
        parts.append("UPLOADED DOCUMENTS (prefer these):")
        parts.extend(f"  - {label}" for label in documents)
        parts.append("")
    if research:
        parts.append("WEB RESEARCH (use when no document says it):"
                     if documents else "SOURCES:")
        parts.extend(f"  - {label}" for label in research)
        parts.append("")
    return SOURCES_INSTRUCTION + "\n".join(parts)


#: Punctuation inside a filename that a model routinely retypes as a space.
#: `Project Orah_Financial Model_vf.xlsx` comes back as "Project Orah
#: Financial Model" often enough that treating the two as different sources
#: drops a real citation for a typographic difference.
_LABEL_PUNCTUATION = str.maketrans({"_": " ", "-": " ", "–": " ",
                                    "—": " "})


def _normalise_label(text):
    """Both sides of a citation match: lowercased, depunctuated, collapsed.

    Underscores and dashes become spaces because a filename is frequently
    quoted back with them swapped, and a citation dropped over an underscore
    is a real source lost to typography. The WORDS still have to match --
    this makes the comparison forgiving about punctuation, not about content.
    """
    cleaned = str(text or "").translate(_LABEL_PUNCTUATION)
    return " ".join(cleaned.split()).casefold()


def canonical_source(claimed, labels):
    """The dossier heading a claimed source refers to, or "" if none.

    Exact first, then containment either way, so "Web Research, Batch 3:
    Products, Services & Business Model" still resolves to the batch it
    names. Longest match wins: "Batch 1" must not swallow "Batch 10".

    A claim that is only a PART of several labels names none of them:
    "Project Orah" is inside both the deck and the financial model, and
    picking the longer one attached a slide quote to a spreadsheet.
    """
    claim = _normalise_label(claimed)
    if not claim or not labels:
        return ""
    best = ""
    partial = []
    for label in labels:
        norm = _normalise_label(label)
        if not norm:
            continue
        if norm == claim:
            return label
        if norm in claim and len(norm) > len(_normalise_label(best)):
            best = label
        elif claim in norm:
            partial.append(label)
    if best:
        return best
    if len(partial) == 1:
        return partial[0]
    if partial:
        return ""
    return _source_by_words(claim, labels)


#: Words that say what KIND of thing a source is, not WHICH one. Two labels
#: sharing only these ("research", "batch") are not the same source.
_GENERIC_SOURCE_WORDS = {
    "a", "an", "and", "the", "of", "for", "on", "in", "to", "vs", "v",
    "batch", "web", "research", "search", "grounded", "source", "sources",
    "document", "documents", "file", "company", "uploaded", "report",
    "pdf", "pptx", "ppt", "xlsx", "xls", "docx", "doc", "csv", "md",
}


def _source_words(norm):
    """The distinguishing words of a normalised label: no digits, no kinds."""
    words = set()
    for word in norm.replace("&", " and ").replace(".", " ").split():
        word = word.strip(",:;()[]'\"")
        if len(word) < 2 or word.isdigit() or word in _GENERIC_SOURCE_WORDS:
            continue
        words.add(word)
    return words


def _source_by_words(claim, labels):
    """The one label whose distinguishing words the claim shares, or "".

    The model often names a source in its own words rather than copying it:
    "Orah teaser deck" for "Project Orah Teaser_vff.pptx", "Founders and
    Leadership research" for "Batch 2: Founders & Leadership". Containment
    misses both, and each citation was dropped.

    Accepted only when it is unambiguous: at least two distinguishing words
    shared, at least two-thirds of the claim's distinguishing words found in
    the label, and no other label scoring as well. A claim naming a site the
    dossier does not list ("LinkedIn") shares nothing and is still dropped --
    attaching it to some batch would invent a source the model never named.
    """
    claim_words = _source_words(claim)
    if len(claim_words) < 2:
        return ""
    scored = []
    for label in labels:
        shared = claim_words & _source_words(_normalise_label(label))
        if len(shared) >= 2 and len(shared) * 3 >= len(claim_words) * 2:
            scored.append((len(shared), label))
    if not scored:
        return ""
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return ""
    return scored[0][1]


#: A canonical group must be at least this long to be recognised inside a
#: longer claim. Short fragments match too much to be trusted as evidence
#: that a company belongs to a cohort.
_MIN_SUB_SECTOR_MATCH = 6


def sub_sector_vocabulary():
    """The sub-sectors that resolve to a benchmark cohort, or [] if none load.

    This is not a style guide, it is the join key. `sub_sector` is matched
    against the benchmark table to pick the peer cohort a company is scored
    against, and a value that misses matches nothing:

        'B2B E-commerce platform'  ->  unresolved, 3 benchmarks
        'B2B Ecommerce'            ->  exact,      6 benchmarks

    Tyreplex got the first. Its own run said so — `sub_sector_method:
    'unresolved'` beside a warning that Category F carries 20% of the rating
    and would score blank — while the prompt offered "e.g. SaaS, Digital
    Health, Payments" as guidance, none of which are in the table either.

    A model cannot reliably produce a value it has not been shown, and this
    one is worth a fifth of a rating.
    """
    try:
        from fundos.assessment.models import SectorDealData
        return sorted({row.clubbed_group
                       for row in SectorDealData.objects.all()
                       if (row.clubbed_group or "").strip()
                       and row.clubbed_group != "Unassigned"})
    except Exception:                       # pragma: no cover - never fatal
        return []


def taxonomy_block():
    """The sub-sector list, for the prompt. "" when nothing is loaded."""
    vocabulary = sub_sector_vocabulary()
    if not vocabulary:
        return ""
    listed = "\n".join(f"  - {name}" for name in vocabulary)
    return (
        "\n\nSUB-SECTOR — `company_profile.sub_sector` MUST be copied "
        "EXACTLY from this list.\nIt selects the peer group this company is "
        "benchmarked against; a value that is not on the list matches no "
        "peers and the whole benchmark category scores blank. Choose the "
        "closest one rather than inventing a more precise label.\n"
        + listed + "\n")


#: The figures a financial row carries, and the legacy key each was written
#: under. The old names encoded the scale -- `_m` for millions -- which is
#: exactly the assumption that let a converted figure be labelled with the
#: currency it was converted FROM.
_FINANCIAL_FIGURES = (("revenue", "revenue_m"),
                      ("ebitda", "ebitda_m"),
                      ("pat", "pat_m"))


def _normalise_financial_rows(data, notes=None):
    """Give every financial row its currency and its scale, as reported.

    A run converted INR 56.9 crore to 6.828 USD millions and then wrote
    `"currency": "INR"` beside it. Read as written, FY2031's 415.8 means 415.8
    million rupees -- about $5M. It means $415.8M: an 83x misread on the row a
    reader cares most about, and the rate that produced it was invented in a
    sentence and discarded.

    Both shapes are accepted, because existing profiles hold the old one. A
    legacy row states no scale, so it is read as MILLIONS -- which is what
    `revenue_m` always meant. Reading that blank as whole units would turn
    US$6.8M into US$0.0M, the same defect the funding-history migration had
    to correct.

    Never converts. The USD companion is derived beside the reported figure,
    with the rate recorded, so a wrong rate is a correctable error rather
    than a permanent one.
    """
    from fundos.core.services import money

    rows = data.get("financials")
    if not isinstance(rows, list):
        return

    converted = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        currency = (money.normalise_currency(row.get("currency"))
                    or str(row.get("currency") or "").upper())
        stated = money.normalise_denomination(row.get("denomination"))
        legacy = not row.get("denomination") and any(
            row.get(old) is not None for _new, old in _FINANCIAL_FIGURES)
        denomination = stated or ("Mn" if legacy else "")

        row["currency"] = currency
        row["denomination"] = denomination
        for new_key, old_key in _FINANCIAL_FIGURES:
            value = row.get(new_key)
            if value is None:
                value = row.get(old_key)
            row[new_key] = value
            # The `_m` keys stay populated for every existing consumer. They
            # now mean what they always claimed: millions of `currency`.
            row[old_key] = value
            row[f"{new_key}_display"] = money.display(value, currency,
                                                      denomination)
            usd, rate = money.to_usd_mn(value, currency, denomination)
            row[f"{new_key}_usd_mn"] = usd
            if rate is not None:
                row["fx_rate"] = rate
        converted += 1

    if converted and notes is not None:
        currencies = sorted({str(r.get("currency") or "")
                             for r in rows if isinstance(r, dict)} - {""})
        if len(currencies) > 1:
            notes.append(
                f"financial_summary: rows state more than one currency "
                f"({', '.join(currencies)}) — a series is only comparable "
                f"within one")


def _canonicalise_sub_sector(data, notes=None):
    """Point `sub_sector` at a real benchmark group where one is recognisable.

    The model's own words are kept when nothing matches — an unresolved
    sub-sector is a visible gap, and overwriting it with a plausible-looking
    neighbour would benchmark the company against the wrong industry while
    looking correct.
    """
    claimed = str(data.get("sub_sector") or "").strip()
    if not claimed:
        return
    resolved = canonical_sub_sector(claimed)
    if not resolved:
        if notes is not None and sub_sector_vocabulary():
            notes.append(
                f"company_profile: sub-sector {claimed!r} matches no "
                f"benchmark group, so the benchmark category will score "
                f"blank")
        return
    if resolved != claimed:
        data["sub_sector"] = resolved
        if notes is not None:
            notes.append(f"company_profile: sub-sector {claimed!r} resolved "
                         f"to the benchmark group {resolved!r}")


def _mapped_sub_sector(claimed):
    """The group an administrator recorded for this label, or "".

    Reads the same `SectorMapping` table the assessment's resolver reads, so
    a label an administrator has explained resolves identically on both
    sides. Never fatal: a missing table means "no alias", not a failed run.
    """
    text = str(claimed or "").strip()
    if not text:
        return ""
    try:
        from fundos.assessment.models import SectorMapping

        row = SectorMapping.objects.filter(raw_label__iexact=text).first()
    except Exception as exc:                # pragma: no cover - never fatal
        logger.debug("SUB-SECTOR: alias lookup unavailable: %s", exc)
        return ""
    return (row.clubbed_group or "") if row else ""


def canonical_sub_sector(claimed):
    """The benchmark group a claimed sub-sector names, or "" if none.

    A second line of defence behind the prompt list. Exact match first, then
    containment either way, longest wins — so "B2B E-commerce platform"
    resolves to "B2B Ecommerce" and Category F keeps its peers instead of
    scoring blank over a hyphen and a trailing noun.

    Returns "" rather than a guess when nothing matches. An unresolved
    sub-sector is a visible gap; a wrongly resolved one silently benchmarks a
    company against the wrong industry, which is worse.
    """
    claim = _normalise_label(str(claimed or "").replace("-", ""))
    if not claim:
        return ""

    # THE ALIAS TABLE FIRST, because the assessment reads it and this did
    # not. The same run logged "'Digital Health' matches no benchmark group,
    # so the benchmark category will score blank" here, and
    # `sub_sector_method="exact"` -> Healthtech there, a minute later. One
    # string, two answers, and the warning shown to the operator was the
    # false one. `SectorMapping` is where an administrator records that a
    # label means a group; consulting it in one resolver and not the other
    # guarantees they disagree.
    mapped = _mapped_sub_sector(claimed)
    if mapped:
        return mapped

    best = ""
    for name in sub_sector_vocabulary():
        norm = _normalise_label(name.replace("-", ""))
        if not norm:
            continue
        if norm == claim:
            return name
        # ONE DIRECTION ONLY: the claim may be more specific than the group
        # ("B2B E-commerce platform" contains "B2B Ecommerce"), never vaguer.
        # Matching the other way round resolved a company describing itself as
        # "SaaS" to "Travel Tech SaaS" — a real group, a real cohort, and the
        # wrong industry, benchmarked with every appearance of being right.
        # A vague claim stays unresolved, which is a visible gap.
        if (norm in claim and len(norm) >= _MIN_SUB_SECTOR_MATCH
                and len(norm) > len(_normalise_label(best.replace("-", "")))):
            best = name
    return best


def schema_prompt_block(active=None, source_labels=None, document_count=0):
    """Render the schema as prompt text for the synthesis call.

    Single-sourced from the section definitions, so the prompt the model sees
    and the shape :func:`normalize_profile` expects back cannot drift apart —
    there is no second, hand-maintained copy of the field list. Skips
    :data:`PIPELINE_OWNED_SECTIONS`: the model is never told the Document
    Center's shape, because it is never asked to produce it.
    """
    lines = []
    for section in (active if active is not None else sections()):
        if section["key"] in PIPELINE_OWNED_SECTIONS:
            continue
        container = ("data is an OBJECT with these fields"
                     if section["kind"] == "object"
                     else "data is an ARRAY of objects, each with these fields")
        lines.append(f'### sections["{section["key"]}"]  ({section["ref"]})')
        lines.append(f"{container}:")
        for name, description in section["fields"].items():
            lines.append(f"  - {name}: {description}")
        lines.append("")
    lines.append(sources_block(source_labels, document_count))
    lines.append(taxonomy_block())
    return "\n".join(lines).rstrip()


def promptable_keys(active=None):
    """The section keys the model is actually asked to produce."""
    return [s["key"] for s in (active if active is not None else sections())
            if s["key"] not in PIPELINE_OWNED_SECTIONS]


# --- normalization ----------------------------------------------------------


def _empty_data(section):
    """The zero value for one section's ``data``, matching its declared kind.

    Never ``None``: every consumer can assume the container type without a null
    check.
    """
    return [] if section["kind"] == "array" else {}


def empty_profile(active=None):
    """A fully-keyed skeleton with empty data for every section.

    This is the baseline :func:`normalize_profile` fills in over, which is what
    guarantees the final result always has every section key present even when
    the model's response omitted some entirely.
    """
    now = _now()
    return {
        "sections": {
            section["key"]: {
                "sectionKey": section["key"],
                "isComplete": False,
                "lastUpdatedAt": now,
                "data": _empty_data(section),
            }
            for section in (active if active is not None else sections())
        }
    }


def is_populated(data):
    """True when a section holds at least one non-empty value.

    Recurses through lists and dicts looking for any leaf that is not blank.
    This computes ``isComplete`` rather than trusting the model's own claim,
    which it can get wrong or omit. A section with the right shape but every
    field empty is correctly treated as unpopulated, which distinguishes "the
    model tried and found nothing" from "the model reported something" — the
    difference that tells a reader whether a thin profile reflects thin source
    material or a synthesis problem.
    """
    if isinstance(data, list):
        return any(is_populated(item) for item in data)
    if isinstance(data, dict):
        return any(is_populated(value) for value in data.values())
    if isinstance(data, str):
        return data.strip() != ""
    return data is not None and data is not False


def _coerce_fields(section, data, notes):
    """Type every declared field of one section against its own spec.

    The spec strings are the same text :func:`schema_prompt_block` renders into
    the prompt, so the type a field is asked for and the type it is stored as
    come from one declaration. Adding a field in Django admin therefore makes
    the prompt request it *and* makes this coerce it, with nothing to keep in
    step by hand.

    Fields the spec does not declare are kept but sanitized. Dropping them
    would discard data an operator may have started asking for in a spec this
    code has not seen; keeping them raw would let markup and unbounded strings
    past the boundary.

    A field the model omitted stays omitted. This types what came back; it does
    not invent a shape. Materialising every declared field as an empty value
    would look helpful and would lie twice over: downstream, a row of empty
    strings is not distinguishable from one the model deliberately left blank,
    and ``_replace_people`` reads a *missing* ``is_founder`` as True — so
    filling that in as False would file every unflagged person as a key person
    and empty the section a reader checks for the team. Clients get the field
    structure from the GET-path row templates in
    :mod:`fundos.profile.spec_serializer`, which is the layer that owes them a
    shape to render.
    """
    if not isinstance(data, dict):
        return data
    spec = section.get("fields") or {}
    out = {}
    for name, value in data.items():
        clean_name = sanitize.plain_text(name, limit=128)
        if not clean_name:
            continue
        if clean_name in spec:
            out[clean_name] = sanitize.coerce_to_spec(
                clean_name, spec[clean_name], value, notes=notes)
        else:
            out[clean_name] = sanitize.sanitize_json(value)
    return out


def coerce_section_data(wire_key, data, notes=None):
    """Type one section's payload against its declared field spec.

    The same coercion :func:`normalize_profile` applies, reachable for a single
    section. The full pipeline run is typed by the normalizer, but that is not
    the only door generated content comes through: single-section and
    field-level regeneration call the per-section roles directly, so without
    this they would write straight past the boundary.

    Returns ``data`` unchanged when the key names no section in the generated
    contract — an admin-added section with no field spec, or a legacy storage
    name. That is not a failure: not every stored section is part of the
    generated profile.
    """
    section = sections_by_key().get(wire_key)
    if section is None:
        return data
    if isinstance(data, list):
        return [_coerce_fields(section, row, notes)
                for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return _coerce_fields(section, data, notes)
    return data


def _one_citation(citation):
    """The single citation inside whatever the model wrote for one item.

    An object section is cited field by field, so for a LIST section a model
    reasonably does the same thing one level down: rather than one citation
    per founder it writes one per field OF that founder --

        "sources": {"0": {"name":       {"source": ..., "quote": ...},
                          "background": {"source": ..., "quote": ...}}}

    That is a sincere answer to the question asked, and it used to be thrown
    away whole: the outer dict has no `source` key, so every citation for
    every founder was dropped and the run reported only that "a sources block
    came back that could not be read". Tyreplex's five founders arrived cited
    and reached the reader with nothing.

    An item is confirmed as one part, and shows one provenance line, so one
    citation per item is what there is room for: take the first that names a
    source, preferring the one carrying a quote, since that is the one a
    reader can check.
    """
    # A LIST IS THE SAME ANSWER IN THE OTHER SHAPE. An object section that
    # contains an array -- `financial_summary.financials`,
    # `investors_cap_table.investors_list` -- is routinely cited one entry
    # per row:
    #
    #     "sources": {"financials": [{"source": ..., "quote": ...}, ...]}
    #
    # That is as sincere as the nested-dict form below, and it was dropped
    # whole for not being a dict: two sections came back cited and reached
    # the reader with no provenance at all, reported only as "a sources
    # block came back that could not be read".
    if isinstance(citation, list):
        citation = {str(i): item for i, item in enumerate(citation)}
    if not isinstance(citation, dict):
        return None
    if citation.get("source"):
        return citation
    nested = [inner for inner in citation.values()
              if isinstance(inner, dict) and inner.get("source")]
    if not nested:
        # One level deeper: a field whose citations are a list of rows, each
        # of which is itself keyed by field. Same rule, same reason.
        for inner in citation.values():
            deeper = _one_citation(inner) if isinstance(
                inner, (dict, list)) else None
            if deeper:
                return deeper
        return None
    for inner in nested:
        if inner.get("quote"):
            return inner
    return nested[0]


#: How much of a quote to keep. A citation quote is a short extract a reader
#: can check at a glance, not a passage.
QUOTE_LIMIT = 400


def _all_citations(citation):
    """Every citation inside whatever shape arrived, most useful first.

    One value can be evidenced by more than one source -- a founder named in
    the deck AND on a web profile -- and only the first was ever kept. The
    ones carrying a quote come first, because a quote is what a reader can
    check.
    """
    if isinstance(citation, dict) and citation.get("source"):
        return [citation]
    if isinstance(citation, list):
        pool = citation
    elif isinstance(citation, dict):
        pool = list(citation.values())
    else:
        return []

    found = []
    for item in pool:
        found.extend(_all_citations(item))

    seen, unique = set(), []
    for item in found:
        key = (str(item.get("source", "")).strip().casefold(),
               str(item.get("locator", "")).strip().casefold())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda c: 0 if c.get("quote") else 1)
    return unique


def _record_citation(out, rejected, field, citation, allowed):
    """Write one citation into the map, applying the same source check."""
    source = sanitize.plain_text(citation.get("source", ""), limit=300)
    if not source:
        return
    if allowed:
        source = canonical_source(source, allowed)
        if not source:
            rejected.append(field)
            return
    out[sanitize.plain_text(field, limit=128)] = {
        "source": source,
        "locator": sanitize.plain_text(citation.get("locator", ""),
                                       limit=120),
        "quote": sanitize.plain_text(citation.get("quote", ""),
                                     limit=QUOTE_LIMIT),
    }


def _coerce_sources(value, *, notes=None, key="", allowed=None):
    """Type the `sources` map a section came back with.

    Same discipline as every other field: whatever arrived is coerced rather
    than trusted, and anything unreadable as a citation is dropped. A
    malformed citation is not worth keeping — the value it describes is still
    fine, and an unreadable source block would surface as an empty chip that
    looks like evidence and is not.
    """
    if not isinstance(value, dict):
        return {}
    raw_sources = value.get("sources")
    if isinstance(raw_sources, list):
        # A list section asked for citations keyed by position often comes
        # back as a list in that same order instead. It says the same thing.
        raw_sources = {str(i): item for i, item in enumerate(raw_sources)}
    if not isinstance(raw_sources, dict):
        if raw_sources and notes is not None:
            notes.append(f"{key}: `sources` arrived as "
                         f"{type(raw_sources).__name__}, which is not a "
                         f"citation map — it was dropped")
        return {}

    out, rejected = {}, []
    for field, citation in raw_sources.items():
        # EVERY SOURCE, NOT THE FIRST ONE.
        #
        # A founder cited from both the deck and a web profile has two
        # sources, and collapsing them to one threw away half the evidence
        # for a value -- the reader saw one chip and had no way to know a
        # second existed. The extras are written under `field.<n>`, which
        # `_citations_for` already collects, so the storage shape and every
        # existing reader are untouched.
        extras = _all_citations(citation)
        for index, extra in enumerate(extras[1:], start=1):
            _record_citation(out, rejected, f"{field}.{index}", extra,
                             allowed)
        citation = extras[0] if extras else _one_citation(citation)
        if not isinstance(citation, dict):
            continue
        source = sanitize.plain_text(citation.get("source", ""), limit=300)
        if not source:
            continue        # a citation naming no source names nothing
        if allowed:
            # It must name something that is actually IN the dossier. The
            # model was handed the list; a value off it is a label written
            # from memory, and a citation nobody can turn to is worse than an
            # honest blank because it passes review looking like evidence.
            source = canonical_source(source, allowed)
            if not source:
                rejected.append(field)
                continue
        out[sanitize.plain_text(field, limit=128)] = {
            "source": source,
            "locator": sanitize.plain_text(citation.get("locator", ""),
                                           limit=120),
            "quote": sanitize.plain_text(citation.get("quote", ""),
                                         limit=QUOTE_LIMIT),
        }
    if rejected and notes is not None:
        notes.append(
            f"{key}: {len(rejected)} citation(s) named a source that is not "
            f"in the dossier and were dropped ({', '.join(rejected[:4])})")
    elif raw_sources and not out and notes is not None:
        notes.append(f"{key}: a sources block came back that could not be "
                     f"read as citations — it was dropped")
    return out


#: "13x", "13 X", "~2.5x" -- a multiple, stated on its own.
_MULTIPLE_RE = re.compile(
    r"^\s*(?:~|approx\.?|about|nearly|over)?\s*"
    r"(\d+(?:\.\d+)?)\s*(?:x|X|×)\s*$")

#: A metric whose value is a multiple of something measured over time. CAGR
#: is deliberately absent: it is a rate, and comparing it against a multiple
#: would flag every correctly-stated one.
_GROWTH_WORDS = ("growth", "grew", "increase", "multiple", "expansion",
                 "scaled", "growth multiple")

_YEAR_RE = re.compile(r"(?:FY)?\s*(\d{2,4})", re.I)


def _fiscal_year(text):
    """2024 from "FY 2024", "FY24", "2024". None when there is no year."""
    match = _YEAR_RE.search(str(text or ""))
    if not match:
        return None
    year = int(match.group(1))
    if year < 100:
        year += 2000
    return year if 1900 <= year <= 2100 else None


def _revenue_by_year(profile):
    """{year: revenue} from the financial rows, in the row's own currency.

    Only the ratio of two rows is ever taken, so the currency cancels --
    provided both rows state the same one, which is why a mixed-currency
    series is excluded rather than silently divided across currencies.
    """
    section = (profile.get("sections") or {}).get("financial_summary") or {}
    rows = (section.get("data") or {}).get("financials")
    if not isinstance(rows, list):
        return {}
    series, currencies = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("revenue")
        if value is None:
            value = row.get("revenue_m")
        year = _fiscal_year(row.get("financial_year"))
        if year is None or value is None:
            continue
        try:
            series[year] = float(value)
        except (TypeError, ValueError):
            continue
        currencies.add((row.get("currency") or "", row.get("denomination")
                        or ""))
    return series if len(currencies) <= 1 else {}


def _implied_multiple(series, years):
    """What the series itself says the growth multiple over `years` was."""
    known = sorted(y for y in series if series[y] is not None)
    if len(known) < 2:
        return None, None, None
    wanted = sorted(y for y in years if y in series)
    first, last = (wanted[0], wanted[-1]) if len(wanted) >= 2 else (known[0],
                                                                   known[-1])
    start, end = series.get(first), series.get(last)
    if not start or start <= 0 or end is None or last <= first:
        return None, None, None
    return round(end / start, 2), first, last


def _reconcile_metrics_with_series(profile, notes=None):
    """A growth multiple must agree with the revenue series beside it.

    One profile stated a 13x growth metric while its own revenue rows, over
    the same years, show about 7x. Both were on the same screen. The number
    that can be checked is the one that stays: the multiple is recomputed
    from the rows, and the figure the model asserted is reported rather than
    quietly replaced.

    Only a metric that IS a bare multiple is touched, and only when the rows
    can produce one to compare it against. A metric nothing can check is left
    exactly as it was -- withholding it would hide the company's own claim
    behind our inability to verify it.
    """
    section = (profile.get("sections") or {}).get("company_metrics") or {}
    rows = section.get("data")
    if not isinstance(rows, list):
        return
    series = _revenue_by_year(profile)
    if len(series) < 2:
        return

    for row in rows:
        if not isinstance(row, dict):
            continue
        match = _MULTIPLE_RE.match(str(row.get("value") or ""))
        label = str(row.get("metric") or "").lower()
        if not match or not any(word in label for word in _GROWTH_WORDS):
            continue
        stated = float(match.group(1))
        years = [y for y in (_fiscal_year(t) for t in
                             _YEAR_RE.findall(f"{label} {row.get('unit')}"))
                 if y is not None]
        implied, first, last = _implied_multiple(series, years)
        if implied is None:
            continue
        # Rounding, a restated year and an off-by-one period all move a
        # multiple a little; a fifth of the figure is past all of them.
        if abs(stated - implied) <= 0.2 * implied:
            continue
        row["value"] = f"{implied:g}x"
        if notes is not None:
            notes.append(
                f"company_metrics: {row.get('metric') or 'a growth metric'} "
                f"was stated as {stated:g}x, but the revenue rows for "
                f"FY{first}-FY{last} in this same profile imply {implied:g}x "
                f"— the figure derived from the rows is shown")


def normalize_profile(raw, active=None, notes=None, allowed_sources=None):
    """Coerce a model response into the canonical envelope.

    Accepts either the full ``{"sections": {...}}`` shape or a bare mapping of
    section keys, and tolerates a section given as raw data instead of a
    wrapped ``{sectionKey, data}`` object. Unknown keys are dropped; missing
    sections are filled in empty.

    This function's whole reason to exist: a model prompted for strict JSON
    still does not *guarantee* the exact envelope every time — it may flatten
    the wrapper, omit a section, add an unrequested key, or return a section's
    ``data`` with the wrong container kind. Each of those is coerced rather
    than raised on, because a stricter reading would turn a nearly-correct
    response into a total failure over a cosmetic mismatch. This is the
    boundary where "whatever came back" becomes "what every downstream consumer
    can rely on".

    Since the shape slips above are only half the problem, the same boundary
    now also enforces *content*: every string is stripped of markdown, every
    numeric field is coerced to a number or ``None``, every enumerated field is
    snapped onto a permitted value, and every URL is validated. Those are the
    defects that reached a live profile — asterisks in prose bound for a PDF, a
    five-term list in a scalar taxonomy field, and ``""`` beside ``28.5`` in
    one ``number|null`` column.

    :param raw: The parsed JSON from the synthesis call — untrusted in shape
        and in content.
    :param notes: Optional list. Every correction worth a human's attention is
        appended to it. A sanitizer that silently improves its input is
        indistinguishable from one that silently damages it, so the caller
        records these on the run.
    :returns: A profile with every active section key present, ``isComplete``
        freshly computed, and every ``data`` matching its declared container.
    """
    active = active if active is not None else sections()
    now = _now()
    profile = empty_profile(active)

    if not isinstance(raw, dict):
        if raw is not None and notes is not None:
            notes.append(
                f"the synthesis response was a {type(raw).__name__}, not an "
                f"object — no section could be read from it")
        return profile

    incoming = raw.get("sections")
    if not isinstance(incoming, dict):
        # The model may have returned the section map at the top level.
        incoming = raw

    for section in active:
        key = section["key"]
        value = incoming.get(key)
        if value is None:
            # Entirely missing: leave the empty skeleton rather than raising.
            # A partially-populated profile is still a useful deliverable.
            continue

        if isinstance(value, dict) and "data" in value:
            data = value.get("data")
        else:
            # The model flattened the wrapper and returned data directly.
            data = value

        # Coerce a container-kind mismatch rather than rejecting it: a single
        # object where an array of one was meant (or vice versa) is a shape
        # slip, not a content problem, and is correctable without losing any of
        # the model's actual work.
        expects_list = section["kind"] == "array"
        if expects_list and isinstance(data, dict):
            data = [data]
        elif not expects_list and isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}

        if data is None or not isinstance(data, (list, dict)):
            # Nothing sane to coerce (a bare string or number where an
            # object/array was expected) — leave the skeleton empty rather than
            # store garbage that breaks every consumer's container assumption.
            if notes is not None:
                notes.append(
                    f"{key}: expected {'an array' if expects_list else 'an object'}"
                    f", got {type(data).__name__} — section left empty")
            continue

        section_notes = []
        if expects_list:
            rows = []
            for row in data[:sanitize.MAX_LIST_ITEMS]:
                if isinstance(row, dict):
                    rows.append(_coerce_fields(section, row, section_notes))
                elif row not in (None, ""):
                    # A bare string where a row object was asked for. The one
                    # declared field that is plainly a label gets it, so a
                    # model answering "competitors: [Acme, Beta]" is kept
                    # rather than discarded on a container technicality.
                    label = next((f for f in ("name", "title", "metric",
                                              "market", "stream")
                                  if f in (section.get("fields") or {})), None)
                    if label:
                        rows.append(_coerce_fields(section, {label: row},
                                                   section_notes))
            data = [row for row in rows if is_populated(row)]
        else:
            data = _coerce_fields(section, data, section_notes)

        if section_notes and notes is not None:
            notes.extend(f"{key} · {line}" for line in section_notes)

        # `sub_sector` is a join key, not prose: it selects the peer cohort
        # the company is benchmarked against. Resolving it here means a
        # near-miss is rescued at the boundary rather than reaching the
        # scorecard as an unmatched string that quietly blanks a fifth of the
        # rating.
        if key == "company_profile" and isinstance(data, dict):
            _canonicalise_sub_sector(data, notes)

        # Financial rows carry the currency and scale the SOURCE used, so a
        # company reporting in crore is not forced through a conversion to
        # fit a field name.
        if key == "financial_summary" and isinstance(data, dict):
            _normalise_financial_rows(data, notes)

        profile["sections"][key] = {
            "sectionKey": key,
            "isComplete": is_populated(data),
            "lastUpdatedAt": now,
            "data": data,
            "sources": _coerce_sources(value, notes=notes, key=key,
                                       allowed=allowed_sources),
        }

    # Cross-section, so it runs once every section has been read: a metric is
    # checked against the financial rows in the same payload.
    _reconcile_metrics_with_series(profile, notes)

    return profile


def apply_document_center(profile, documents):
    """Fill section 8.17 from the run's own extraction records, not the model.

    Called after :func:`normalize_profile`, unconditionally overwriting that
    key. The pipeline already has exact, verified data about every document
    (filename, category, how it was handled, whether OCR ran); there is nothing
    for the model to usefully add, and letting it try would only risk it
    inventing document metadata that looks plausible and is not real.

    Mutates and returns ``profile`` for convenient chaining.
    """
    profile.setdefault("sections", {})
    profile["sections"]["document_center"] = {
        "sectionKey": "document_center",
        "isComplete": bool(documents),
        "lastUpdatedAt": _now(),
        "data": {"documents": list(documents or [])},
    }
    return profile
