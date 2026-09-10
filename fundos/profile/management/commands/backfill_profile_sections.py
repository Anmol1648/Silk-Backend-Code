"""
Backfill / regenerate Company Profile structured data.

Companies whose profiles were generated before the narrative-structured
generation fix (products_services, customers_markets, competitive_advantages,
business_model) hold their AI output only as prose in ProfileSection.content,
so sections.data reads as empty (or via the content-fallback only). This
command re-runs generation for those profiles so the structured §7 data is
materialised properly.

Usage:
    python manage.py backfill_profile_sections            # all profiles
    python manage.py backfill_profile_sections --company <uuid>
    python manage.py backfill_profile_sections --only-empty   # skip ones that
        already have structured data in all four narrative sections
    python manage.py backfill_profile_sections --dry-run
"""
from django.core.management.base import BaseCommand


NARRATIVE = ["products_services", "customers_markets",
             "competitive_advantages", "business_model"]


class Command(BaseCommand):
    help = "Regenerate Company Profile sections so structured data is populated."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Only this company id (UUID).")
        parser.add_argument(
            "--only-empty", action="store_true",
            help="Skip profiles whose 4 narrative sections already have "
                 "structured data.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would run without generating.")

    def handle(self, *args, **opts):
        from fundos.profile.models import CompanyProfile, ProfileSection
        from fundos.profile.services import generate_profile

        qs = CompanyProfile.objects.all()
        if opts.get("company"):
            qs = qs.filter(company_id=opts["company"])

        total = qs.count()
        self.stdout.write(f"Considering {total} profile(s).")
        ran = skipped = 0

        for profile in qs.iterator():
            if opts.get("only_empty") and self._has_all_structured(
                    profile, ProfileSection):
                skipped += 1
                continue
            if opts.get("dry_run"):
                self.stdout.write(f"  would regenerate: {profile.company_id}")
                ran += 1
                continue
            try:
                generate_profile(profile, user=None)
                ran += 1
                self.stdout.write(f"  regenerated: {profile.company_id}")
            except Exception as e:  # never let one bad profile stop the batch
                skipped += 1
                self.stderr.write(f"  FAILED {profile.company_id}: {e}")

        self.stdout.write(self.style.SUCCESS(
            f"Done. regenerated={ran} skipped={skipped}"))

    def _has_all_structured(self, profile, ProfileSection):
        rows = {s.section_key: s for s in ProfileSection.objects.filter(
            profile=profile, section_key__in=NARRATIVE, is_active=True)}
        for key in NARRATIVE:
            s = rows.get(key)
            st = (s.structured if s else None) or {}
            if key == "business_model":
                if not any(v for v in st.values()):
                    return False
            else:
                if not (st.get("items")):
                    return False
        return True
