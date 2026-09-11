"""Phase 3 — extract assessment inputs from company research.

WHICH PARAMETERS THIS CAN ANSWER, AND WHY NOT THE OTHERS
--------------------------------------------------------
The workbook declares 80 parameters across seven categories. They divide
cleanly by WHERE THE TRUTH LIVES, and that division is not a limitation to be
worked around — it is the reason the numbers can be trusted:

  * Categories A (Team), C (Business Quality), E (Sector) and F (Sub-Sector),
    plus parts of D (Deal Dynamics) — obtainable from company research. A
    founder's years in the industry, the company's age, its patent count, the
    sector's TAM: all of these are public facts about a company, and step 1
    already reads the sources that carry them.

  * Category B (Financials) — comes from the uploaded financial model. Revenue
    scale, CM1, CM2, EBITDA margin and runway are audited-model figures, and
    sourcing them from a marketing website would be the single most damaging
    thing this module could do. They are DELIBERATELY absent from the
    obtainable set, and `document` extraction fills them at tier 1.

  * Category G (Mandate Context) — internal to the advisor: how many deals
    they have done in this sector, their live mandate count, their capacity.
    The company cannot know these and neither can research.

So this emits roughly 45 of the 80, and leaving the other 35 blank here is
correct behaviour, not a gap.

THE SUB-SECTOR PROBLEM
----------------------
Category F carries 20% of the rating — double Sector — and it is scored
against one of 33 clubbed benchmark groups. The label the model returns is
free text ("B2B SaaS for logistics"), and the benchmark table is keyed by
group. Getting from one to the other is what `resolve_sub_sector()` does,
against the 104 raw labels loaded in Phase 2. A wrong group mis-scores a
fifth of the deal, so the resolution is recorded with its method and left
UNSET rather than guessed when nothing matches.
"""
import logging

logger = logging.getLogger(__name__)

# Confidence below which a value is stored but flagged. Not a rejection
# threshold: a low-confidence value with its source attached is still better
# than a blank, provided the reader can see the confidence.
LOW_CONFIDENCE = 0.60

# Below this many characters of real source, with no search performed, the
# model is answering from memory whatever it claims. 400 characters is about a
# paragraph — enough for a company name and a tagline, not a funding history.
MIN_GROUNDED_CHARS = 400

# How much of the consolidated dossier to send with the extraction call.
#
# Bounded by `adapter.MAX_CONTEXT_CHARS` (120,000), which caps the WHOLE
# context and cuts from the end. This call also carries 61 parameter
# definitions, 24 anchor band descriptions and the sector vocabulary — and
# those are the instructions, not the evidence. A dossier large enough to
# push them past the cap would leave the model reading source material
# without being told what to extract from it.
#
# 500,000 sends nearly the full dossier so coverage is maximised. The
# dossier is ordered by source tier, so what a truncation drops is the LEAST
# authoritative material: the financial model and the deck sit at the top,
# general web research at the bottom.
MAX_DOSSIER_CHARS = 500_000

# Categories step 1 does not source, and why. This is the workbook's own
# division of labour, not a limitation to be worked around.
#   B — the uploaded financial model. Sourcing revenue or margin off a
#       marketing site is the most damaging thing this module could do.
#   G — internal to the advisor: sector track record, live mandate load.
NOT_RESEARCHABLE = {"B", "G"}


def question_set(tenant_id=None):
    """Every parameter step 1 should try to answer, straight from config.

    Read rather than written. The previous hand-kept list used `SUB_TAM`
    where the workbook says `SEC_TAM_SUB` — so nothing the sub-sector half
    emitted could ever match a parameter — and omitted rows the workbook
    collects. A second copy of the contract is what made that possible.

    Reference rows are INCLUDED: the sheet says they "do not feed a score,
    but they must still be filled where the data exists", and they are what
    the contradiction checks compare against.
    """
    from fundos.assessment.models import ConfigParameter

    base = {p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id__isnull=True, is_active=True, is_workbook_input=True)}
    base.update({p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id=tenant_id, is_active=True, is_workbook_input=True)})

    numeric, anchors = [], []
    for _key, p in sorted(base.items(), key=lambda kv: kv[1].sort_order):
        if p.category_code in NOT_RESEARCHABLE:
            continue
        (anchors if p.scoring_type == "anchor" else numeric).append(p)
    return numeric, anchors


def obtainable_keys(tenant_id=None):
    numeric, anchors = question_set(tenant_id)
    return {p.input_key for p in numeric} | {p.input_key for p in anchors}


