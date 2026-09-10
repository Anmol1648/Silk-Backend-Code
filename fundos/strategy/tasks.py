"""Stage-2 Celery tasks — thin wrappers re-entering services with explicit
tenant/deal args (Doc 8 §5). Long generators run async; the API returns 202."""
import logging

from celery import shared_task

from fundos.core.scoping import tenant_context

logger = logging.getLogger(__name__)


def _run(tenant_id, deal_id, user_id, fn_name, job_id=None):
    with tenant_context(tenant_id):
        from fundos.core.models import Deal, User
        from fundos.core.services import jobs
        from fundos.strategy import services

        deal = Deal.objects.get(id=deal_id)
        user = User.objects.filter(id=user_id).first() if user_id else None
        try:
            fn = getattr(services, fn_name)
            # Gap G6: run under job tracking (running → succeeded/failed,
            # completion notification + alert).
            result = jobs.run_tracked(job_id, fn, deal, user=user)
            return str(getattr(result, "id", "ok"))
        except Exception as e:
            logger.error("STRATEGY: %s failed for deal %s: %s",
                         fn_name, deal_id, e)
            from fundos.core.alerting.tasks import emit_alert
            emit_alert("fundos.generation.failed",
                       {"deal_id": str(deal_id), "what": fn_name,
                        "error": str(e)[:200], "tenant_id": str(tenant_id)})
            raise


@shared_task(name="fundos.strategy.tasks.generate_peers_task", bind=True)
def generate_peers_task(self, tenant_id, deal_id, user_id=None, job_id=None):
    return _run(tenant_id, deal_id, user_id, "generate_peers", job_id=job_id)


@shared_task(name="fundos.strategy.tasks.generate_raise_task", bind=True)
def generate_raise_task(self, tenant_id, deal_id, user_id=None, job_id=None):
    return _run(tenant_id, deal_id, user_id, "generate_raise", job_id=job_id)


@shared_task(name="fundos.strategy.tasks.generate_valuation_task", bind=True)
def generate_valuation_task(self, tenant_id, deal_id, user_id=None, job_id=None):
    return _run(tenant_id, deal_id, user_id, "generate_valuation", job_id=job_id)


@shared_task(name="fundos.strategy.tasks.generate_blueprint_task", bind=True)
def generate_blueprint_task(self, tenant_id, deal_id, user_id=None, job_id=None):
    return _run(tenant_id, deal_id, user_id, "generate_blueprint", job_id=job_id)
