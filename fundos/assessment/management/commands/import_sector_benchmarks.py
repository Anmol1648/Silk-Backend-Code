"""Load the workbook's Sector Deal Data into the benchmark tables."""
import datetime as dt

from django.core.management.base import BaseCommand
from django.db import transaction

from fundos.assessment.benchmarks import (SHEET_NAME, apply_rows,
                                          parse_workbook)


class Command(BaseCommand):
    help = ("Import sector and sub-sector deal benchmarks from the deal "
            "assessment workbook. Categories E and F are scored against "
            "these, so a third of every deal rating rests on this table.")

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the .xlsx workbook.")
        parser.add_argument("--sheet", default=SHEET_NAME)
        parser.add_argument(
            "--as-of", default="",
            help="Date this data represents (YYYY-MM-DD). Defaults to today.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report every row that would change, and by how much, "
                 "without writing. Run this first: a re-import updates in "
                 "place and moves existing ratings.")

    def handle(self, *args, **options):
        as_of = (dt.date.fromisoformat(options["as_of"])
                 if options["as_of"] else dt.date.today())
        dry = options["dry_run"]

        sectors, subs, report = parse_workbook(options["path"],
                                               sheet_name=options["sheet"])
        for block in report["blocks"]:
            self.stdout.write(
                f"Block {block['block']} ({block['level']}) — header row "
                f"{block['header_row']}, {block['rows']} rows:")
            for field, header in block["columns"].items():
                self.stdout.write(f"    {field:24} <- {header!r}")
        if report.get("raw_sub_sector_columns"):
            self.stdout.write(
                f"Raw sub-sector -> clubbed group "
                f"({report.get('raw_sub_sector_mappings', 0)} pairs):")
            for field, header in report["raw_sub_sector_columns"].items():
                self.stdout.write(f"    {field:24} <- {header!r}")
        if report["skipped"]:
            self.stdout.write(self.style.WARNING(
                f"  {len(report['skipped'])} row(s) skipped:"))
            for line in report["skipped"][:10]:
                self.stdout.write(f"    {line}")

        self.stdout.write(f"\nParsed {len(sectors)} sector and {len(subs)} "
                          f"sub-sector rows.")
        if not sectors and not subs:
            self.stderr.write(self.style.ERROR(
                "Nothing to import. Check --sheet, and that the header row "
                "carries a deal-count column."))
            raise SystemExit(1)

        with transaction.atomic():
            result = apply_rows(sectors + subs, as_of_date=as_of,
                                source_file=options["path"], dry_run=dry,
                                mappings=report.get("mappings"))
            if dry:
                transaction.set_rollback(True)

        verb = "would be" if dry else ""
        self.stdout.write(f"\n  created {verb}: {len(result['created'])}")
        for name in result["created"][:15]:
            self.stdout.write(f"    + {name}")
        if len(result["created"]) > 15:
            self.stdout.write(f"    … {len(result['created']) - 15} more")

        self.stdout.write(f"  updated {verb}: {len(result['updated'])}")
        for line in result["updated"][:20]:
            self.stdout.write(f"    ~ {line}")
        if len(result["updated"]) > 20:
            self.stdout.write(f"    … {len(result['updated']) - 20} more")

        self.stdout.write(f"  unchanged: {result['unchanged']}")
        self.stdout.write(
            f"  sub-sector mappings: {result['mappings_created']} new, "
            f"{result['mappings_updated']} updated")
        for line in (result.get("mapping_changes") or [])[:10]:
            self.stdout.write(f"    ~ {line}")

        if dry:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — nothing written. A real import updates these "
                "rows IN PLACE, which will change the Sector and Sub-Sector "
                "scores of any assessment re-run afterwards."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\nBenchmarks imported, as of {as_of}."))