EXTRACTION_SYSTEM = (
    "You extract typed facts about one company for an investment scorecard. "
    "You are given research context and a list of parameters, each with its "
    "key, its unit and the question it answers.\n\n"
    "`dossier` is your PRIMARY SOURCE: the full text of the company's "
    "uploaded documents — the investor deck, the financial model — merged "
    "with the research pass, ordered with the most authoritative material "
    "first. Read it before anything else. `document_extracts` beside it holds "
    "short per-file summaries; where the two differ, the dossier is the "
    "original and wins. Financial figures in particular survive in the "
    "dossier and are routinely absent from the summaries, so a revenue, burn "
    "or runway figure you cannot find in `document_extracts` is very often "
    "sitting in `dossier`.\n\n"
    "NEVER ANSWER FROM MEMORY. Every value must come from a source you were "
    "given or retrieved in this session. Recalling a figure you happen to "
    "know about this company is NOT a source, and is the one failure this "
    "task cannot tolerate: an unattributable number is indistinguishable "
    "from a correct one until it reaches an investor.\n"
    "- Never estimate, never infer a plausible figure, never substitute an "
    "industry norm for a fact about this company.\n"
    "- If the sources do not support a parameter, OMIT it. A blank is a "
    "correct answer that the scorecard handles properly; a fabricated number "
    "corrupts a weighted score and is not recoverable downstream.\n"
    "- Return the value in the UNIT ASKED FOR. If your source states it in "
    "another unit, convert it and say so in justification.\n"
    "- CITE EVERY VALUE, one of two ways. For something read on the web, give "
    "`sourceUrl`. For something read in one of the company's own uploaded "
    "documents — the deck, the financial model, anything in `dossier` — give "
    "`sourceDoc` naming the file and the place inside it, e.g. "
    "\"Project Orah_Financial Model.xlsx, Summary tab\" or "
    "\"Investor Deck.pptx, slide 12\". A value with neither is an opinion and "
    "will be discarded.\n"
    "- Do NOT invent a URL for a figure you read in a document. `sourceDoc` "
    "is the correct and preferred citation for it: the file is held in this "
    "system and can be checked directly. Uploaded documents are the most "
    "authoritative source available for revenue, burn, runway, headcount and "
    "the cap table — never skip a parameter merely because its evidence has "
    "no web address.\n"
    "- `citable_sources` LISTS EVERY SOURCE THE DOSSIER WAS BUILT FROM, "
    "uploaded documents first. For anything read in `dossier`, copy one of "
    "those strings EXACTLY into `sourceDoc` and add the place inside it, "
    "e.g. \"<filename>, slide 12\". The dossier is a MERGED corpus: it is "
    "assembled from those sources and is not itself one, so citing \"the "
    "dossier\" names the container rather than the origin and cannot be "
    "turned to.\n"
    "- PREFER AN UPLOADED DOCUMENT over web research wherever both say the "
    "same thing. The company's own deck and financial model are what a "
    "reader trusts.\n"
    "- If you genuinely cannot identify which source a figure came from, "
    "still answer and put \"dossier\" in `sourceDoc`. An answer you found in "
    "the evidence must never be dropped for want of a label — silence is "
    "read downstream as 'this company has no such figure', which is a "
    "different and worse claim than 'read here'.\n"
    "- confidence is your own: 0.9+ when a source states it directly, 0.7 "
    "when you derived it from stated figures, below 0.6 when it is a weak "
    "inference.\n\n"
    "For qualitative parameters you are given four band definitions. Choose "
    "the band the evidence ACTUALLY satisfies. If the evidence does not "
    "clearly meet a band, choose the band BELOW it.\n\n"
    # The vocabulary was already being PASSED in the context and never
    # mentioned here, so the model treated `sector` as free text and answered
    # descriptively — "Digital Health" where the table says "Healthtech".
    # Nothing matched, and the six percentile rows that read the deal
    # database (E.2, E.3, E.6, F.2, F.3, F.6) scored blank on a company that
    # had a perfectly ordinary sector. Categories E and F are 30% of the
    # rating between them.
    "SECTOR AND SUB-SECTOR ARE CLOSED LISTS, NOT FREE TEXT. `sector` must be "
    "copied EXACTLY from `sector_vocabulary` and `subSector` EXACTLY from "
    "`sub_sector_vocabulary` in the context — same spelling, same casing, "
    "character for character. These are not descriptions of the company; "
    "they select which cohort of real deals it is benchmarked against, and a "
    "label that is not on the list matches nothing and silently removes 30% "
    "of the scorecard.\n"
    "- Choose the CLOSEST entry on the list even when a more precise "
    "description exists. A healthcare-software company is whatever the list "
    "calls that, not 'Digital Health'.\n"
    "- Return \"\" for either field only if no entry is even loosely "
    "applicable. Never coin a new label.\n\n"
    # `sector` and `subSector` come FIRST, and the order is load-bearing.
    #
    # They used to be emitted after `values` and `bands`. A response that hits
    # the output ceiling is truncated from the end, the tail is recovered as a
    # valid prefix, and the two fields that decide 30% of the scorecard were
    # simply not in it — so a run that answered 15 parameters correctly
    # reported sector "" and scored categories E and F blank. Emitting them
    # before the long arrays makes them the first thing written rather than
    # the first thing lost.
    'Return JSON only, with the keys in THIS ORDER: '
    '{"sector":"","subSector":"",'
    '"values":[{"inputKey":"","value":"","unit":"",'
    '"sourceUrl":"","sourceDoc":"","sourceTier":1-4,"confidence":0.0-1.0,'
    '"justification":"one sentence"}],'
    '"bands":[{"inputKey":"","band":"Excellent|Good|Fair|Poor",'
    '"confidence":0.0-1.0,"justification":"","sourceUrl":"","sourceDoc":""}],'
    '"missing":[""]}\n\n'
    "CRITICAL REQUIREMENT: In `values` and `bands`, `inputKey` MUST BE EXACTLY ONE OF THE `inputKey` STRINGS LISTED IN `parameters` OR `qualitative_parameters` (for example, 'TEAM_FDR_EXP', 'ANC_FDR_EDU', 'FIN_REV_SCALE', 'ANC_MOAT_IP'). DO NOT RETURN DESCRIPTIVE OR LOWERCASE STRINGS LIKE 'headcount' OR 'arr'. A RESPONSE WITH INVALID KEYS CANNOT BE MATCHED TO THE SCORECARD.\n"
    "Write `sector` and `subSector` FIRST, before any value. They are short "
    "and they select the benchmark cohort; if your response is cut short "
    "everything after them is recoverable, and they are not.\n"
    "Give sourceUrl OR sourceDoc on every entry — whichever matches where you "
    "actually read it. Leave the other empty.\n"
    "Keep `justification` to one short sentence. Output length is capped, and "
    "a long justification costs a parameter that would otherwise have been "
    "answered."
)


# ---------------------------------------------------------------------------
# Sub-sector resolution — category F is 20% of the rating
# ---------------------------------------------------------------------------

def _normalise(text):
    """Fold a label for comparison: 'Health Tech' == 'Healthtech'.

    The model returned 'Health Tech' where the sheet says 'Healthtech', and
    an exact match rejected it. Spacing and punctuation in a sector name
    carry no meaning, and letting them decide a 20%-weight lookup is a
    matching bug dressed up as a data problem.
    """
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def vocabulary():
    """The exact sector and sub-sector labels the workbook knows.

    Given to the model as a CLOSED LIST to choose from. v24 asked for free
    text and got 'Corporate Employee Health Benefits' and 'Digital Healthcare
    / Teleconsultation' — reasonable descriptions, matching nothing, leaving
    category F blank at 20% of the rating. The model was never shown the 18
    sectors and 33 groups it was expected to pick from, so that was our
    omission, not its error.
    """
    from fundos.assessment.models import SectorDealData

    if not SectorDealData.objects.exists():
        try:
            from django.core.management import call_command
            call_command("seed_sector_taxonomy")
        except Exception as err:
            logger.warning("ASSESSMENT INPUTS: auto-seeding sector taxonomy failed: %s", err)

    sectors = sorted(SectorDealData.objects.filter(
        level="sector").values_list("name", flat=True))
    groups = sorted(SectorDealData.objects.filter(
        level="sub_sector").values_list("name", flat=True))
    return sectors, groups


