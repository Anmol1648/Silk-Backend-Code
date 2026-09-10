"""Phase 3 — the profile-to-assessment bridge, step 2 half.

Step 1 emits typed values under the workbook's own `input_key` names
(`fundos/profile/assessment_extraction.py`). This module reads them into
`ParameterValue` rows, and it is the only place the two halves meet.

PRECEDENCE, AND WHY IT IS NOT "NEWEST WINS"
-------------------------------------------
Two sources can answer the same parameter. Revenue appears in the founder's
financial model and on a press page; headcount appears on LinkedIn and in the
deck. When they disagree, the rule is SOURCE TIER, not recency and not
confidence:

    1  the founder's own uploaded documents
    2  the company's own website, filings and LinkedIn
    3  reputable third parties — press, databases, registries
    4  inferred or derived

A model can be highly confident about a figure it read in a news article and
still be quoting a number the company's own audited model contradicts. Ranking
by confidence would let the article win, which is exactly backwards. Tier
first, confidence only to break a tie within a tier.

A founder confirmation outranks everything, including tier 1, because a human
who has looked at the number and said "no, it is this" is the best source
available and the whole point of showing them the value.

BLANK MUST STAY BLANK
---------------------
The scoring model excludes an unanswered parameter from the denominator and
redistributes its weight (§2.3.1). That only works if "we could not find it"
is stored as absence. So this seeds ONLY what it has: no zero-filling, no
"0 patents" for a company nobody researched patents for. A zero is a claim,
and an unmade claim must not look like one.
"""
import logging

logger = logging.getLogger(__name__)

# Tier ordering — lower wins. Mirrors the profile side's constants.
TIER_BEST = 1
TIER_WORST = 4


def seed_from_profile(assessment, *, profile=None):
    """Seed this assessment's ParameterValue rows from step 1 research.

    Runs BEFORE document extraction so documents, which are tier 1, overwrite
    research where they overlap. Returns a counts dict; never raises, because
    an assessment built from documents alone is still a usable assessment and
    losing it to a bridge failure would be a poor trade.
    """
    from fundos.assessment.models import ConfigParameter, ParameterValue

    counts = {"available": 0, "seeded": 0, "skipped_unknown": 0,
              "skipped_blank": 0, "bands": 0}

    profile = profile or _profile_for(assessment)
    if profile is None:
        logger.info(
            "BRIDGE %s: no company profile exists for this company, so step "
            "1 research contributed nothing. The assessment will score from "
            "uploaded documents alone.", assessment.id)
        # The deal-table percentiles are keyed on the assessment's own
        # cohort, not on the profile, so they are still available here.
        counts["benchmarks"] = refresh_benchmarks(assessment)
        return counts

    rows = list(profile.assessment_inputs.all())
    counts["available"] = len(rows)
    if not rows:
        logger.info(
            "BRIDGE %s: the profile has no assessment inputs. Regenerate the "
            "company profile to populate them.", assessment.id)
        _apply_sector(assessment, profile)
        counts["benchmarks"] = refresh_benchmarks(assessment)
        return counts

    tenant_id = getattr(assessment, "tenant_id", None)
    config = _config_by_key(tenant_id)

    for row in rows:
        cfg = config.get(row.input_key)
        if cfg is None:
            # A key the scoring model does not declare. Worth logging rather
            # than dropping silently: it usually means the workbook moved and
            # the prompt did not, and that is a config fix, not a data fix.
            counts["skipped_unknown"] += 1
            logger.debug("BRIDGE %s: %s is not a configured parameter.",
                         assessment.id, row.input_key)
            continue
        if not str(row.value or "").strip():
            counts["skipped_blank"] += 1
            continue

        is_band = (row.unit or "").lower() == "band"
        defaults = dict(
            ref_code=cfg.ref_code or "",
            category=cfg.category_code or "",
            raw_value=("" if is_band else str(row.value)[:255]),
            unit=("" if is_band else (row.unit or cfg.unit or "")[:32]),
            source_type=row.source_type or "profile",
            source_detail=_detail(row),
            source_tier=row.source_tier,
            confidence=row.confidence,
            justification=(row.justification or "")[:2000],
        )
        if is_band:
            # Anchor rows arrive pre-banded. band_parameter() re-validates the
            # band against the stored definitions on every scoring run, so a
            # wrong one is caught by config rather than trusted from research.
            defaults["band"] = row.value
            counts["bands"] += 1

        ParameterValue.objects.update_or_create(
            assessment=assessment, input_key=row.input_key,
            defaults=defaults)
        counts["seeded"] += 1

    _apply_sector(assessment, profile)
    # AFTER the sector is resolved, never before: the six percentiles are
    # looked up by the cohort `_apply_sector` just established.
    counts["benchmarks"] = refresh_benchmarks(assessment)
    logger.info("BRIDGE %s: %s", assessment.id, counts)
    return counts


