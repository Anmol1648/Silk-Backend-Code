"""config_export — dump admin configuration (scoring config, master values,
flags, alerts, LLM registry) to JSON for env promotion (Doc 9 §promotion)."""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Export FundOS configuration to JSON (stdout or --out FILE)."

    def add_arguments(self, parser):
        parser.add_argument("--out", default=None)

    def handle(self, *args, **options):
        from fundos.config.models import (
            AppConfiguration, InvestorCategoryMap, MasterValue,
            ResearchSourceFlags, ScoringConfig, SectorTaxonomyMap,
        )
        from fundos.core.models import AlertDefinition
        from fundos.llm.models import LLMEndpoint, LLMRoleBinding

        cfg = AppConfiguration.get_solo()
        payload = {
            "appConfiguration": {
                f.name: getattr(cfg, f.name) for f in
                AppConfiguration._meta.fields
                if f.name not in ("id",)
            } if cfg else None,
            "scoringConfigs": [
                {"versionNo": c.version_no, "isActive": c.is_active,
                 "data": c.data}
                for c in ScoringConfig.objects.order_by("version_no")],
            "researchSourceFlags": [
                {"sourceType": f.source_type, "enabled": f.enabled,
                 "legalCleared": f.legal_cleared}
                for f in ResearchSourceFlags.objects.all()],
            "masterValues": [
                {"table": v.table, "code": v.code, "label": v.label,
                 "meta": v.meta, "sortOrder": v.sort_order,
                 "isActive": v.is_active}
                for v in MasterValue.objects.all()],
            "investorCategoryMap": [
                {"roundType": r.round_type, "raiseBand": r.raise_band,
                 "bandLowUsd": float(r.band_low_usd),
                 "bandHighUsd": float(r.band_high_usd),
                 "primary": r.primary, "secondary": r.secondary,
                 "optional": r.optional}
                for r in InvestorCategoryMap.objects.all()],
            "sectorTaxonomyMap": [
                {"founderSector": s.founder_sector,
                 "canonicalSector": s.canonical_sector,
                 "adjacentSectors": s.adjacent_sectors}
                for s in SectorTaxonomyMap.objects.all()],
            "alertDefinitions": [
                {"name": a.name, "sourceType": a.source_type,
                 "eventKey": a.event_key, "heading": a.heading,
                 "messageTemplate": a.insight_user_query,
                 "notifyEmail": a.notify_email,
                 "notifyInApp": a.notify_in_app,
                 "notifyWhatsapp": a.notify_whatsapp,
                 "isActive": a.is_active}
                for a in AlertDefinition.objects.all()],
            "llmEndpoints": [
                {"code": e.code, "providerKind": e.provider_kind,
                 "baseUrl": e.base_url, "defaultModel": e.default_model,
                 "apiKeyEnvVar": e.api_key_env_var,
                 "isActive": e.is_active, "priority": e.priority}
                for e in LLMEndpoint.objects.all()],
            "llmRoleBindings": [
                {"role": b.role, "primary": b.primary_endpoint_id,
                 "fallback": b.fallback_endpoint_id,
                 "isMocked": b.is_mocked}
                for b in LLMRoleBinding.objects.all()],
        }
        text = json.dumps(payload, indent=2, default=str)
        if options["out"]:
            with open(options["out"], "w") as fh:
                fh.write(text)
            self.stdout.write(f"exported to {options['out']}")
        else:
            self.stdout.write(text)