def resolve_sector(label):
    """Match a sector label against the table, ignoring spacing and case.

    Sub-sector resolution has had a fuzzy tier and a warning on failure since
    category F was found scoring blank. Sector had neither: an exact match, a
    bare `return None`, and a caller that skipped the row without a word. So
    'Digital Health' against a table that says 'Healthtech' removed E.2, E.3
    and E.6 from the scorecard and left nothing in the log to say why.

    Categories E and F carry 30% of the rating between them. Both halves of
    that lookup now behave the same way, and both say so when they fail.
    """
    from fundos.assessment.models import SectorDealData

    target = _normalise(label)
    if not target:
        return None
    names = list(SectorDealData.objects.filter(
        level="sector").values_list("name", flat=True))
    for name in names:
        if _normalise(name) == target:
            return name

    # A curated alias for a descriptive label. The closed-list instruction in
    # the prompt is the primary defence and it now has a populated vocabulary
    # to offer, but a model that answers "Digital Health" anyway has named a
    # real market correctly and picked the wrong word for it -- and fuzzy
    # matching cannot rescue that pair, because "Digital Health" and
    # "Healthtech" share almost no characters. The alias list is data, and
    # every entry resolves to one of the 18 names above.
    from fundos.assessment.management.commands.seed_sector_taxonomy import (
        sector_aliases)
    aliases = {_normalise(raw): name for raw, name in sector_aliases().items()}
    aliased = aliases.get(target)
    if aliased:
        for name in names:
            if _normalise(name) == _normalise(aliased):
                logger.info(
                    "ASSESSMENT INPUTS: sector %r resolved to %r through the "
                    "taxonomy alias list.", label, name)
                return name

    # Near-miss, above the same high floor sub-sector resolution uses. Below
    # it, blank is the correct answer: benchmarking a company against the
    # wrong cohort scores it confidently and wrongly, which is worse than not
    # scoring those rows at all.
    try:
        from rapidfuzz import fuzz, process
        best = process.extractOne(str(label), names, scorer=fuzz.WRatio)
        if best and best[1] >= 88:
            logger.info(
                "ASSESSMENT INPUTS: sector %r matched %r at %d%%.",
                label, best[0], int(best[1]))
            return best[0]
    except ImportError:
        pass

    logger.warning(
        "ASSESSMENT INPUTS: sector %r is not in the benchmark taxonomy, so "
        "the sector percentile rows (E.2 Deal Velocity, E.3 Deal Ticket "
        "Size, E.6 Active Investors) cannot score. The model must choose "
        "from: %s", label, ", ".join(sorted(names)) or "(table is empty)")
    return None


def resolve_sub_sector(label):
    """Map a free-text sub-sector label onto a clubbed benchmark group.

    Returns (group, method). `method` is recorded rather than discarded so a
    reviewer can tell an exact match from a fuzzy one: the two carry very
    different risk when the answer decides 20% of the score.

    Returns (None, "unresolved") when nothing matches, and that is the right
    outcome. Assigning a company to the wrong benchmark group is worse than
    leaving category F blank, because blank is excluded from the denominator
    and redistributes its weight, whereas wrong quietly scores it.
    """
    from fundos.assessment.models import SectorDealData, SectorMapping

    text = (label or "").strip()
    if not text:
        return None, "unresolved"

    target_norm = _normalise(text)

    # 1. Exact match on a raw label loaded from the workbook.
    row = SectorMapping.objects.filter(raw_label__iexact=text).first()
    if row:
        return row.clubbed_group, "exact"

    # 2. The label may already BE a clubbed group or sub-sector name.
    group = SectorDealData.objects.filter(
        level="sub_sector", clubbed_group__iexact=text).first()
    if group:
        return group.clubbed_group, "group"
    named = SectorDealData.objects.filter(level="sub_sector",
                                          name__iexact=text).first()
    if named:
        return (named.clubbed_group or named.name), "name"

    # 3. Aliases live in SectorMapping, which is step 4 — not in a literal
    #    here.
    #
    #    There WAS a dictionary at this point mapping nine labels onto groups,
    #    and it demonstrated why sector knowledge does not belong in source.
    #    Four of its nine entries — the tyre ones, added to rescue exactly the
    #    Tyreplex case — targeted "B2B E-commerce", which is not a group in
    #    the benchmark table. The table holds "B2B Ecommerce", without the
    #    hyphen. So the aliases written to fix the problem resolved to a
    #    cohort that does not exist and did nothing at all, and nothing said
    #    so, because an alias that resolves to a missing group looks exactly
    #    like an alias that was never consulted.
    #
    #    They are seeded into SectorMapping instead, with the targets
    #    corrected, so they are visible, editable without a deploy, and
    #    validated against the groups that actually exist.

    # 4. Normalized exact match on raw_label or clubbed_group.
    for m in SectorMapping.objects.all():
        if _normalise(m.raw_label) == target_norm or _normalise(m.clubbed_group) == target_norm:
            return m.clubbed_group, "normalized"

    for d in SectorDealData.objects.filter(level="sub_sector"):
        if _normalise(d.name) == target_norm or _normalise(d.clubbed_group) == target_norm:
            return d.clubbed_group, "normalized"

    # 5. Fuzzy matching above a high threshold.
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        logger.warning(
            "ASSESSMENT INPUTS: rapidfuzz is not installed, so sub-sector "
            "'%s' could only be matched exactly. Install it to enable "
            "near-miss matching.", text)
        return None, "unresolved"

    candidates = list(SectorMapping.objects.values_list("raw_label", "clubbed_group"))
    for d in SectorDealData.objects.filter(level="sub_sector"):
        candidates.append((d.name, d.clubbed_group))
        candidates.append((d.clubbed_group, d.clubbed_group))

    if not candidates:
        return None, "unresolved"

    labels = [c[0] for c in candidates]
    best = process.extractOne(text, labels, scorer=fuzz.WRatio)
    if best and best[1] >= 85:
        return candidates[labels.index(best[0])][1], f"fuzzy:{int(best[1])}"
    return None, "unresolved"


#: Why a benchmark percentile could not be supplied. The distinction is the
#: whole point of recording it: three of these are operations someone can
#: act on — import the sheet, add a mapping row, check a label — and only the
#: fourth is the table honestly declining to rank a cohort it has too few
#: deals for. Reporting all four as "no percentile is held" told a reader the
#: data does not exist when in three cases out of four it does.
BENCHMARK_TABLE_EMPTY = "table_empty"
BENCHMARK_UNRESOLVED = "cohort_unresolved"
BENCHMARK_COHORT_MISSING = "cohort_not_in_table"
BENCHMARK_BELOW_FLOOR = "below_sample_floor"

BENCHMARK_REASONS = {
    BENCHMARK_TABLE_EMPTY: (
        "The sector deal table has not been imported, so no cohort can be "
        "ranked. Import the workbook's Sector Deal Data sheet."),
    BENCHMARK_UNRESOLVED: (
        "This company's {level} could not be matched to a benchmark cohort, "
        "so there is no population to rank it against."),
    BENCHMARK_COHORT_MISSING: (
        "{name!r} is not a row in the {level} benchmark table, so there is "
        "no population to rank against."),
    BENCHMARK_BELOW_FLOOR: (
        "{name!r} holds {deals} deals, below the sheet's reliable-sample "
        "floor, so the table withholds this percentile rather than ranking "
        "on too little data."),
}


