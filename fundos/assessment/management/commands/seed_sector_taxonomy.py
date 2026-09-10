"""Load the sector and sub-sector deal taxonomy that categories E and F score against.

Why this command exists
-----------------------
Six of the scored parameters -- E.2, E.3, E.6, F.2, F.3, F.6 -- are not
extracted from anything the company wrote. They are read out of a database of
Indian deals, looked up by cohort name. Categories E and F carry 30% of the
rating between them, so an empty taxonomy does not degrade the score, it
removes a third of it.

Until now the only way to populate those tables was
``import_sector_benchmarks``, which parses the source .xlsx. That is the right
tool for refreshing the figures, and the wrong one for standing an environment
up: an install with no workbook to hand has no cohorts at all, and every
company it assesses scores blank on both market categories with nothing in the
response to say why.

The second, quieter failure is the one that actually bit. The extraction prompt
shows the model a CLOSED LIST of sector names to choose from, built by
``assessment_extraction.vocabulary()`` -- which reads these same tables. An
empty table produces an empty list, the closed-list instruction has nothing to
close over, and the model falls back to describing the company in its own
words: "Healthcare", "Digital Health". Neither matches, so the cohort rows
score blank, and the cause looks like a taxonomy gap when it is really an
empty-vocabulary bug one step upstream.

The data
--------
``fundos/assessment/data/sector_taxonomy.json`` -- 18 sectors, 50 clubbed
sub-sector groups and the 104 raw labels that map into them, with the deal
counts, ticket sizes, investor counts and percentile scores already derived
from the same source workbook. It is checked in as data, not code, so adding a
sector stays a row rather than a deploy.

Percentile scores are taken as published rather than recomputed. A rank depends
on the whole population, so recomputing after a partial load would silently
move the score of a row nobody touched -- the same reason
``benchmarks.parse_workbook`` prefers the sheet's own figures.
"""
import datetime as dt
import json
import os

from django.core.management.base import BaseCommand
from django.db import transaction

from fundos.assessment.benchmarks import apply_rows

DATA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "sector_taxonomy.json")


def _rows(payload):
    """The taxonomy as SectorDealData row dicts, plus the raw-label mapping.

    A sub-sector row IS its own clubbed group -- the 50 are the groups, and the
    104 raw labels below them are the mapping. Setting `clubbed_group` to the
    row's own name is what lets `resolve_sub_sector` find a company that
    already named its group correctly, without a mapping row for every one.
    """
    rows = []
    for level, key in (("sector", "sectors"), ("sub_sector", "sub_sectors")):
        for entry in payload.get(key) or []:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            rows.append({
                "level": level,
                "name": name,
                "deal_count": _int(entry.get("deals")),
                "total_raised_usd_mn": _dec(entry.get("raised_usd_mn")),
                "avg_ticket_usd_mn": _dec(entry.get("avg_ticket_usd_mn")),
                "investor_count": _int(entry.get("investors")),
                "velocity_score": _dec(entry.get("velocity_score"), 2),
                "ticket_score": _dec(entry.get("ticket_score"), 2),
                "investors_score": _dec(entry.get("investor_score"), 2),
                "clubbed_group": name if level == "sub_sector" else "",
                "sample_quality": str(entry.get("sample_quality") or "")[:32],
            })

    # The 104 labels the deal database itself records, plus the curated
    # aliases. An alias is a descriptive name an extractor has actually
    # returned -- "Digital Health", "Corporate Employee Health Benefits" --
    # pointed at the group it unambiguously means. Every target is a name
    # already in the taxonomy; nothing here invents a cohort.
    mappings = []
    seen = set()
    for key, field in (("raw_sub_sector_map", "clubbed"),
                       ("sub_sector_aliases", "clubbed")):
        for entry in payload.get(key) or []:
            raw = str(entry.get("raw") or "").strip()
            group = str(entry.get(field) or "").strip()
            if raw and group and raw.casefold() not in seen:
                seen.add(raw.casefold())
                mappings.append((raw, group))
    return rows, mappings


def sector_aliases(payload=None):
    """Descriptive sector labels mapped to one of the 18 cohort names.

    Read from the shipped taxonomy rather than from `SectorMapping`, which is
    the SUB-sector table: putting "Healthcare" in there would resolve it as a
    sub-sector group as well, and the two lists are not interchangeable.
    """
    if payload is None:
        try:
            with open(DATA_FILE, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return {}
    out = {}
    for entry in payload.get("sector_aliases") or []:
        raw = str(entry.get("raw") or "").strip()
        sector = str(entry.get("sector") or "").strip()
        if raw and sector:
            out[raw] = sector
    return out


def _int(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _dec(value, places=4):
    from decimal import Decimal
    try:
        return Decimal(str(round(float(value), places)))
    except (TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = ("Load the 18 sectors, 50 clubbed sub-sector groups and 104 raw "
            "label mappings that categories E and F score against. Safe to "
            "re-run: rows are upserted by (level, name).")

    def add_arguments(self, parser):
        parser.add_argument("--path", default=DATA_FILE,
                            help="Taxonomy JSON. Defaults to the shipped file.")
        parser.add_argument(
            "--as-of", default="",
            help="Date this data represents (YYYY-MM-DD). Defaults to today.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing. Worth running "
                 "first on an environment that already has cohort data: a "
                 "re-import updates in place and moves existing ratings.")

    def handle(self, *args, **options):
        path = options["path"]
        if not os.path.exists(path):
            self.stderr.write(self.style.ERROR(
                f"No taxonomy file at {path}."))
            raise SystemExit(1)

        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)

        rows, mappings = _rows(payload)
        if not rows:
            self.stderr.write(self.style.ERROR(
                "The taxonomy file carries no sector rows."))
            raise SystemExit(1)

        as_of = (dt.date.fromisoformat(options["as_of"])
                 if options["as_of"] else dt.date.today())
        dry = options["dry_run"]

        sectors = [r for r in rows if r["level"] == "sector"]
        subs = [r for r in rows if r["level"] == "sub_sector"]
        self.stdout.write(
            f"{payload.get('coverage') or 'Sector deal database'}\n"
            f"  {len(sectors)} sectors, {len(subs)} clubbed sub-sector groups, "
            f"{len(mappings)} raw label mappings")

        with transaction.atomic():
            report = apply_rows(rows, as_of_date=as_of,
                                source_file=os.path.basename(path),
                                dry_run=dry, mappings=mappings)

        verb = "would create" if dry else "created"
        self.stdout.write(
            f"  {verb} {len(report['created'])}, "
            f"{'would update' if dry else 'updated'} {len(report['updated'])}, "
            f"unchanged {report['unchanged']}")
        self.stdout.write(
            f"  mappings: {report['mappings_created']} created, "
            f"{report['mappings_updated']} updated")

        for line in report["updated"][:20]:
            self.stdout.write(f"    {line}")

        if dry:
            self.stdout.write(self.style.WARNING(
                "Dry run — nothing was written."))
        else:
            self.stdout.write(self.style.SUCCESS(
                "Sector taxonomy loaded. Categories E and F can now resolve a "
                "cohort, and the extraction prompt has a closed list to offer "
                "the model."))
