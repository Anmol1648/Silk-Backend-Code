"""Which source is responsible for answering each parameter.

WHY THIS EXISTS
---------------
Two problems, one cause.

First, a parameter that no source is responsible for is INVISIBLE. It has a
rubric, it passes every config integrity check, and it scores blank on every
company forever while its weight quietly redistributes onto its siblings.
That is the same defect as the stranded category-F weight, one level further
down, and nothing in the system could detect it — because "who is supposed to
answer this?" was never written down anywhere.

Second, the scorecard's `missingEvidence` block is meant to convert a coverage
gap into a task: not "coverage is 40%" but "the financial model carries 20% of
the score and we do not have it". It read `expected_source` off ConfigParameter
— a field that does not exist — so every gap fell back to the same string,
"Additional information required", and the block named nothing at all.

THE FIVE SOURCES, AND WHY THE SPLIT IS WHAT IT IS
-------------------------------------------------
The division is not administrative. Each source is the one place a given fact
can be established honestly, and reading a fact from the wrong source is how a
scorecard becomes indefensible:

  RESEARCH    Public facts about the company and its market. Step 1 finds
              these, and they are checkable against a URL.

  MODEL       The founder's uploaded financial model. Revenue, margins,
              runway. Sourcing these from a marketing site would be the most
              damaging single thing this system could do, so category B is
              deliberately absent from research.

  BENCHMARK   The imported sector deal table. Not researched and not asked —
              read from a percentile computed at import over the whole
              population.

  DEAL        The terms of this specific raise: the ask, the pre-money, who is
              following on, what the money is for. Only the founder and the
              advisor know these; there is no public version.

  MANDATE     Internal to the advisor — sector experience, live mandate load,
              capacity. The company cannot know these and neither can research.
"""

RESEARCH = "Company research (step 1)"
MODEL = "Financial model (uploaded)"
BENCHMARK = "Sector benchmark table"
DEAL = "Deal terms (founder / advisor entered)"
MANDATE = "Advisor mandate context"

# Per-parameter overrides, for rows whose category does not imply their source.
# Both entries below sit in category C but are financial-model figures: a gross
# margin trend in basis points and a unit cost advantage are not facts a
# website states.
OVERRIDES = {
    "BQ_GM_TREND": MODEL,
    "BQ_UNIT_COST_ADV": MODEL,
    "BQ_PRICE_CHG": MODEL,
    "BQ_CAC_TREND": MODEL,
}

# Fallback by category, applied when no override and no explicit declaration
# elsewhere fits.
BY_CATEGORY = {
    "A": RESEARCH,
    "B": MODEL,
    "C": RESEARCH,
    "D": DEAL,
    "E": RESEARCH,
    "F": RESEARCH,
    "G": MANDATE,
}


def source_for(param, obtainable=None):
    """The source of record for one ConfigParameter.

    Resolution order is most-specific-first: an explicit override, then the
    scoring type (a lookup row is by definition read from the benchmark
    table), then what step 1 actually asks for, then the category default.

    :param obtainable: optional pre-read set of the keys step 1 asks for. When
        omitted this reads them, which costs two queries — fine for one row,
        but a caller naming the source of every unanswered parameter on a page
        pays that per row. The set is identical for all of them, so it can be
        read once and handed in; the resolution order above is unchanged.
    """
    key = getattr(param, "input_key", str(param))
    if key in OVERRIDES:
        return OVERRIDES[key]
    if getattr(param, "scoring_type", "") == "lookup":
        return BENCHMARK
    try:
        if obtainable is None:
            from fundos.profile.assessment_extraction import obtainable_keys
            obtainable = obtainable_keys()
        if key in obtainable:
            return RESEARCH
    except Exception:      # pragma: no cover — import-order safety only
        pass
    if not getattr(param, "is_workbook_input", True):
        return BENCHMARK
    return BY_CATEGORY.get(getattr(param, "category_code", ""), RESEARCH)


def unsourced(parameters):
    """Scored parameters that no source is responsible for.

    Returns input keys. A non-empty result means those rows score blank on
    every company and their weight redistributes silently — worth failing a
    test over, and worth surfacing in the config audit.
    """
    known = set(BY_CATEGORY)
    return sorted(
        p.input_key for p in parameters
        if p.feeds_score and p.scoring_type != "lookup"
        and p.input_key not in OVERRIDES
        and (p.category_code or "") not in known)