def _benchmark_cohort_rows(sector, sub_sector):
    """The deal-table row behind each level, and why one is absent.

    Returns `(rows, group, method, absent)` where `rows` is keyed by level
    and `absent` carries a `(reason, context)` pair for each level that
    produced nothing.
    """
    from fundos.assessment.models import SectorDealData

    group, method = resolve_sub_sector(sub_sector)
    if not SectorDealData.objects.exists():
        logger.warning(
            "ASSESSMENT INPUTS: the sector benchmark table is EMPTY, so "
            "categories E and F cannot score at all. Import the workbook's "
            "Sector Deal Data sheet (manage.py import_sector_benchmarks).")
        empty = (BENCHMARK_TABLE_EMPTY, {})
        return {}, group, method, {"sector": empty, "sub_sector": empty}

    rows, absent = {}, {}
    for level, name in (("sector", resolve_sector(sector) or ""),
                        ("sub_sector", (group or "").strip())):
        if not name:
            absent[level] = (BENCHMARK_UNRESOLVED, {"level": level})
            continue
        row = SectorDealData.objects.filter(level=level,
                                            name__iexact=name).first()
        if row is None and level == "sub_sector":
            row = SectorDealData.objects.filter(
                level=level, clubbed_group__iexact=name).first()
        if row is None:
            logger.warning(
                "ASSESSMENT INPUTS: %r is not in the %s benchmark table — "
                "check the label against the imported sheet.", name, level)
            absent[level] = (BENCHMARK_COHORT_MISSING,
                             {"name": name, "level": level})
            continue
        rows[level] = row
    return rows, group, method, absent


def resolve_benchmarks(sector, sub_sector):
    """The six percentile nodes, plus a reason for each one not supplied.

    Returns `(rows, group, method, absences)`. `absences` maps an input key
    to `(reason_code, sentence)` so the payload can say which of the four
    things went wrong rather than asserting the table holds nothing.
    """
    from fundos.assessment.models import ConfigParameter

    rows, group, method, absent = _benchmark_cohort_rows(sector, sub_sector)

    out, absences = [], {}
    for p in ConfigParameter.objects.filter(is_active=True,
                                            scoring_type="lookup"):
        row = rows.get(p.benchmark_level)
        if row is None:
            reason, ctx = absent.get(
                p.benchmark_level, (BENCHMARK_UNRESOLVED,
                                    {"level": p.benchmark_level}))
            absences[p.input_key] = (
                reason, BENCHMARK_REASONS[reason].format(**ctx))
            continue
        value = getattr(row, f"{p.benchmark_metric}_score", None)
        if value is None:
            # Under the sheet's minimum sample the percentile is withheld,
            # and withheld is not the same as low.
            logger.info(
                "ASSESSMENT INPUTS: %s has no %s percentile (%s deals, below "
                "the reliable-sample floor) — scoring blank.",
                row.name, p.benchmark_metric, row.deal_count)
            absences[p.input_key] = (
                BENCHMARK_BELOW_FLOOR,
                BENCHMARK_REASONS[BENCHMARK_BELOW_FLOOR].format(
                    name=row.name, deals=row.deal_count or 0))
            continue
        detail = (f"{row.level} '{row.name}' — {row.deal_count or 0} deals"
                  + (f", as of {row.as_of_date}" if row.as_of_date else ""))
        out.append({
            "input_key": p.input_key, "value": str(value), "unit": "Score",
            "source_type": "benchmark", "source_tier": 2, "source_url": "",
            "source_detail": detail, "confidence": 0.95,
            "justification": ("Percentile from the imported deal table, "
                              "ranked at import across the whole population.")})
    return out, group, method, absences


def benchmark_absences(sector, sub_sector):
    """`{input_key: (reason_code, sentence)}` for every percentile withheld."""
    return resolve_benchmarks(sector, sub_sector)[3]


def benchmark_inputs(sector, sub_sector):
    """The six percentile nodes, read from the imported deal table.

    Not researched and not asked — the workbook computes these by looking the
    sector and the clubbed group up in Sector Deal Data. Emitted as rows so
    the number that scored is visible beside its source rather than being
    resolved invisibly at scoring time.
    """
    rows, group, method, _absences = resolve_benchmarks(sector, sub_sector)
    return rows, group, method


#: What a value read out of the consolidated dossier is credited to when the
#: model names no more specific source. The dossier is stored per run and can
#: be re-read, so this is a real citation — just a coarse one.
DOSSIER_SOURCE = "Consolidated research dossier"

#: Source tier for those values. Deliberately 3 ("derived / secondary"), not
#: 1: a figure traced to a named file or URL must continue to outrank one
#: traced only to the merged corpus, and `profile_bridge._outranks` decides
#: that by tier. Accepting the value is not the same as trusting it equally.
DOSSIER_TIER = 3


def _citation(item, dossier=False):
    """Where this value came from: (url, detail, ok).

    A value must name a source that someone else can go and check. That rule
    is not negotiable — it is what stops a remembered figure entering the
    scorecard wearing a confidence score.

    But it was implemented as "must have a URL", and the company's own
    financial model does not have one. So every figure read out of an
    uploaded document was discarded as unsourced, while the run logged
    `unsourced_rejected: 11` and the scorecard showed category B blank on a
    company that had supplied its model. The most authoritative source in the
    system was the only one that could not be cited.

    A document reference is a stronger citation than a URL, not a weaker one:
    the file is held in the system, so "financial_model.xlsx, Summary tab" can
    be verified against the artefact itself and cannot rot the way a link can.
    Either satisfies the rule. Neither present still rejects.
    """
    url = str(item.get("sourceUrl") or "").strip()
    doc = str(item.get("sourceDoc") or item.get("sourceDocument") or "").strip()
    if url:
        return url[:1024], doc[:2000], True
    if doc:
        return "", doc[:2000], True
    # Nothing named, but a dossier WAS supplied for this call.
    #
    # A run on 27 Aug answered about thirty-six parameters and threw away
    # TWENTY-TWO of them here, including every qualitative anchor, so
    # Financials, Deal Dynamics and Business Quality all rendered blank on a
    # company whose 845,000-character dossier was sitting in the prompt. The
    # model had read the evidence; it just had no filename to quote, because
    # what it was given is a merged corpus rather than a set of files.
    #
    # Discarding those is not caution, it is losing evidence we hold. The
    # dossier is written to storage per run and can be re-read line by line,
    # so "the dossier" is a source that can actually be checked — which is
    # the entire point of the rule. It is credited at a lower tier so a named
    # file or URL still wins wherever both exist.
    if dossier:
        return "", DOSSIER_SOURCE, True
    return "", "", False