def refresh_benchmarks(assessment):
    """Re-derive the six deal-table percentiles for this assessment.

    E.2/E.3/E.6 and F.2/F.3/F.6 are not research and not a founder's answer.
    They are a lookup of this company's cohort in `SectorDealData`, and the
    table is the source of truth for them — so they are re-derived here on
    every seed rather than being frozen at whatever the table held on the day
    step 1's extraction happened to run.

    That freezing was the defect. `benchmark_inputs` was called in exactly
    one place, inside the profile extraction, and its answer was stored on
    the profile. Import the deal table afterwards — which is the documented
    way to load it, and the documented way to refresh it — and every
    assessment already extracted kept six blank rows while the table held
    every number they needed. Thirty percent of the model, silently
    unscoreable, with the data sitting in the database.

    Re-derives rather than merges: a percentile has no competing source to
    lose to, and a stale one has no claim over the current table. A row a
    reviewer overrode by hand is left alone, as everywhere else.

    :returns: the number of rows written.
    """
    from fundos.assessment.models import ParameterValue
    from fundos.profile.assessment_extraction import resolve_benchmarks

    rows, group, method, absences = resolve_benchmarks(
        assessment.sector or "", assessment.sub_sector or "")

    config = _config_by_key(getattr(assessment, "tenant_id", None))
    overridden = set(ParameterValue.objects.filter(
        assessment=assessment, is_overridden=True
    ).values_list("input_key", flat=True))

    written = 0
    for row in rows:
        if row["input_key"] in overridden:
            continue
        cfg = config.get(row["input_key"])
        ParameterValue.objects.update_or_create(
            assessment=assessment, input_key=row["input_key"],
            defaults=dict(
                ref_code=(cfg.ref_code if cfg else ""),
                category=(cfg.category_code if cfg else ""),
                raw_value=str(row["value"])[:255],
                unit=(row.get("unit") or "")[:32],
                source_type=row["source_type"],
                source_detail=row["source_detail"][:2000],
                source_tier=row["source_tier"],
                confidence=row["confidence"],
                justification=row["justification"][:2000]))
        written += 1

    if absences:
        logger.info(
            "BRIDGE %s: %d benchmark percentile(s) withheld (%s); cohort "
            "%r resolved by %s.", assessment.id, len(absences),
            ", ".join(sorted({r for r, _s in absences.values()})),
            group or assessment.sub_sector, method)
    return written


def merge_value(assessment, input_key, *, raw_value, unit="", source_type,
                source_detail="", source_tier=None, confidence=None,
                justification="", band=None):
    """Write a value only if it beats what is already there on source tier.

    This is the function document extraction and any later source should go
    through, so precedence is decided in ONE place rather than by whichever
    writer happened to run last.

    Returns True when the value was written.
    """
    from fundos.assessment.models import ParameterValue

    existing = ParameterValue.objects.filter(
        assessment=assessment, input_key=input_key).first()

    if existing is not None and not _outranks(
            source_tier, confidence, source_type, existing.source_tier,
            existing.confidence, existing.source_type):
        logger.debug(
            "BRIDGE %s: kept %s from %s (tier %s) over incoming %s (tier %s).",
            assessment.id, input_key, existing.source_type,
            existing.source_tier, source_type, source_tier)
        return False

    final_detail = (source_detail or "")[:2000]
    if existing and existing.source_detail and existing.source_detail.strip():
        old_det = existing.source_detail.strip()
        if old_det not in final_detail:
            final_detail = f"{final_detail}; {old_det}"[:2000]

    defaults = dict(raw_value=str(raw_value)[:255] if raw_value else "",
                    unit=(unit or "")[:32], source_type=source_type,
                    source_detail=final_detail,
                    source_tier=source_tier, confidence=confidence,
                    justification=(justification or "")[:2000])
    if band:
        defaults["band"] = band
    ParameterValue.objects.update_or_create(
        assessment=assessment, input_key=input_key, defaults=defaults)
    return True


