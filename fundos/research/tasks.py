"""
Research tasks (Doc 1 M1-§1 step 1.1) — non-blocking, per-source isolation:
an unreachable adapter marks that source failed and research proceeds
(ERR-M1-001). Thin wrappers re-entering services with explicit tenant/deal
args (Doc 8 §5); idempotent (safe to re-run).
"""
import logging

from celery import shared_task
from django.utils import timezone

from fundos.core.scoping import tenant_context

logger = logging.getLogger(__name__)


@shared_task(name="fundos.research.tasks.run_research", bind=True)
def run_research(self, tenant_id, deal_id, sources=None, user_id=None,
                 job_id=None):
    from fundos.core.services import jobs
    return jobs.run_tracked(job_id, _run_research, tenant_id, deal_id,
                            sources, user_id)


def _run_research(tenant_id, deal_id, sources=None, user_id=None):
    with tenant_context(tenant_id):
        from fundos.core.models import Deal, User
        from fundos.core.services import ckb_service
        from fundos.research.adapters.registry import ADAPTERS, enabled_sources
        from fundos.research.models import CompanyResearchSource
        from fundos.research.synthesis import synthesise

        deal = Deal.objects.get(id=deal_id)
        user = User.objects.filter(id=user_id).first() if user_id else None
        snapshot = ckb_service.snapshot(deal.id)
        runnable = enabled_sources(sources)

        payloads = {}
        for source_type in runnable:
            row, _ = CompanyResearchSource.all_objects.get_or_create(
                deal_id=deal.id, source_type=source_type, is_deleted=False,
                defaults={"tenant_id": deal.tenant_id})
            row.status = "pending"
            row.error_message = ""
            row.save(update_fields=["status", "error_message", "updated_at"])
            try:
                adapter = ADAPTERS[source_type](deal, snapshot)
                payload = adapter.fetch()
                row.payload = payload
                row.confidence = payload.get("confidence")
                row.status = "success"
                row.fetched_at = timezone.now()
                payloads[source_type] = payload
            except Exception as e:
                # ERR-M1-001 — never blocks; field stays Missing and surfaces
                # in the gap analysis.
                row.status = "failed"
                row.error_message = str(e)[:1000]
                logger.warning("RESEARCH: %s failed for deal %s: %s",
                               source_type, deal_id, e)
            row.save()

        result = {"synthesised": 0, "inconsistencies": 0}
        if payloads:
            try:
                synth = synthesise(deal, payloads, user=user)
                result = {"synthesised": len(synth["proposed"]),
                          "inconsistencies": len(synth["inconsistencies"])}
            except Exception as e:
                logger.error("RESEARCH: synthesis failed for deal %s: %s",
                             deal_id, e)
                from fundos.core.alerting.tasks import emit_alert
                emit_alert("fundos.generation.failed",
                           {"deal_id": str(deal_id), "what": "research_synthesis",
                            "error": str(e)[:200], "tenant_id": str(tenant_id)})

        from fundos.core.services.stage_state import recompute_stage_completion
        recompute_stage_completion(deal.id)
        return {"sources_run": runnable, **result}