def grounding(payloads, searched=False):
    """Did this run RETRIEVE anything, or is it about to answer from memory?

    v24 measured character count against a floor of 400. That was the wrong
    measure and the wrong number: the Plum run cleared it with 6,020
    characters of marketing website and the deep extract then populated 229 of
    231 fields from it, with no search. Marketing copy does not contain a
    founder's years in industry, a churn rate or a peer's last raise — so
    every one of those fields came from recollection, and the gate waved it
    through because the bytes were there.

    What actually distinguishes a grounded run is whether anything was
    RETRIEVED: a web search performed, a document uploaded, or a research
    adapter that returned facts. A website alone is one page of the company's
    own marketing, and it can support a description but not a scorecard.

    Returns (ok, reason, mode). `mode` is 'full' when retrieval happened and
    'website_only' when the run may answer descriptive questions from the
    site but must not answer market or history questions at all.
    """
    documents = (payloads or {}).get("documents") or []
    research = (payloads or {}).get("research") or {}
    website = (payloads or {}).get("website") or {}
    dossier = (payloads or {}).get("dossier") or ""

    research_chars = len(str(research)) if research else 0
    has_documents = bool(documents)
    has_research = research_chars > 200
    website_chars = len(str(website))
    # The consolidated dossier is retrieved evidence by construction — it is
    # the merged output of the research pass and the document reader, and it
    # exists only after both have run. It counts for the same reason a
    # document does, and its absence is why runs with a fully-read deck were
    # still being graded as ungrounded.
    has_dossier = len(str(dossier)) >= MIN_GROUNDED_CHARS

    if searched or has_documents or has_research or has_dossier:
        return True, "", "full"
    if website_chars >= MIN_GROUNDED_CHARS:
        # Degraded, not refused. The site can answer "how old is the company"
        # and "who are the named CXOs"; the caller restricts the question set
        # so nothing market-facing is asked of a marketing page.
        return True, (
            f"no search, no documents and no research — only {website_chars} "
            f"characters of the company's own website. Restricting the "
            f"question set to what a website can actually evidence."
        ), "website_only"
    return False, (
        f"nothing was retrieved: no search, no documents, no research, and "
        f"only {website_chars} characters of website"), "none"


# Parameters a company's own website can legitimately evidence. Everything
# else — market size, peer behaviour, funding history, churn — requires
# retrieval, and asking for it from a marketing page is an invitation to
# invent. Kept as a prefix/exact set so it survives workbook revisions.
WEBSITE_EVIDENCEABLE = {
    "BQ_CO_AGE", "TEAM_CXO_SEATS", "TEAM_ADVISORS", "TEAM_MARQUEE_CLIENTS",
    "BQ_PATENTS", "ANC_CXO_PRODUCT", "ANC_CXO_SALES", "ANC_CXO_FINANCE",
    "ANC_CXO_OPS", "ANC_BOARD", "ANC_ADVISORS", "ANC_FDR_EDU",
    "ANC_PRIOR_STARTUP", "ANC_MOAT_BRAND", "ANC_MOAT_IP", "ANC_REGULATION",
}


