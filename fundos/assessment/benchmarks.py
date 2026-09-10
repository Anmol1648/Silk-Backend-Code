"""Sector / sub-sector benchmark ingestion (§2.7).

The deal-assessment workbook carries a `Sector Deal Data` sheet: how many
deals closed in each sector, how much was raised, the average ticket and how
many distinct investors participated. Categories E (Sector, 10%) and F
(Sub-Sector, 20%) are scored against it, so between them a third of the deal
rating rests on this one table.

It is DATA, not configuration. Adding a sector is a row, and a refresh is a
re-import — never a deploy.

WHY THE SCORES ARE STORED, NOT COMPUTED ON READ
-----------------------------------------------
Velocity, ticket and investor scores are percentile ranks: a row's score
depends on every other row at the same level. Computing them at read time
means a later partial refresh silently re-scores rows nobody touched. They
are therefore ranked once, at import, over the whole population being
loaded, and written down.

RE-IMPORT UPDATES IN PLACE
--------------------------
A refresh is meant to move existing ratings — newer market data should
change what "fast-moving sector" means. So rows are updated where they
match on (level, name) and the whole population is re-ranked together.
`--dry-run` reports exactly which rows would move, and by how much, before
anything is written.
"""
import datetime as dt
import logging
import re
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

SHEET_NAME = "Sector Deal Data"

# Header text in the workbook -> our field. Matching is on a normalised form
# (lowercase, alphanumerics only), so "# Deals", "#Deals" and "Deals" all
# land on the same field and a re-export with cosmetic header changes does
# not break the import.
SECTOR_COLUMNS = {
    # Block 3 carries the CLUBBED SUB-SECTOR groups and is read with this
    # same spec, so the natural headers for that block belong here too.
    # Without them a sheet headed "Clubbed Sub-Sector" detected every column
    # except the name, and `_read` returns nothing at all when the name is
    # missing — so block 3 imported ZERO rows and said so only as a quiet
    # "0 rows" in the report. Category F then scored against an empty table
    # while every other number on the import looked correct.
    "name": ("name", "sector", "sectorname", "subsector", "clubbedsubsector",
             "clubbedgroup", "clubbed", "group"),
    "deal_count": ("deals", "dealcount", "nodeals", "numberofdeals"),
    "total_raised_usd_mn": ("raisedmn", "raised", "totalraised",
                            "raisedusdmn", "amountraised"),
    "avg_ticket_usd_mn": ("avgticketmn", "avgticket", "averageticket",
                          "avgticketusdmn"),
    "investor_count": ("investors", "investorcount", "noinvestors",
                       "numberofinvestors"),
    "sample_quality": ("samplequality", "quality"),
    "velocity_score": ("velocityscore",),
    "ticket_score": ("ticketscore",),
    "investors_score": ("investorsscore", "investorscore"),
}

SUB_SECTOR_COLUMNS = {
    "name": ("rawsubsector", "subsector", "rawlabel", "name"),
    "deal_count": ("deals", "dealcount", "nodeals"),
    "total_raised_usd_mn": ("raisedmn", "raised", "totalraised"),
    "investor_count": ("investors", "investorcount", "noinvestors"),
    "clubbed_group": ("clubbedgroup", "group", "clubbed"),
}