def _outranks(new_tier, new_conf, new_source_type, old_tier, old_conf, old_source_type):
    """True when a new value should replace the stored one.

    Precedence rules:
    1. Founder confirmation (source_type == "founder") outranks everything.
    2. Uploaded documents (source_type == "document") unconditionally outrank web research (source_type == "profile" / "web" / "research").
    3. Source tier (lower number wins).
    4. Confidence score breaks ties within the same tier.
    """
    if old_source_type == "founder":
        return False
    if new_source_type == "founder":
        return True

    # Uploaded document unconditionally outranks web research / profile
    norm_new = str(new_source_type or "").lower()
    norm_old = str(old_source_type or "").lower()
    if norm_new == "document" and norm_old in ("profile", "web", "research"):
        return True
    if norm_old == "document" and norm_new in ("profile", "web", "research"):
        return False

    new_tier = new_tier if new_tier is not None else TIER_WORST
    old_tier = old_tier if old_tier is not None else TIER_WORST
    if new_tier != old_tier:
        return new_tier < old_tier
    # Same tier: confidence breaks the tie, and equal confidence keeps the
    # incumbent so a re-run does not churn values for no reason.
    return float(new_conf or 0) > float(old_conf or 0)


def _detail(row):
    parts = []
    if row.source_url:
        parts.append(row.source_url)
    if row.source_detail:
        parts.append(row.source_detail)
    if row.is_founder_confirmed:
        parts.append("Confirmed by the founder.")
    return " — ".join(parts)[:2000] or "Company profile research"


def _config_by_key(tenant_id):
    from fundos.assessment.models import ConfigParameter
    base = {p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id__isnull=True, is_active=True)}
    base.update({p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id=tenant_id, is_active=True)})
    return base


def _sector_from_profile(profile):
    """The researched macro-sector and sub-sector, as step 1 stored them.

    Step 1 writes both into the `company_profile` section's `structured` blob
    (`spec_serializer` reads them straight back out for the profile screen).
    Nothing copies them onto `Company`, so this section IS the source — read
    it here rather than leaving the bridge to look for a field that does not
    exist.

    Returns ("", "") for anything unreadable. A missing sector must leave
    categories E and F blank so their weight redistributes; inventing one
    would score 30% of the rating against the wrong benchmark population.
    """
    if profile is None:
        return "", ""
    from fundos.profile.models import ProfileSection

    # `company_overview` is the older key for the same section. Both are
    # matched, and the rows are read in preference order rather than taking
    # whatever `.first()` happens to return — with two candidate keys and no
    # ordering, which one answers is left to the database.
    rows = {s.section_key: s for s in ProfileSection.all_objects.filter(
        profile=profile,
        section_key__in=["company_profile", "company_overview"],
        is_active=True, is_deleted=False)}

    for key in ("company_profile", "company_overview"):
        structured = getattr(rows.get(key), "structured", None)
        if not isinstance(structured, dict):
            continue
        sector = structured.get("macro_sector") or structured.get("sector") or ""
        sub = structured.get("sub_sector") or ""
        if sector or sub:
            return sector, sub
    return "", ""


def _apply_sector(assessment, profile):
    """Carry the researched sector / sub-sector onto the assessment.

    Recorded on the assessment because the sub-sector decides which of the 33
    benchmark groups categories E and F are scored against — a fifth of the
    rating — and a scorecard that does not say which population it compared
    against is not auditable.
    """
    from fundos.profile.assessment_extraction import resolve_sub_sector

    company = assessment.company
    # `Company` carries only name, domain and hq_country — there has never
    # been a `sector` field on it, so the getattr below is always the empty
    # default. The researched sector lives in the profile's company_profile
    # section, which is why this function is handed the profile at all.
    profile_sector, profile_sub = _sector_from_profile(profile)
    sector = (assessment.sector or getattr(company, "sector", "")
              or profile_sector or "")
    sub = (assessment.sub_sector or getattr(company, "sub_sector", "")
           or profile_sub or "")
    if not (sector or sub):
        return

    group, method = resolve_sub_sector(sub)
    changed = []
    if sector and assessment.sector != sector:
        assessment.sector = sector
        changed.append("sector")
    resolved = group or sub
    if resolved and assessment.sub_sector != resolved:
        assessment.sub_sector = resolved
        changed.append("sub_sector")
    if changed:
        assessment.save(update_fields=changed)
    if sub and method == "unresolved":
        logger.warning(
            "BRIDGE %s: sub-sector %r matched no benchmark group. Category F "
            "carries 20%% of the rating and will score blank — add a "
            "SectorMapping row for this label.", assessment.id, sub)


def _profile_for(assessment):
    from fundos.profile.models import CompanyProfile
    return CompanyProfile.objects.filter(company=assessment.company).first()