def extract_assessment_inputs(profile, payloads=None, user=None):
    """Collect the workbook's parameters for this company. Never raises."""
    counts = {"asked": 0, "written": 0, "low_confidence": 0, "bands": 0,
              "benchmarks": 0, "missing": 0, "sub_sector_method": "",
              "unsourced_rejected": 0, "unknown_key_rejected": 0,
              "excluded_category_rejected": 0, "rescued": 0,
              "unparseable_band": 0, "dossier_sourced": 0, "mode": ""}

    if payloads is None:
        from fundos.profile.services import collect_sources
        payloads, _ = collect_sources(profile, user=user)

    numeric, anchors = question_set(getattr(profile, "tenant_id", None))
    counts["asked"] = len(numeric) + len(anchors)
    if not counts["asked"]:
        logger.error(
            "ASSESSMENT INPUTS %s: no parameters are configured, so step 1 "
            "has nothing to collect. Import the assessment workbook: "
            "manage.py import_assessment_workbook <file> --activate",
            profile.company_id)
        return {**counts, "error": "no_config"}

    ok, why, mode = grounding(payloads)
    if not ok:
        logger.error(
            "ASSESSMENT INPUTS %s: refusing to extract — %s. Anything "
            "returned would be the model's recollection, which cannot be "
            "cited or checked and is worse on a scorecard than a blank.",
            profile.company_id, why)
        return {**counts, "error": "ungrounded", "reason": why}
    counts["mode"] = mode
    if mode == "website_only":
        logger.warning("ASSESSMENT INPUTS %s: %s", profile.company_id, why)
        numeric = [p for p in numeric
                   if p.input_key in WEBSITE_EVIDENCEABLE]
        anchors = [p for p in anchors
                   if p.input_key in WEBSITE_EVIDENCEABLE]
        counts["asked"] = len(numeric) + len(anchors)

    try:
        data = _ask(profile, payloads, numeric, anchors, user=user)
    except Exception as e:
        logger.error("ASSESSMENT INPUTS %s: call failed: %s",
                     profile.company_id, e, exc_info=True)
        return {**counts, "error": str(e)}
    if not data:
        logger.warning("ASSESSMENT INPUTS %s: unparseable response.",
                       profile.company_id)
        return counts

    by_key = {p.input_key: p for p in numeric}
    anchors_by_key = {p.input_key: p for p in anchors}
    anchor_keys = set(anchors_by_key)
    # Combined lookup so we can rescue keys placed in the wrong list.
    all_asked = {**by_key, **anchors_by_key}
    # All valid ConfigParameter keys — to distinguish "excluded by design"
    # (cat B/G) from "truly unknown / hallucinated".
    from fundos.assessment.models import ConfigParameter
    _all_config = {p.input_key: p.category_code
                   for p in ConfigParameter.objects.filter(
                       tenant_id__isnull=True, is_active=True)}
    rows = []
    # Whether the consolidated dossier was actually in this call's context.
    # It decides whether an uncited answer is attributable evidence or an
    # unsupported claim, so it is read from the payload rather than assumed.
    has_dossier = len(str((payloads or {}).get("dossier") or "")) >= \
        MIN_GROUNDED_CHARS

    # Anchors arriving in `values` are ACCEPTED rather than dropped. v24 kept
    # two disjoint lists and silently `continue`d anything that missed the
    # numeric one, so a run could report 61 asked, 35 missing and 1 written
    # with 25 answers unaccounted for — and no counter said so. Where a model
    # puts an answer is a formatting detail; whether it answered is not.
    values = list(data.get("values") or [])
    bands = list(data.get("bands") or [])
    for item in list(values):
        if item.get("inputKey") in anchor_keys and not item.get("band"):
            band = str(item.get("value") or "").strip().title()
            if band in ("Excellent", "Good", "Fair", "Poor"):
                values.remove(item)
                bands.append({**item, "band": band})

    for item in values:
        key = item.get("inputKey")
        cfg = by_key.get(key)
        if cfg is None:
            # The key is not a numeric parameter.  Before rejecting, check
            # whether it belongs to the anchor set — the LLM may have placed
            # an anchor answer into the values list without a band label.
            if key in anchor_keys:
                # Try to interpret the value as a band name.
                band = str(item.get("value") or "").strip().title()
                if band in ("Excellent", "Good", "Fair", "Poor"):
                    bands.append({**item, "band": band})
                    counts["rescued"] += 1
                    logger.info(
                        "ASSESSMENT INPUTS: rescued anchor %r from values "
                        "(value %r interpreted as band).", key, band)
                else:
                    # Anchor key, non-band value — cannot be scored.
                    counts["unknown_key_rejected"] += 1
                    logger.warning(
                        "ASSESSMENT INPUTS: %r is an anchor key but appeared "
                        "in values with non-band value %r — dropped.",
                        key, str(item.get("value"))[:80])
                continue
            # Not in numeric, not in anchors — is it a valid key we excluded?
            cat = _all_config.get(key)
            if cat and cat in NOT_RESEARCHABLE:
                counts["excluded_category_rejected"] += 1
                logger.debug(
                    "ASSESSMENT INPUTS: %r belongs to excluded category %s "
                    "— skipped by design.", key, cat)
            else:
                counts["unknown_key_rejected"] += 1
                logger.warning(
                    "ASSESSMENT INPUTS: %r is not a requested key "
                    "(value=%r). Check prompt/workbook alignment.",
                    key, str(item.get("value"))[:80])
            continue
        raw = item.get("value")
        if raw in (None, "", "null", "N/A"):
            continue
        url, detail, sourced = _citation(item, dossier=has_dossier)
        if not sourced:
            # No citation of any kind, no value. This is the rule that stops a
            # remembered figure entering the scorecard wearing a confidence
            # score. A document reference satisfies it; see _citation.
            counts["unsourced_rejected"] += 1
            continue
        from_dossier = detail == DOSSIER_SOURCE and not url
        if from_dossier:
            counts["dossier_sourced"] += 1
        confidence = _float(item.get("confidence"))
        if confidence < LOW_CONFIDENCE:
            counts["low_confidence"] += 1
        rows.append({
            "input_key": cfg.input_key, "value": str(raw)[:255],
            "unit": (item.get("unit") or cfg.unit or "")[:32],
            "source_type": "profile" if url else "document",
            "source_url": url,
            "source_detail": detail,
            "source_tier": (DOSSIER_TIER if from_dossier
                            else _tier(item.get("sourceTier"))),
            "confidence": round(confidence, 2),
            "justification": (item.get("justification") or "")[:2000]})

    for item in bands:
        key = item.get("inputKey")
        band = (item.get("band") or "").strip().title()
        if key not in anchor_keys:
            # The key is not an anchor parameter.  Before rejecting, check
            # whether it is a numeric parameter the LLM misplaced into bands.
            if key in by_key:
                # Rescue: extract the raw value and re-route to values
                # processing.  The band text itself is often a description
                # that carries no numeric meaning, so use "value" if present.
                raw = item.get("value") or item.get("band")
                if raw not in (None, "", "null", "N/A"):
                    url, detail, sourced = _citation(item, dossier=has_dossier)
                    if sourced:
                        cfg = by_key[key]
                        from_dossier = detail == DOSSIER_SOURCE and not url
                        if from_dossier:
                            counts["dossier_sourced"] += 1
                        confidence = _float(item.get("confidence"))
                        if confidence < LOW_CONFIDENCE:
                            counts["low_confidence"] += 1
                        rows.append({
                            "input_key": cfg.input_key,
                            "value": str(raw)[:255],
                            "unit": (item.get("unit") or cfg.unit or "")[:32],
                            "source_type": "profile" if url else "document",
                            "source_url": url,
                            "source_detail": detail,
                            "source_tier": (DOSSIER_TIER if from_dossier
                                            else _tier(item.get("sourceTier"))),
                            "confidence": round(confidence, 2),
                            "justification": (item.get("justification") or "")[:2000]})
                        counts["rescued"] += 1
                        logger.info(
                            "ASSESSMENT INPUTS: rescued numeric %r from "
                            "bands (value=%r).", key, str(raw)[:80])
                        continue
            # Not in anchors, not rescued as numeric — categorise the rejection.
            cat = _all_config.get(key)
            if cat and cat in NOT_RESEARCHABLE:
                counts["excluded_category_rejected"] += 1
                logger.debug(
                    "ASSESSMENT INPUTS: %r belongs to excluded category %s "
                    "— skipped by design.", key, cat)
            else:
                counts["unknown_key_rejected"] += 1
                logger.warning(
                    "ASSESSMENT INPUTS: band key %r is not a requested anchor "
                    "(band=%r). Check prompt/workbook alignment.", key, band)
            continue
        if band not in ("Excellent", "Good", "Fair", "Poor"):
            counts["unparseable_band"] += 1
            continue
        url, detail, sourced = _citation(item, dossier=has_dossier)
        if not sourced:
            counts["unsourced_rejected"] += 1
            continue
        from_dossier = detail == DOSSIER_SOURCE and not url
        if from_dossier:
            counts["dossier_sourced"] += 1
        rows.append({
            "input_key": key, "value": band, "unit": "Band",
            "source_type": "profile" if url else "document",
            "source_url": url,
            "source_detail": detail,
            "source_tier": (DOSSIER_TIER if from_dossier
                            else _tier(item.get("sourceTier"))),
            "confidence": round(_float(item.get("confidence")), 2),
            "justification": (item.get("justification") or "")[:2000]})
        counts["bands"] += 1

    sector = (data.get("sector") or getattr(profile.company, "sector", "")
              or "").strip()
    sub_sector = (data.get("subSector")
                  or getattr(profile.company, "sub_sector", "") or "").strip()
    bench, group, method = benchmark_inputs(sector, sub_sector)
    rows.extend(bench)
    counts["benchmarks"] = len(bench)
    counts["sub_sector_method"] = method
    if sub_sector and method == "unresolved":
        from fundos.assessment.models import SectorMapping
        if SectorMapping.objects.exists():
            logger.warning(
                "ASSESSMENT INPUTS %s: sub-sector %r matched none of the "
                "loaded labels. Category F carries 20%% of the rating and "
                "will score blank — add a SectorMapping row.",
                profile.company_id, sub_sector)
        else:
            logger.warning(
                "ASSESSMENT INPUTS %s: no sub-sector mappings are loaded at "
                "all, so %r could not resolve. Import the workbook's Sector "
                "Deal Data sheet.", profile.company_id, sub_sector)

    counts["missing"] = len(data.get("missing") or [])

    # Rows that are ARITHMETIC are computed, not read. They overwrite whatever
    # the model returned for the same keys, because a ratio of two recorded
    # numbers is not something a model can be more right about than division.
    derived_rows = _derived_rows(profile)
    computed = {r["input_key"] for r in derived_rows}
    from fundos.assessment.derived import DERIVED_KEYS
    # A derived key we could NOT compute must not fall back to the model's
    # answer. Blank excludes the row from the roll-up and redistributes its
    # weight, which is the honest treatment of "we do not know what they are
    # raising". Scoring the model's stand-in put two rows a full band each
    # above the truth on Tyreplex, both of them Excellent.
    dropped = [r["input_key"] for r in rows
               if r["input_key"] in DERIVED_KEYS
               and r["input_key"] not in computed]
    rows = [r for r in rows
            if r["input_key"] not in DERIVED_KEYS
            or r["input_key"] in computed]
    rows.extend(derived_rows)
    counts["derived"] = len(derived_rows)
    counts["derived_blank"] = len(dropped)
    if dropped:
        logger.warning(
            "ASSESSMENT INPUTS %s: %s left blank — they are the ask divided "
            "by history, and no amount being raised is recorded for this "
            "company. The model's stand-in was discarded rather than scored.",
            profile.company_id, ", ".join(sorted(dropped)))

    counts["written"] = _persist(profile, rows)
    counts["sector"] = sector
    counts["subSector"] = sub_sector
    counts["subSectorGroup"] = group or ""
    counts["coveragePct"] = (round(counts["written"] / counts["asked"] * 100, 1)
                             if counts["asked"] else 0)

    logger.info("ASSESSMENT INPUTS %s: %s", profile.company_id, counts)
    return counts


