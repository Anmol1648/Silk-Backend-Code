"""Re-run the critique for uploaded documents whose analysis is stale.

An analysis is only as good as the text it was given, and for a long time
every upload was given nothing: `_extract_text` called a helper that had been
renamed, caught its own AttributeError, and returned `("", False)`. The
critique model was then handed an empty document and asked to assess it, so it
did the only thing it could and reasoned from the filename:

    "Text could not be extracted from this file format, so section coverage
     is unverified until a searchable export is provided."

That was written about a .pptx nobody had tried to open. Those rows are stale
rather than wrong-in-an-interesting-way, and they stay stale after the fix,
because nothing re-reads a file that has already been processed.

Two further reasons a row can be stale, both handled by the same pass: it was
analysed before `inconsistencies` and `missing_information` were stored, so
half its findings were discarded; or its text extracted fine but the model was
unavailable that day.

Safe to re-run. Nothing is deleted — each material is re-extracted and
re-critiqued in place, and a failure leaves the previous analysis alone.
"""
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = ("Re-extract and re-critique uploaded documents whose stored "
            "analysis was produced without their text.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", default="",
            help="Company id. Omit to sweep every company.")
        parser.add_argument(
            "--all", action="store_true",
            help="Re-analyse every material, not only the stale ones.")
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after this many. 0 means no limit.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List what would be re-analysed and change nothing.")

    def handle(self, *args, **opts):
        from fundos.profile.models import ProfileDocument
        from fundos.readiness.models import MaterialAsset
        from fundos.readiness.tasks import scan_and_analyse_material

        rows = MaterialAsset.objects.filter(is_deleted=False)
        if opts["company"]:
            ids = ProfileDocument.objects.filter(
                profile__company_id=opts["company"]).exclude(
                material_asset_id=None).values_list(
                "material_asset_id", flat=True)
            rows = rows.filter(id__in=list(ids))
        if not opts["all"]:
            # Stale: never extracted, or analysed before the two findings
            # columns existed. A row with real text and real findings is left
            # alone — re-running it costs a model call to reproduce what is
            # already there.
            rows = rows.filter(extraction_status__in=("failed", "pending"))
        rows = rows.order_by("created_at")
        if opts["limit"]:
            rows = rows[:opts["limit"]]

        total = rows.count() if hasattr(rows, "count") else len(rows)
        self.stdout.write(f"{total} material(s) to re-analyse"
                          + (" (dry run)" if opts["dry_run"] else ""))

        done = failed = 0
        for material in rows:
            label = f"{material.id} ({material.category})"
            if opts["dry_run"]:
                self.stdout.write(
                    f"  would re-analyse {label} "
                    f"[extraction={material.extraction_status}]")
                continue
            try:
                # Through the real task, so re-analysis and first analysis
                # cannot drift apart: one path, one set of behaviours.
                scan_and_analyse_material(
                    str(material.tenant_id), str(material.deal_id),
                    str(material.id))
                material.refresh_from_db()
                done += 1
                self.stdout.write(
                    f"  {label}: {material.extraction_status}, "
                    f"{len(material.inconsistencies or [])} inconsistency(ies), "
                    f"{len(material.missing_information or [])} gap(s)")
            except Exception as exc:              # noqa: BLE001 - report, go on
                failed += 1
                self.stderr.write(f"  {label}: FAILED — {exc}")

        if not opts["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(f"re-analysed {done}, failed {failed}"))
