"""Run the refresh pipeline from the command line.

The admin console is the primary interface; this exists for the scheduler and
for scripted use.

    python manage.py run_refresh --dry-run
    python manage.py run_refresh --commit --stages scrape explode
    python manage.py run_refresh --commit --limit-articles 2
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the investor data refresh pipeline."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true",
                            help="Write changes. Without this it is a dry run.")
        parser.add_argument("--stages", nargs="*", default=None,
                            choices=["scrape", "explode", "enrich", "merge",
                                     "derive"])
        parser.add_argument("--limit-articles", type=int, default=None)
        parser.add_argument("--single-url", default=None)
        parser.add_argument("--source", default=None)

    def handle(self, *args, **options):
        from fundos.investors.pipeline import run_refresh

        run = run_refresh(
            dry_run=not options["commit"], stages=options["stages"],
            limit_articles=options["limit_articles"],
            single_url=options["single_url"], source_name=options["source"])

        self.stdout.write(f"Run {run.id} — {run.status}")
        for report in run.stage_reports:
            self.stdout.write(f"  [{report['stage']}] {report['status']}")
            for key, value in (report.get("counts") or {}).items():
                self.stdout.write(f"      {key}: {value}")
            for warning in (report.get("warnings") or [])[:5]:
                self.stdout.write(self.style.WARNING(f"      ! {warning}"))
            for error in (report.get("errors") or []):
                self.stdout.write(self.style.ERROR(f"      ✗ {error}"))

        if not options["commit"]:
            self.stdout.write(self.style.WARNING(
                "Dry run — nothing written. Re-run with --commit to apply."))
