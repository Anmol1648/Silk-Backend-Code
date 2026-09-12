"""Accepting a sector nobody curated, without inventing a benchmark cohort.

A founder whose industry is not in the list of 60 has to put something in
the box. Blocking them produces the worst outcome available: they pick the
nearest value that lets the form submit, and the profile carries a label
that is wrong and looks deliberate. So a new label is ACCEPTED.

What it is not allowed to do is create a peer group.

THREE TABLES, THREE DIFFERENT JOBS
----------------------------------
    lookup_sector / lookup_sub_sector   the picklists a person chooses from
    SectorDealData                      the benchmark cohorts -- deal counts,
                                        average ticket, investor counts
    SectorMapping                       "this label means that cohort"

Adding to the first is housekeeping. Adding to the second would be
fabrication: a row created here holds no deals, so Category F -- a fifth of
the rating -- would score a company against an empty cohort while looking
calculated. Blank is the honest answer, and the roll-up already handles it
by redistributing the weight.

The useful question a new label raises is almost never "is this a new
industry?" but "which of our existing groups is this?". `Digital Health`
was not a missing sector; it was Healthtech under another name. No string
comparison settles that. A person settles it in two seconds, and this module
exists to put it in front of them.
"""
import logging

logger = logging.getLogger("fundos.profile")

#: What `record` did, for a caller that wants to log or surface it.
MATCHED = "matched"
ADDED = "added"
IGNORED = "ignored"


def _clean(value):
    return " ".join(str(value or "").split())[:100]


def _existing(model, name):
    """The row whose name matches, ignoring case and surrounding space."""
    return model.objects.filter(name__iexact=name).first()


def record_sector(name, *, company=""):
    """Ensure a macro sector exists in the picklist. Returns (row, outcome)."""
    from fundos.platformcfg.lookup_models import Sector

    clean = _clean(name)
    if not clean:
        return None, IGNORED

    found = _existing(Sector, clean)
    if found:
        return found, MATCHED

    row = Sector.objects.create(name=clean, is_active=True, is_pending=True,
                                added_from=str(company or "")[:255])
    logger.info("TAXONOMY: macro sector %r added from %s, pending review.",
                clean, company or "an unnamed company")
    return row, ADDED


def record_sub_sector(name, *, sector="", company=""):
    """Ensure a sub-sector exists in the picklist. Returns (row, outcome).

    The parent sector is recorded where one is known, because `SubSector`
    carries a foreign key to it and a sub-sector filed under nothing is
    harder to review than one filed under its industry.

    NEVER touches `SectorDealData`. A benchmark cohort is not something this
    can conjure: see the module docstring.
    """
    from fundos.platformcfg.lookup_models import SubSector

    clean = _clean(name)
    if not clean:
        return None, IGNORED

    found = _existing(SubSector, clean)
    if found:
        return found, MATCHED

    parent, _ = record_sector(sector, company=company) if sector \
        else (None, IGNORED)
    row = SubSector.objects.create(name=clean, sector=parent, is_active=True,
                                   is_pending=True,
                                   added_from=str(company or "")[:255])
    logger.info("TAXONOMY: sub-sector %r added from %s, pending review — "
                "its benchmark group is unknown until someone maps it, so "
                "the benchmark category scores blank in the meantime.",
                clean, company or "an unnamed company")
    return row, ADDED


def record(macro_sector, sub_sector, *, company=""):
    """Accept whatever a profile says its industry is.

    Returns ``{"sector": outcome, "sub_sector": outcome}`` so a caller can
    report what happened without reading the database again. Never raises:
    a taxonomy row is not worth failing a generation run over.
    """
    out = {"sector": IGNORED, "sub_sector": IGNORED}
    try:
        if macro_sector:
            out["sector"] = record_sector(macro_sector, company=company)[1]
        if sub_sector:
            out["sub_sector"] = record_sub_sector(
                sub_sector, sector=macro_sector, company=company)[1]
    except Exception as exc:                # pragma: no cover - never fatal
        logger.warning("TAXONOMY: could not record %r/%r: %s",
                       macro_sector, sub_sector, exc)
    return out


def pending():
    """Every value awaiting review, newest question first.

    A sub-sector comes with the one question that matters — which benchmark
    group it belongs to — and the groups available to answer it with.
    """
    from fundos.assessment.models import SectorDealData
    from fundos.platformcfg.lookup_models import Sector, SubSector

    groups = sorted({row.clubbed_group for row in SectorDealData.objects
                     .filter(level="sub_sector").exclude(clubbed_group="")
                     if row.clubbed_group})
    return {
        "sectors": [{"id": row.id, "name": row.name,
                     "addedFrom": row.added_from}
                    for row in Sector.objects.filter(is_pending=True)],
        "subSectors": [{"id": row.id, "name": row.name,
                        "sector": row.sector.name if row.sector else "",
                        "addedFrom": row.added_from,
                        "question": f"Which benchmark group is "
                                    f"{row.name!r}?"}
                       for row in SubSector.objects.filter(is_pending=True)],
        "benchmarkGroups": groups,
    }


class TaxonomyError(Exception):
    """The review could not be applied, with a reason for the caller."""


def resolve_sub_sector(name, group, *, user=None):
    """Record which benchmark group a pending sub-sector belongs to.

    Writes the alias BOTH resolvers read, so the profile and the scorecard
    cannot disagree about it afterwards, and clears the pending mark.

    `group` must be a cohort that exists. Accepting a name nobody has deal
    data for would put the company back where it started — scored against
    nothing — with the gap now hidden behind a confirmation.
    """
    from fundos.assessment.models import SectorDealData, SectorMapping
    from fundos.platformcfg.lookup_models import SubSector

    label = _clean(name)
    target = _clean(group)
    if not label or not target:
        raise TaxonomyError("Name the sub-sector and the group it belongs to.")

    row = _existing(SubSector, label)
    if row is None:
        raise TaxonomyError(f"{label!r} is not in the sub-sector list.")

    known = SectorDealData.objects.filter(
        level="sub_sector", clubbed_group__iexact=target).first()
    if known is None:
        raise TaxonomyError(
            f"{target!r} is not a benchmark group with deal data behind it. "
            f"Pick one that is, or the category still scores against "
            f"nothing.")

    SectorMapping.objects.update_or_create(
        raw_label=label, defaults={"clubbed_group": known.clubbed_group})
    row.is_pending = False
    row.save(update_fields=["is_pending"])
    logger.info("TAXONOMY: %r mapped to benchmark group %r by %s.", label,
                known.clubbed_group, getattr(user, "email", "an admin"))
    return {"subSector": label, "group": known.clubbed_group}


def approve_sector(name):
    """Accept a pending macro sector as curated. No cohort is implied."""
    from fundos.platformcfg.lookup_models import Sector

    row = _existing(Sector, _clean(name))
    if row is None:
        raise TaxonomyError(f"{name!r} is not in the sector list.")
    row.is_pending = False
    row.save(update_fields=["is_pending"])
    return {"sector": row.name}
