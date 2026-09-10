"""config_import — apply a config_export JSON (Doc 9 promotion path).
Secrets never travel in the file: api_key_env_var / *_env_var names are
imported; values stay in the target environment."""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Import FundOS configuration from a config_export JSON file."

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, *args, **options):
        from fundos.config.models import (
            InvestorCategoryMap, MasterValue, ResearchSourceFlags,
            ScoringConfig, SectorTaxonomyMap,
        )
        from fundos.core.models import AlertDefinition
        from fundos.llm.models import LLMEndpoint, LLMRoleBinding

        with open(options["path"]) as fh:
            payload = json.load(fh)

        for c in payload.get("scoringConfigs", []):
            ScoringConfig.objects.update_or_create(
                version_no=c["versionNo"],
                defaults={"data": c["data"], "is_active": c["isActive"]})
        for f in payload.get("researchSourceFlags", []):
            ResearchSourceFlags.objects.update_or_create(
                source_type=f["sourceType"],
                defaults={"enabled": f["enabled"],
                          "legal_cleared": f["legalCleared"]})
        for v in payload.get("masterValues", []):
            MasterValue.objects.update_or_create(
                table=v["table"], code=v["code"],
                defaults={"label": v["label"], "meta": v["meta"],
                          "sort_order": v["sortOrder"],
                          "is_active": v["isActive"]})
        for r in payload.get("investorCategoryMap", []):
            InvestorCategoryMap.objects.update_or_create(
                round_type=r["roundType"], raise_band=r["raiseBand"],
                defaults={"band_low_usd": r["bandLowUsd"],
                          "band_high_usd": r["bandHighUsd"],
                          "primary": r["primary"],
                          "secondary": r["secondary"],
                          "optional": r["optional"]})
        for s in payload.get("sectorTaxonomyMap", []):
            SectorTaxonomyMap.objects.update_or_create(
                founder_sector=s["founderSector"],
                defaults={"canonical_sector": s["canonicalSector"],
                          "adjacent_sectors": s["adjacentSectors"]})
        for e in payload.get("llmEndpoints", []):
            LLMEndpoint.objects.update_or_create(
                code=e["code"],
                defaults={"provider_kind": e["providerKind"],
                          "base_url": e["baseUrl"],
                          "default_model": e["defaultModel"],
                          "api_key_env_var": e["apiKeyEnvVar"],
                          "is_active": e["isActive"],
                          "priority": e["priority"]})
        for b in payload.get("llmRoleBindings", []):
            binding = LLMRoleBinding.objects.filter(role=b["role"]).first()
            if binding:
                binding.primary_endpoint_id = b["primary"]
                binding.fallback_endpoint_id = b.get("fallback")
                binding.is_mocked = b["isMocked"]
                binding.save()
        for a in payload.get("alertDefinitions", []):
            AlertDefinition.all_objects.update_or_create(
                event_key=a["eventKey"], name=a["name"], is_deleted=False,
                defaults={"source_type": a["sourceType"],
                          "heading": a["heading"],
                          "insight_user_query": a["messageTemplate"],
                          "notify_email": a["notifyEmail"],
                          "notify_in_app": a["notifyInApp"],
                          "notify_whatsapp": a["notifyWhatsapp"],
                          "is_active": a["isActive"]})
        self.stdout.write(self.style.SUCCESS("config_import complete."))
