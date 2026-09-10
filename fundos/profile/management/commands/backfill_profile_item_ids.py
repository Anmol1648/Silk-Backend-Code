"""Give every existing list-section item a durable id.

New writes get one from `section_writer._ensure_item_ids`, but a profile
generated before that existed has founders, competitors and news entries with
nothing to address them by — so the per-item Confirm has no key to send and
the section falls back to being confirmed all-or-nothing.

Assigning ids changes no content and no confirmation: a section whose
`confirmed_fields` holds the whole-section sentinel keeps reading as fully
confirmed, because `_confirmed_items` still honours it.
"""
import uuid

from django.core.management.base import BaseCommand

from fundos.profile.models import ProfileSection


class Command(BaseCommand):
    help = ("Assign a durable id to every object in a list-shaped profile "
            "section that does not already have one.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", default="",
            help="Limit to one company id. Default: every profile.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be assigned without writing.")

    def handle(self, *args, **options):
        rows = ProfileSection.objects.filter(is_active=True)
        if options["company"]:
            rows = rows.filter(profile__company_id=options["company"])

        sections_touched, items_assigned = 0, 0
        for section in rows.select_related("profile").iterator():
            structured = section.structured
            # Both storage shapes: a bare list, or rows nested under `items`.
            # Only looking for the first found nothing on products_services,
            # customers_markets and every other section that nests.
            data = structured
            if isinstance(structured, dict):
                nested = structured.get("items")
                data = nested if isinstance(nested, list) else None
            if not isinstance(data, list) or not data:
                continue
            missing = [item for item in data
                       if isinstance(item, dict) and not item.get("id")]
            if not missing:
                continue
            for item in missing:
                item["id"] = str(uuid.uuid4())
            sections_touched += 1
            items_assigned += len(missing)
            self.stdout.write(
                f"  {section.section_key}: {len(missing)} of {len(data)} "
                f"item(s) assigned an id")
            if not options["dry_run"]:
                section.structured = structured
                section.save(update_fields=["structured", "updated_at"])

        verb = "would assign" if options["dry_run"] else "assigned"
        self.stdout.write(
            f"\n{verb} {items_assigned} id(s) across {sections_touched} "
            f"section(s).")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