#: The evidence tiers synthesis may report, and the stored source tier each
#: maps to. The same four words `assessment.extraction` asks its own model
#: for, so a parameter carries the same meaning whichever half of the system
#: established it.
#:
#: An allow-list, deliberately: an unrecognised tier lands on `None` and the
#: row is dropped, which narrows the assessment visibly. A deny-list would let
#: a typo through carrying whatever provenance it failed to state.
SYNTHESIS_TIERS = {
    "verified": 2,
    "management": 2,
    "estimate": 4,
    "not evidenced": None,
}


def _synthesis_tier(value):
    """The stored source_tier for a tier reported by synthesis.

    Note the ceiling: research NEVER earns tier 1. That tier means the
    founder's own uploaded documents, and `profile_bridge._outranks` uses it
    to decide which of two answers wins. A figure the synthesis call read on
    the web must not outrank the same figure read out of the financial model,
    however sure it was -- so "Verified" here means verified against research,
    and maps to 2 (company-owned), not to 1.
    """
    text = str(value or "").strip().lower()
    return SYNTHESIS_TIERS.get(text)


def persist_synthesis_inputs(profile, entries, user=None):
    """Store the assessment parameters synthesis reported alongside its sections.

    This is the write half of the "read the dossier once" change. The values
    arrive from `pipeline.synthesize`, co-located with the section whose
    evidence answered them, and land in the same table and with the same
    provenance rules the standalone extraction used -- so everything
    downstream, the bridge included, is unaffected by which call produced
    them.

    Applies the same three rules as `extract_assessment_inputs`, for the same
    reasons: a value must name a source someone can check, a value the model
    could not establish is dropped rather than stored empty, and a founder's
    own correction is never overwritten.

    :param entries: `{input_key: entry}` from `assessment_map.harvest`.
    :returns: A counts dict for the run record.
    """
    from fundos.assessment.models import ConfigParameter

    counts = {"reported": len(entries), "written": 0, "unsourced": 0,
              "untiered": 0, "unknown_band": 0}
    if not entries:
        return counts

    units = {p.input_key: (p.unit or "")
             for p in ConfigParameter.objects.filter(
                 input_key__in=list(entries), is_active=True)}
    anchors = {p.input_key for p in ConfigParameter.objects.filter(
        input_key__in=list(entries), is_active=True, scoring_type="anchor")}

    rows = []
    for key, entry in entries.items():
        tier = _synthesis_tier(entry.get("evidenceTier"))
        if tier is None:
            # Either the model could not establish it, or it answered with
            # something outside the vocabulary. Both mean the row is not
            # evidenced, and an unevidenced row is excluded from scoring and
            # its weight redistributes -- which is the correct outcome, not a
            # loss.
            counts["untiered"] += 1
            continue

        value = entry.get("value")
        if key in anchors:
            band = str(value or "").strip().title()
            if band not in ("Excellent", "Good", "Fair", "Poor"):
                # Exceptional included: that band is override-only, and a
                # model may not mint a 10/10 with no override record.
                counts["unknown_band"] += 1
                continue
            value = band
        value = str(value).strip()
        if not value:
            continue

        url, detail, ok = _citation(entry, dossier=True)
        if not ok:
            counts["unsourced"] += 1
            continue

        section = entry.get("_section") or ""
        if section:
            detail = (f"{detail} [profile section: {section}]"
                      if detail else f"profile section: {section}")

        rows.append({
            "input_key": key,
            "value": value[:255],
            "unit": (str(entry.get("unit") or "") or units.get(key, ""))[:32],
            "source_type": "profile",
            "source_url": url,
            "source_detail": detail[:2000],
            "source_tier": tier,
            "confidence": round(min(max(_float(entry.get("confidence")), 0.0),
                                    1.0), 2),
            "justification": str(entry.get("justification") or "")[:2000],
        })

    counts["written"] = _persist(profile, rows)
    logger.info("ASSESSMENT INPUTS %s (from synthesis): %s",
                getattr(profile, "company_id", "?"), counts)
    return counts


def _derived_rows(profile):
    """The scorecard rows that are arithmetic, computed from what we hold.

    Resolving the three terms is the whole job, and each has one right source:

    * the CURRENT RAISE is the ask — the amount being raised now, which lives
      on the assessment as the operator entered it. It is emphatically not the
      last round that closed, and reading it as such is what put 1.5x and
      39.3% on Tyreplex's scorecard where the truth was 4.2x and 164%;
    * the PREVIOUS ROUND is the most recent closed round that states an
      amount, from the funding table;
    * the TOTAL RAISED is the sum of that table when it is complete, and the
      reported figure only when it is not — said out loud either way.

    Never raises. A company with no recorded ask simply has no derived rows,
    and a blank row is excluded from the roll-up rather than scored as zero.
    """
    from fundos.assessment.derived import build_rows

    try:
        from fundos.profile.spec_serializer import serialize_section
        rounds = (serialize_section(profile, "funding_history").get("data")
                  or [])
    except Exception:                       # pragma: no cover - never fatal
        rounds = []

    reported_total = None
    try:
        overview = serialize_section(profile, "company_profile").get("data")
        reported_total = (overview or {}).get("total_funding_raised_usd_mn")
    except Exception:                       # pragma: no cover
        pass

    current_raise = _current_raise(profile)
    try:
        return build_rows(
            current_raise=current_raise, rounds=rounds,
            reported_total=reported_total,
            input_tiers={"current_raise": 1, "previous_round": 1,
                         "total_raised": 1 if rounds else 3},
            citations={
                "current_raise": {"source": "Operator input",
                                  "locator": "Amount being raised"},
                "previous_round": {"source": "Funding history"},
                "total_raised": {"source": "Funding history"},
            })
    except Exception:                       # pragma: no cover - never fatal
        logger.warning("ASSESSMENT INPUTS %s: derived rows failed",
                       profile.company_id, exc_info=True)
        return []


def _current_raise(profile):
    """The amount being raised now, in US$ millions, or None.

    None is a real answer: a company that has not told us what it is raising
    cannot have its ask scored, and inventing one from the last close is how
    two rows came to flatter a deal by a full band each.
    """
    from fundos.assessment.models import Assessment

    row = (Assessment.objects
           .filter(deal__company_id=profile.company_id)
           .exclude(capital_raised_usd_mn=None)
           .order_by("-created_at").first())
    return row.capital_raised_usd_mn if row else None


