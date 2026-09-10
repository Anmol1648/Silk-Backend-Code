"""
Migrate existing data to the Silk 2.0 model.

Idempotent — safe to re-run. Performs four things:

1. Unlocks every stage (C6 — stages are never locked).
2. Creates a CompanyProfile for every existing company, carrying across
   website, HQ country and home currency.
3. Seeds profile sections from the existing knowledge base so nothing a
   founder already entered is lost.
4. Links artefacts produced by the former Stage 3 to the Company Profile's
   Document Center as READ-ONLY addenda (C7). Nothing is deleted; each
   subpart is slated to return as a dedicated assistant.

    python manage.py migrate_to_silk
    python manage.py migrate_to_silk --dry-run
"""
from django.core.management.base import BaseCommand
from django.db import transaction

# Old Stage-3 doc types → Document Center categories (C7)
DOC_TYPE_MAP = {
    "teaser": "teaser",
    "deck": "pitch_deck",
    "im": "information_memorandum",
    "model": "financial_model",
}

# Knowledge-base fields that seed profile sections, so founder-entered data
# survives the move from the deal-scoped CKB to the company-scoped profile.
SECTION_FROM_CKB = {
    "business_model": ("business_model", "Business model"),
    "revenue_model": ("revenue_model", "Revenue model"),
    "description": ("company_overview", "Company description"),
    "solution": ("products_services", "Products and services"),
    "segments": ("customers_markets", "Customers and markets"),
}


class Command(BaseCommand):
    help = "Migrate existing companies and deals to the Silk 2.0 model."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change without writing.")

    def handle(self, *args, **options):
        self.dry = options.get("dry_run", False)
        if self.dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes.\n"))

        self._unlock_stages()
        self._create_profiles()
        self._link_stage3_artefacts()

        self.stdout.write(self.style.SUCCESS(
            "\nMigration complete." if not self.dry
            else "\nDry run complete — re-run without --dry-run to apply."))

    # ------------------------------------------------------------------
    def _unlock_stages(self):
        from fundos.core.models import StageState

        locked = StageState.objects.filter(status="locked")
        count = locked.count()
        if not self.dry and count:
            locked.update(status="available")
        self.stdout.write(f"  stages unlocked: {count}")

    # ------------------------------------------------------------------
    def _create_profiles(self):
        from fundos.core.models import Company

        created = seeded = 0
        for company in Company.objects.filter(is_deleted=False):
            if self.dry:
                from fundos.profile.models import CompanyProfile
                if not CompanyProfile.objects.filter(company=company).exists():
                    created += 1
                continue
            with transaction.atomic():
                profile, made = self._ensure_profile(company)
                created += int(made)
                seeded += self._seed_sections(company, profile)
        self.stdout.write(f"  profiles created: {created}")
        if not self.dry:
            self.stdout.write(f"  sections seeded from knowledge base: {seeded}")

    def _ensure_profile(self, company):
        from fundos.platformcfg.services import home_currency_for_country
        from fundos.profile.models import CompanyProfile
        from fundos.profile.services import ensure_sections

        profile = CompanyProfile.objects.filter(company=company).first()
        made = False
        if not profile:
            profile = CompanyProfile.objects.create(
                tenant_id=company.tenant_id, company=company,
                website_url=company.domain or "",
                hq_country=(company.hq_country or "")[:2].upper(),
                home_currency=home_currency_for_country(company.hq_country))
            made = True
        ensure_sections(profile)
        return profile, made

    def _seed_sections(self, company, profile):
        """Carry founder-entered knowledge-base values into profile sections."""
        from fundos.core.models import CkbField, Deal
        from fundos.profile.models import ProfileSection

        deal_ids = list(Deal.objects.filter(company=company, is_deleted=False)
                        .values_list("id", flat=True))
        if not deal_ids:
            return 0

        seeded = 0
        for field_key, (section_key, label) in SECTION_FROM_CKB.items():
            row = (CkbField.objects
                   .filter(deal_id__in=deal_ids, field_key=field_key,
                           is_deleted=False)
                   .exclude(value_text__isnull=True, value_json__isnull=True)
                   .first())
            if not row:
                continue
            value = row.value
            if not value or not isinstance(value, str):
                continue
            section = ProfileSection.objects.filter(
                profile=profile, section_key=section_key, is_active=True).first()
            if not section or section.content:
                continue    # never overwrite existing profile content
            section.content = value
            section.needs_input = False
            section.source = row.source or "founder"
            section.verified = row.verified
            section.change_summary = f"Migrated from knowledge base ({label})"
            section.save()
            seeded += 1
        return seeded

    # ------------------------------------------------------------------
    def _link_stage3_artefacts(self):
        """C7 — existing Stage-3 output becomes a read-only Profile addendum.

        Nothing is deleted or reassigned to the new Stage 3 (Investor
        Discovery), which would be wrong. Each artefact is attached to its
        company's Document Center and flagged read-only until the
        corresponding assistant is rebuilt.
        """
        from fundos.core.models import Deal
        from fundos.docs.models import Document
        from fundos.profile.models import CompanyProfile, ProfileDocument

        linked = 0
        docs = Document.objects.filter(is_deleted=False,
                                       doc_type__in=DOC_TYPE_MAP.keys())
        for doc in docs:
            deal = Deal.objects.filter(id=doc.deal_id).first()
            if not deal:
                continue
            profile = CompanyProfile.objects.filter(company=deal.company).first()
            if not profile:
                continue
            category = DOC_TYPE_MAP.get(doc.doc_type, "other")
            if self.dry:
                if not ProfileDocument.objects.filter(
                        profile=profile, document_id=doc.id).exists():
                    linked += 1
                continue
            _, made = ProfileDocument.objects.get_or_create(
                profile=profile, document_id=doc.id,
                defaults={
                    "tenant_id": profile.tenant_id,
                    "category": category,
                    "filename": getattr(doc, "title", "") or doc.doc_type,
                    "is_readonly": True,
                    "origin_stage": "legacy_stage_3",
                })
            linked += int(made)
        self.stdout.write(f"  Stage-3 artefacts linked read-only: {linked}")