def normalise_header(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _to_decimal(value):
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = re.sub(r"[^\d.\-]", "", str(value))
    if not text or text in ("-", ".", "-."):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _to_int(value):
    d = _to_decimal(value)
    return int(d) if d is not None else None


def detect_columns(header_row, spec):
    """Map worksheet columns to fields. Returns {field: column_index}.

    Returned rather than applied so an administrator can review and correct
    it before the import runs — the header text in a hand-maintained
    workbook is the least reliable thing about it.
    """
    found = {}
    for index, cell in enumerate(header_row):
        key = normalise_header(cell)
        if not key:
            continue
        for field, candidates in spec.items():
            if field in found:
                continue
            if key in candidates or any(key.startswith(c) for c in candidates):
                found[field] = index
                break
    return found


def _min_deals():
    """The sheet's minimum sample for a percentile to mean anything."""
    try:
        from fundos.assessment.services import constant
        return int(constant("min_deals_for_percentile", 5) or 5)
    except Exception:
        return 5


def _percentile_scores(values):
    """Rank a list of values to 0-10, highest value scoring highest.

    Ties share a rank, and a missing value scores nothing at all rather than
    scoring zero — the same "blank is not zero" rule the rest of the model
    runs on. A single-row population gets no score: a percentile against
    yourself is not information.
    """
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    out = [None] * len(values)
    if len(present) < 2:
        return out
    ordered = sorted(present, key=lambda pair: pair[1])
    ranks, position = {}, 0
    while position < len(ordered):
        end = position
        while (end + 1 < len(ordered)
               and ordered[end + 1][1] == ordered[position][1]):
            end += 1
        share = (position + end) / 2.0
        for slot in range(position, end + 1):
            ranks[ordered[slot][0]] = share
        position = end + 1
    top = len(present) - 1
    for index, rank in ranks.items():
        out[index] = round(Decimal(rank) / Decimal(top) * 10, 2)
    return out


def _sheet_rows(path, sheet_name):
    """Read one sheet fully into memory, then close the file.

    Everything below works on the resulting list, so the workbook has no reason
    to stay open past this point — and a `read_only` workbook holds the .xlsx
    zip archive open until it is closed explicitly. Leaving it open leaks a file
    handle per import and, on Windows, holds a lock that stops the caller
    deleting the upload it just parsed.

    The sheet check happens here, inside the `finally`, so the "no such sheet"
    error does not itself leak the handle it was raised over.
    """
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            raise ValueError(
                f"the workbook has no sheet named {sheet_name!r}. "
                f"Sheets present: {', '.join(wb.sheetnames)}")
        return [[c.value for c in row] for row in wb[sheet_name].iter_rows()]
    finally:
        wb.close()


def parse_workbook(path, sheet_name=SHEET_NAME, sector_map=None,
                   sub_sector_map=None):
    """Read the sheet's three blocks. Returns (sectors, sub_sectors, report).

    The sheet is not one table. It carries:

      1. Sector attractiveness      — left columns, under the first header.
      2. Raw sub-sector detail      — right columns of the SAME block, whose
                                      job is the raw-label -> clubbed-group
                                      mapping rather than scoring.
      3. Sub-sector attractiveness  — left columns again, under a SECOND
                                      header further down, holding the
                                      CLUBBED groups.

    Category F scores against block 3, not block 2 — scoring uses the group,
    not the raw label, because a group aggregates enough deals for a
    percentile to mean something. Reading the whole left column as one table
    silently files 34 clubbed groups as sectors.
    """
    rows = _sheet_rows(path, sheet_name)

    header_rows = []
    for index, row in enumerate(rows):
        keys = {normalise_header(c) for c in row if c is not None}
        if "deals" in keys or "dealcount" in keys:
            header_rows.append(index)
    if not header_rows:
        raise ValueError(
            f"no header row found in {sheet_name!r} — expected a column of "
            f"deal counts. If the layout changed, map the columns "
            f"explicitly.")

    # Each block runs from its header to the next header, or to the end.
    bounds = []
    for position, start in enumerate(header_rows):
        end = (header_rows[position + 1] if position + 1 < len(header_rows)
               else len(rows))
        bounds.append((start, end))

    report = {"header_rows": [i + 1 for i in header_rows],
              "blocks": [], "skipped": []}

    def _read(header, body, cols, level, label):
        out = []
        if "name" not in cols:
            return out
        for number, row in body:
            def get(field):
                index = cols.get(field)
                return row[index] if index is not None and index < len(row) \
                    else None

            name = str(get("name") or "").strip()
            low = name.lower()
            # A blank name AFTER data has started ends the table. Below it sit
            # the sheet's own integrity checks, which carry numbers in the
            # same columns — "Deals in source extract (input) 2699" imported
            # as a sub-sector otherwise, and would then be scored against.
            if not name:
                if out:
                    break
                continue
            # A totals row carries a deal count and would otherwise import
            # as a sector called "Total — Sector Table".
            if low.startswith(("total", "grand total")) \
                    or low in ("none", "name", "sr. no."):
                continue
            deals = _to_int(get("deal_count"))
            if deals is None:
                report["skipped"].append(
                    f"{label} row {number}: {name[:60]} — no deal count")
                continue
            out.append({
                "level": level,
                "name": name[:255],
                "deal_count": deals,
                "total_raised_usd_mn": _to_decimal(get("total_raised_usd_mn")),
                "avg_ticket_usd_mn": _to_decimal(get("avg_ticket_usd_mn")),
                "investor_count": _to_int(get("investor_count")),
                "clubbed_group": str(get("clubbed_group") or "").strip()[:255],
                "sample_quality": str(get("sample_quality") or "").strip()[:32],
                "velocity_score": _to_decimal(get("velocity_score")),
                "ticket_score": _to_decimal(get("ticket_score")),
                "investors_score": _to_decimal(get("investors_score")),
            })
        return out

    sectors, subs, mappings = [], [], []
    for position, (start, end) in enumerate(bounds):
        header = rows[start]
        body = [(i + 1, rows[i]) for i in range(start + 1, end)]
        # Block 1 is sectors; every later block holds clubbed sub-sectors.
        level = "sector" if position == 0 else "sub_sector"
        cols = dict((sector_map or {}) if position == 0 else {})
        if not cols:
            cols = detect_columns(header, SECTOR_COLUMNS)
        block = _read(header, body, cols, level, f"block {position + 1}")
        (sectors if level == "sector" else subs).extend(block)
        report["blocks"].append({
            "block": position + 1, "level": level,
            "header_row": start + 1, "rows": len(block),
            "columns": {f: header[i] for f, i in cols.items()
                        if i < len(header)}})

        # The raw sub-sector table shares block 1's rows, to the right of
        # the sector columns. It is read for its mapping only.
        if position == 0:
            after = max(cols.values()) + 1 if cols else 0
            raw_cols = dict(sub_sector_map or {})
            if not raw_cols:
                detected = detect_columns(header[after:], SUB_SECTOR_COLUMNS)
                raw_cols = {f: i + after for f, i in detected.items()}
            if "name" in raw_cols and "clubbed_group" in raw_cols:
                # The raw table is TALLER than the sector block beside it —
                # 104 raw labels against 18 sectors — so it is scanned to the
                # end of the sheet, not to the end of block 1. Bounding it to
                # the block silently kept only the first 21 mappings, and a
                # label with no mapping falls through to whatever the scoring
                # engine does with an unknown sub-sector.
                def cell(row, index):
                    # str(None) is "None", which is how twelve mappings from
                    # "None" to "None" got written on the first run.
                    if index is None or index >= len(row):
                        return ""
                    value = row[index]
                    return "" if value is None else str(value).strip()

                for index in range(start + 1, len(rows)):
                    row = rows[index]
                    raw = cell(row, raw_cols["name"])
                    group = cell(row, raw_cols["clubbed_group"])
                    if raw and group and not raw.lower().startswith("total"):
                        mappings.append((raw[:255], group[:255]))
            report["raw_sub_sector_columns"] = {
                f: header[i] for f, i in raw_cols.items() if i < len(header)}
            report["raw_sub_sector_mappings"] = len(mappings)

    report["mappings"] = mappings

    for group in (sectors, subs):
        if not group:
            continue
        # Average ticket is derived where the sheet does not carry it, so a
        # block with only totals still scores on ticket size.
        for row in group:
            if row["avg_ticket_usd_mn"] is None and row["deal_count"]:
                raised = row["total_raised_usd_mn"]
                if raised is not None:
                    row["avg_ticket_usd_mn"] = round(
                        raised / Decimal(row["deal_count"]), 4)
        # The workbook's own scores win where it carries them; ranking only
        # fills the gaps, so an import reproduces the sheet rather than
        # quietly recomputing it.
        #
        # A row under the minimum deal count is EXCLUDED from the ranking and
        # scores blank — the sheet's own rule (Deal Scorecard L42, "Sectors
        # and sub-sectors below this deal count are excluded from the
        # percentile ranking and score blank, so the category weight
        # redistributes rather than crediting a one-deal sample"). Web3, with
        # four deals, has three empty score cells in the shipped workbook.
        floor = _min_deals()
        eligible = [r for r in group if (r["deal_count"] or 0) >= floor]
        for field, source in (("velocity_score", "deal_count"),
                              ("ticket_score", "avg_ticket_usd_mn"),
                              ("investors_score", "investor_count")):
            if all(r[field] is not None for r in group):
                continue
            ranked = _percentile_scores([r[source] for r in eligible])
            for index, row in enumerate(eligible):
                if row[field] is None:
                    row[field] = ranked[index]
        for row in group:
            if (row["deal_count"] or 0) < floor:
                # Blank, not zero. A thin sample is unknown, not bad.
                for field in ("velocity_score", "ticket_score",
                              "investors_score"):
                    row[field] = None
            if not row["sample_quality"]:
                row["sample_quality"] = ("Thin"
                                         if (row["deal_count"] or 0) < floor
                                         else "OK")
    return sectors, subs, report


# Fields whose change is worth reporting in a dry run. The scores are
# derived, so they move whenever the population moves and would drown the
# diff in noise.
_DIFF_FIELDS = ("deal_count", "total_raised_usd_mn", "avg_ticket_usd_mn",
                "investor_count", "clubbed_group")


def apply_rows(rows, as_of_date=None, source_file="", dry_run=False,
               mappings=None):
    """Upsert benchmark rows. Returns a report of what changed.

    Re-import updates IN PLACE and moves existing ratings: newer market data
    should change what "a fast-moving sector" means, and an assessment run
    tomorrow ought to reflect it. Nothing is versioned here, so the dry run
    is the safeguard — it names every row that would move and by how much.
    """
    from fundos.assessment.models import SectorDealData, SectorMapping

    as_of_date = as_of_date or dt.date.today()
    report = {"created": [], "updated": [], "unchanged": 0,
              "mappings_created": 0, "mappings_updated": 0}
    existing = {(r.level, r.name): r
                for r in SectorDealData.objects.filter(
                    level__in={row["level"] for row in rows})}

    for row in rows:
        current = existing.get((row["level"], row["name"]))
        if current is None:
            report["created"].append(row["name"])
            if not dry_run:
                SectorDealData.objects.create(
                    as_of_date=as_of_date, source_file=source_file[:255],
                    **row)
            continue
        changes = []
        for field in _DIFF_FIELDS:
            before, after = getattr(current, field), row.get(field)
            if before is None and after is None:
                continue
            if str(before) != str(after):
                changes.append(f"{field} {before} -> {after}")
        if changes:
            report["updated"].append(f"{row['name']}: {'; '.join(changes)}")
        else:
            report["unchanged"] += 1
        if not dry_run:
            for field, value in row.items():
                setattr(current, field, value)
            current.as_of_date = as_of_date
            current.source_file = source_file[:255]
            current.save()

    # The raw-label -> clubbed-group mapping the scoring engine resolves
    # against. Free-text sector labels arrive spelled a dozen ways, so this
    # is the layer that decides which benchmark row a company is judged by —
    # and getting it wrong misprices a category worth 20% of the rating.
    pairs = list(mappings or [])
    pairs += [(r["name"], r["clubbed_group"]) for r in rows
              if r["level"] == "sub_sector" and r.get("clubbed_group")]
    seen = set()
    for raw, group in pairs:
        if raw in seen:
            continue
        seen.add(raw)
        if dry_run:
            match = SectorMapping.objects.filter(raw_label=raw).first()
            if match is None:
                report["mappings_created"] += 1
            elif match.clubbed_group != group:
                report["mappings_updated"] += 1
                report.setdefault("mapping_changes", []).append(
                    f"{raw}: {match.clubbed_group} -> {group}")
            continue
        _, created = SectorMapping.objects.update_or_create(
            raw_label=raw, defaults={"clubbed_group": group})
        report["mappings_created" if created else "mappings_updated"] += 1
    return report
