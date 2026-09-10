"""Re-derive E.2/E.3/E.6 and F.2/F.3/F.6 for existing assessments.

The deal table is DATA: `import_sector_benchmarks` loads it, and reloading it
is how the numbers are refreshed. But the six percentile parameters were only
ever resolved once, inside step 1's profile extraction — so an assessment
extracted before the sheet was imported, or before it was refreshed, kept six
blank rows while the table held every number they needed. Categories E and F
are 30% of the rating between them.

`profile_bridge.refresh_benchmarks` fixes this for every assessment seeded
from now on. This command is the other half: it applies the same lookup to
the assessments that already exist, so a re-import actually moves the ratings
it is supposed to move.

Nothing here scores anything. It writes the same ParameterValue rows the
bridge writes, from the same lookup, and re-runs the existing scoring so the
category rolls up — no rubric, weight or formula is touched.
"""
from django.core.management.base import BaseCommand

from fundos.assessment.models import Assessment
from fundos.assessment.profile_bridge import refresh_benchmarks
from fundos.profile.assessment_extraction import benchmark_absences


class Command(BaseCommand):
    help = ("Re-derive the six sector/sub-sector percentile parameters for "
            "existing assessments from the current Sector Deal Data table. "
            "Run after import_sector_benchmarks.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", default="",
            help="Limit to one company id. Default: every live assessment.")
        parser.add_argument(
            "--assessment", default="",
            help="Limit to one assessment id.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be written, and why each blank stays "
                 "blank, without writing.")
        parser.add_argument(
            "--rescore", action="store_true",
            help="Re-run scoring for each assessment that changed, so the "
                 "new percentiles reach the category roll-up.")

    def handle(self, *args, **options):
        from fundos.assessment.services import run_scoring

        qs = Assessment.objects.exclude(status="superseded")
        if options["company"]:
            qs = qs.filter(company_id=options["company"])
        if options["assessment"]:
            qs = qs.filter(id=options["assessment"])
        qs = qs.select_related("company").order_by("-created_at")

        total, changed, rescored = 0, 0, 0
        for assessment in qs:
            total += 1
            absences = benchmark_absences(assessment.sector or "",
                                          assessment.sub_sector or "")
            if options["dry_run"]:
                from fundos.profile.assessment_extraction import (
                    resolve_benchmarks)
                rows, _group, _method, _absent = resolve_benchmarks(
                    assessment.sector or "", assessment.sub_sector or "")
                written = len(rows)
            else:
                written = refresh_benchmarks(assessment)

            if written:
                changed += 1
            verb = "would write" if options["dry_run"] else "wrote"
            self.stdout.write(
                f"{assessment.company.name} [{assessment.id}] "
                f"{assessment.sector!r}/{assessment.sub_sector!r}: "
                f"{verb} {written} percentile(s)")
            for key, (reason, sentence) in sorted(absences.items()):
                self.stdout.write(self.style.WARNING(
                    f"    {key} withheld ({reason}): {sentence}"))

            if written and options["rescore"] and not options["dry_run"]:
                run_scoring(assessment)
                rescored += 1

        self.stdout.write(
            f"\n{total} assessment(s) examined, {changed} updated"
            + (f", {rescored} re-scored." if rescored else "."))
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
