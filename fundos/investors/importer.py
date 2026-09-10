"""Bulk import of the supplied investor workbook.

This is the seed corpus: 7,777 investor-deal rows across 3,454 investors,
already classified and already carrying the relationship data. Importing it
first means Stage 3 has a complete dataset on day one and the refresh pipeline
becomes an incremental problem rather than a bootstrap problem.

IDEMPOTENT. Re-running updates rather than duplicating, keyed on the deal
dedup key and the investor's normalised name. Relationship fields are written
on FIRST import only — after that the database is authoritative for them,
because the deal team will have edited them and the spreadsheet will not have.
"""
import datetime as dt
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction

logger = logging.getLogger(__name__)

SHEET = "Consol - Investors"
HEADER_ROW = 2

# Workbook column letter → our field. Declared rather than positional so a
# column inserted in the spreadsheet does not silently shift everything.
COLUMNS = {
    "C": "investor_name",
    "E": "category",
    "I": "hq_city",
    "J": "hq_country",
    "L": "has_local_office",
    "G": "single_sector_focused",
    "H": "is_impact",
    "P": "avg_ticket_usd_mn",
    "U": "count_of_investments",
    "V": "deal_size_usd_mn",
    "Z": "company_name",
    "AA": "sector",
    "AB": "sub_sector",
    "AC": "total_investors_in_deal",
    "AD": "investors_in_deal",
    "AE": "fundraise_per_investor",
    "AL": "deal_date",
    # Relationship block — first import only.
    "D": "rel_internal_name",
    "AF": "rel_met",
    "AG": "rel_relationship",
    "AH": "rel_contact_name",
    "AI": "rel_mobile",
    "AM": "rel_email_flag",
    "AN": "rel_email_1",
    "AO": "rel_email_2",
    "AP": "rel_email_3",
    "AQ": "rel_email_4",
    "AR": "rel_email_5",
}


def _col_index(letter):
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _dec(value):
    if value in (None, "", "-"):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _bool(value):
    if value is None or value == "":
        return None
    s = str(value).strip().lower()
    if s in ("yes", "y", "true", "1"):
        return True
    if s in ("no", "n", "false", "0"):
        return False
    return None


def _date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    from fundos.investors.pipeline import parse_date
    return parse_date(value)


def import_workbook(path, *, tenant_id=None, import_relationships=True,
                    dry_run=False, progress=None):
    """Import the workbook. Returns a counts dict.

    A thin wrapper that exists only to guarantee the file is closed. A
    `read_only` workbook holds the .xlsx zip archive open until told otherwise,
    so a missing sheet or a bad row part-way through would leak the handle —
    and on Windows hold a lock that stops the caller deleting the upload it was
    given. The import itself is :func:`_import_from_workbook`.
    """
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return _import_from_workbook(
            wb, tenant_id=tenant_id,
            import_relationships=import_relationships,
            dry_run=dry_run, progress=progress)
    finally:
        wb.close()


