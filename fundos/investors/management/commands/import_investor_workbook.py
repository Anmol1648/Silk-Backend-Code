"""Import the supplied investor workbook as the seed corpus.

Run this before anything else: it gives Stage 3 a complete dataset on day one
and turns the refresh pipeline into an incremental problem rather than a
bootstrap one.

    python manage.py seed_investor_config
    python manage.py import_investor_workbook /path/to/workbook.xlsx
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Import investor deal rows from the source workbook."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--tenant", default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--skip-relationships", action="store_true",
                            help="Do not import the CRM/contact block.")

    def handle(self, *args, **options):
        import os
        from fundos.investors.importer import import_workbook

        path = options["path"]
        if not os.path.exists(path):
            raise CommandError(f"File not found: {path}")

        def progress(counts):
            self.stdout.write(f"  … {counts['rows']} rows")

        self.stdout.write(f"Importing {path} …")
        counts = import_workbook(
            path, tenant_id=options["tenant"],
            import_relationships=not options["skip_relationships"],
            dry_run=options["dry_run"], progress=progress)

        for key, value in counts.items():
            self.stdout.write(f"  {key}: {value}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
        else:
            self.stdout.write(self.style.SUCCESS("Import complete."))
            self.stdout.write(
                "Verify with: Admin → Investors → Match config → Run preview. "
                "The reference query should return 301 investors / 477 deals.")