def _persist(profile, rows):
    """Write the rows, never overwriting a founder confirmation.

    A founder who corrected a value has given the best signal available; a
    later run silently reverting it would make the correction pointless and
    the score unstable between runs.
    """
    from fundos.profile.models import ProfileAssessmentInput

    confirmed = set(ProfileAssessmentInput.objects.filter(
        profile=profile, is_founder_confirmed=True
    ).values_list("input_key", flat=True))

    written = 0
    for r in rows:
        if r["input_key"] in confirmed:
            continue
        ProfileAssessmentInput.objects.update_or_create(
            profile=profile, input_key=r["input_key"],
            defaults={**r, "tenant_id": getattr(profile, "tenant_id", None)})
        written += 1
    return written


def _citable_sources(payloads):
    """Every source the dossier was built from, uploaded documents first.

    Returns [] when the dossier is absent or nameless — the prompt then
    carries no list and the existing fallback still applies. This adds a
    vocabulary; it does not remove a safety net.
    """
    try:
        from fundos.profile.pipeline.dossier import source_labels

        labels, _documents = source_labels((payloads or {}).get("dossier", ""))
        return labels
    except Exception:                       # pragma: no cover - never fatal
        return []


def _ask(profile, payloads, numeric, anchors, user=None):
    """Three concurrent batch calls, carrying the workbook's guidance per parameter.

    Matching Fundraising Strategy V2.0: Categories are split across 3 focused
    batches run concurrently so the LLM reasons carefully about each parameter
    group without truncating or dropping parameters due to output limits.
    """
    from concurrent.futures import ThreadPoolExecutor
    from fundos.assessment.models import ConfigAnchor
    from fundos.llm.adapter import llm_generate

    defs = {}
    for a in ConfigAnchor.objects.filter(
            input_key__in=[p.input_key for p in anchors],
            is_active=True).order_by("-version"):
        defs.setdefault(a.input_key, {
            "Excellent": a.excellent_def, "Good": a.good_def,
            "Fair": a.fair_def, "Poor": a.poor_def,
            "evidenceRequired": a.evidence_required})

    sectors, groups = vocabulary()
    base_context = {
        "sector_vocabulary": sectors,
        "sub_sector_vocabulary": groups,
        "company": {
            "name": profile.company.name,
            "website": profile.website_url,
            "hqCountry": profile.hq_country,
            "homeCurrency": profile.home_currency,
            "db_sector": getattr(profile.company, "sector", "") or "",
            "db_sub_sector": getattr(profile.company, "sub_sector", "") or "",
        },
        "website_extract": (payloads or {}).get("website", {}),
        "document_extracts": (payloads or {}).get("documents", []),
        "research": (payloads or {}).get("research", {}),
        "founders": (payloads or {}).get("founders", []),
        "dossier": (payloads or {}).get("dossier", "")[:MAX_DOSSIER_CHARS],
        # The vocabulary a citation may name, harvested from the dossier's own
        # headings, uploaded documents first.
        #
        # Without it the model had nothing to quote: what it is handed is
        # 370,000 characters of merged prose, not a set of files, so it named
        # the container — 44 stored rows cite "Consolidated research dossier",
        # which is the library rather than the book. The same list fixed the
        # profile side, where the deck went from 0 citations to 38.
        "citable_sources": _citable_sources(payloads),
    }
    try:
        from fundos.profile.services import judgment_context
        base_context["judgment"] = judgment_context(profile)
    except Exception:
        pass

    # Split parameters into 3 focused batches (matching V2.0):
    # Batch 1: Categories A & D (Team & Deal Dynamics)
    # Batch 2: Categories B & C (Financials & Business Quality)
    # Batch 3: Categories E & F (Sector & Percentile Benchmarks)
    batches_spec = [
        ("people_and_deal", ("A", "D")),
        ("financials_and_quality", ("B", "C")),
        ("market_and_benchmarks", ("E", "F")),
    ]

    def _cat_code(p):
        return getattr(p, "category_code", None) or (p.input_key.split("_")[0] if "_" in p.input_key else "")

    def _run_batch(name, cat_codes):
        b_num = [p for p in numeric if _cat_code(p) in cat_codes or (name == "market_and_benchmarks" and _cat_code(p) not in ("A", "B", "C", "D"))]
        b_anc = [p for p in anchors if _cat_code(p) in cat_codes or (name == "market_and_benchmarks" and _cat_code(p) not in ("A", "B", "C", "D"))]
        if not b_num and not b_anc and (numeric or anchors):
            return {"values": [], "bands": []}

        ctx = {
            **base_context,
            "batch_name": name,
            "parameters": [{
                "inputKey": p.input_key, "parameter": p.name, "unit": p.unit,
                "definition": p.definition, "whereToFind": p.where_to_find,
                "ifMissing": p.if_missing} for p in b_num],
            "qualitative_parameters": [{
                "inputKey": p.input_key, "parameter": p.name,
                "definition": p.definition, "ifMissing": p.if_missing,
                "bands": defs.get(p.input_key, {})} for p in b_anc],
        }
        logger.info("ASSESSMENT EXTRACTION: starting batch '%s' (%d numeric, %d anchors asked)...",
                    name, len(b_num), len(b_anc))
        raw = llm_generate(role="assessment_inputs", system=EXTRACTION_SYSTEM,
                           context=ctx, user=user,
                           calling_context=f"profile.assessment_inputs.{name}")
        coerced = _coerce(raw) or {}
        vals = coerced.get("values") or []
        bands = coerced.get("bands") or []
        logger.info("ASSESSMENT EXTRACTION: batch '%s' completed -> returned %d values, %d bands.",
                    name, len(vals), len(bands))
        return {"values": vals, "bands": bands}

    all_values = []
    all_bands = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(_run_batch, b_name, b_cats)
                   for b_name, b_cats in batches_spec]
        for f in futures:
            try:
                res = f.result()
                all_values.extend(res.get("values") or [])
                all_bands.extend(res.get("bands") or [])
            except Exception as e:
                logger.error("ASSESSMENT EXTRACTION batch execution failed: %s", e, exc_info=True)

    logger.info("ASSESSMENT EXTRACTION (consolidated 3 batches): %d total values, %d total bands returned.",
                len(all_values), len(all_bands))
    return {"values": all_values, "bands": all_bands}


def _float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _tier(value):
    try:
        tier = int(value)
    except (TypeError, ValueError):
        return 3
    return tier if 1 <= tier <= 4 else 3


def _coerce(raw):
    """Accept the adapter's normalised shape or a raw JSON string."""
    import json

    if isinstance(raw, dict):
        if isinstance(raw.get("content"), dict):
            return raw["content"]
        if isinstance(raw.get("content"), str):
            raw = raw["content"]
        else:
            return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip().strip("`")
    if s.lower().startswith("json"):
        s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None