def _import_from_workbook(wb, *, tenant_id=None, import_relationships=True,
                          dry_run=False, progress=None):
    """The import proper. Never closes `wb` — its caller owns the handle."""
    from fundos.investors import identity, normalizer
    from fundos.investors.models import (CanonicalSector, FundingDeal,
                                         Investor, InvestorCategory,
                                         InvestorDeal)
    from fundos.investors.pipeline import dedup_key

    if SHEET not in wb.sheetnames:
        raise ValueError(
            f"Sheet {SHEET!r} not found. Sheets present: {wb.sheetnames}")
    ws = wb[SHEET]

    idx = {field: _col_index(letter) for letter, field in COLUMNS.items()}
    counts = {"rows": 0, "investors_created": 0, "deals_created": 0,
              "investor_deals": 0, "skipped": 0, "sector_unmapped": 0}

    categories = {c.name.strip().lower(): c
                  for c in InvestorCategory.objects.all()}
    sectors_seen, deal_cache, investor_cache = {}, {}, {}

    def resolve_sector(raw, level):
        """Exact match, or create. DELIBERATELY NOT FUZZY.

        Fuzzy matching is right for a live scrape, where the source spells
        things inconsistently and we want "Artficial Intelligence (AI)" to
        land on the canonical row. It is wrong here.

        On import the workbook IS the taxonomy: its sub-sector labels are
        deliberate distinctions, and rapidfuzz cheerfully scores
        "D2C (F&B)" against "D2C" above threshold. Collapsing them would
        merge three genuinely different sub-sectors into one and change every
        downstream match count. So import takes the labels literally and
        creates what it does not have.
        """
        if not raw:
            return None
        name = str(raw).strip()
        key = (name.lower(), level)
        if key in sectors_seen:
            return sectors_seen[key]
        if dry_run:
            sectors_seen[key] = None
            return None

        sector = CanonicalSector.objects.filter(name__iexact=name,
                                                level=level).first()
        if sector is None:
            sector = CanonicalSector.objects.create(name=name[:255],
                                                    level=level)
            counts["sector_created"] = counts.get("sector_created", 0) + 1
            normalizer.invalidate()
        sectors_seen[key] = sector
        return sector

    for n, row in enumerate(ws.iter_rows(min_row=HEADER_ROW + 1,
                                         values_only=True)):
        def val(field):
            i = idx.get(field)
            return row[i] if i is not None and i < len(row) else None

        investor_name = val("investor_name")
        if not investor_name or not str(investor_name).strip():
            counts["skipped"] += 1
            continue
        counts["rows"] += 1

        if progress and counts["rows"] % 500 == 0:
            progress(counts)

        # -- investor ------------------------------------------------------
        iname = str(investor_name).strip()
        ikey = identity.normalise_name(iname)
        investor = investor_cache.get(ikey)
        if investor is None and not dry_run:
            investor = Investor.objects.filter(normalised_name=ikey,
                                               tenant_id=tenant_id).first()
            if investor is None:
                cat = categories.get(str(val("category") or "").strip().lower())
                fields = dict(
                    name=iname[:255], normalised_name=ikey,
                    tenant_id=tenant_id, category=cat,
                    hq_city=str(val("hq_city") or "")[:128],
                    hq_country=str(val("hq_country") or "")[:128],
                    has_local_office=_bool(val("has_local_office")),
                    single_sector_focused=_bool(val("single_sector_focused")),
                    is_impact=_bool(val("is_impact")),
                    classification_source="import",
                    avg_ticket_usd_mn=_dec(val("avg_ticket_usd_mn")))
                if import_relationships:
                    emails = [str(val(f"rel_email_{i}")).strip()
                              for i in range(1, 6)
                              if val(f"rel_email_{i}")]
                    fields.update(
                        rel_internal_name=str(val("rel_internal_name") or "")[:255],
                        rel_met=_bool(val("rel_met")),
                        rel_relationship=str(val("rel_relationship") or "")[:64],
                        rel_contact_name=str(val("rel_contact_name") or "")[:255],
                        rel_mobile=str(val("rel_mobile") or "")[:64],
                        rel_emails=emails)
                investor = Investor.objects.create(**fields)
                counts["investors_created"] += 1
            investor_cache[ikey] = investor
        if investor is None:
            continue

        # -- deal ----------------------------------------------------------
        company = str(val("company_name") or "").strip()
        deal_date = _date(val("deal_date"))
        amount = _dec(val("deal_size_usd_mn"))
        if not company:
            continue

        dkey = dedup_key(company, deal_date, amount)
        deal = deal_cache.get(dkey)
        if deal is None and not dry_run:
            sector = resolve_sector(val("sector"), "sector")
            sub = resolve_sector(val("sub_sector"), "sub_sector")
            if sub and sector and sub.parent_id is None:
                sub.parent = sector
                sub.save(update_fields=["parent"])
            deal, created = FundingDeal.objects.get_or_create(
                dedup_key=dkey,
                defaults=dict(
                    source="workbook_import", company_name=company[:255],
                    deal_date=deal_date, amount_usd_mn=amount,
                    raw_sector=str(val("sector") or "")[:255],
                    raw_sub_sector=str(val("sub_sector") or "")[:255],
                    sector=sector, sub_sector=sub,
                    iso_week=deal_date.isocalendar()[1] if deal_date else None,
                    iso_year=deal_date.isocalendar()[0] if deal_date else None))
            if created:
                counts["deals_created"] += 1
            deal_cache[dkey] = deal
        if deal is None:
            continue

        # -- investor-deal row ---------------------------------------------
        if not dry_run:
            _, made = InvestorDeal.objects.get_or_create(
                investor=investor, funding_deal=deal,
                defaults=dict(
                    tenant_id=tenant_id, deal_date=deal_date,
                    deal_size_usd_mn=amount, sector=deal.sector,
                    sub_sector=deal.sub_sector, company_name=company[:255],
                    investors_in_deal=int(val("investors_in_deal") or 1),
                    fundraise_per_investor=_dec(val("fundraise_per_investor"))))
            if made:
                counts["investor_deals"] += 1

    if not dry_run:
        from fundos.investors.pipeline import (recompute_investor_aggregates,
                                               rebuild_sector_deal_data)
        counts["aggregates_recomputed"] = recompute_investor_aggregates()
        counts["sector_rows"] = rebuild_sector_deal_data()

    logger.info("IMPORT: %s", counts)
    return counts
