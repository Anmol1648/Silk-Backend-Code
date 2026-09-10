"""Stage-3 Celery tasks — thin async wrappers over the services."""
import logging

from celery import shared_task

from fundos.core.scoping import tenant_context

logger = logging.getLogger(__name__)


def _run(tenant_id, deal_id, user_id, fn_name, job_id=None, **kwargs):
    with tenant_context(tenant_id):
        from fundos.core.models import Deal, User
        from fundos.core.services import jobs
        from fundos.materials import services

        deal = Deal.objects.get(id=deal_id)
        user = User.objects.filter(id=user_id).first() if user_id else None
        try:
            # Gap G6: run under job tracking.
            result = jobs.run_tracked(job_id, getattr(services, fn_name),
                                      deal, user=user, **kwargs)
            return str(getattr(result, "id", "ok"))
        except Exception as e:
            logger.error("MATERIALS: %s failed for deal %s: %s",
                         fn_name, deal_id, e)
            from fundos.core.alerting.tasks import emit_alert
            emit_alert("fundos.generation.failed",
                       {"deal_id": str(deal_id), "what": fn_name,
                        "error": str(e)[:200], "tenant_id": str(tenant_id)})
            raise


@shared_task(name="fundos.materials.tasks.generate_story_task", bind=True)
def generate_story_task(self, tenant_id, deal_id, user_id=None, job_id=None):
    return _run(tenant_id, deal_id, user_id, "generate_story", job_id=job_id)


@shared_task(name="fundos.materials.tasks.generate_teaser_task", bind=True)
def generate_teaser_task(self, tenant_id, deal_id, user_id=None, job_id=None):
    return _run(tenant_id, deal_id, user_id, "generate_teaser", job_id=job_id)


@shared_task(name="fundos.materials.tasks.generate_im_task", bind=True)
def generate_im_task(self, tenant_id, deal_id, user_id=None, job_id=None):
    return _run(tenant_id, deal_id, user_id, "generate_im", job_id=job_id)


@shared_task(name="fundos.materials.tasks.generate_model_task", bind=True)
def generate_model_task(self, tenant_id, deal_id, user_id=None,
                        horizon_months=36, job_id=None):
    return _run(tenant_id, deal_id, user_id, "generate_financial_model",
                job_id=job_id, horizon_months=horizon_months)


@shared_task(name="fundos.materials.tasks.run_package_review_task", bind=True)
def run_package_review_task(self, tenant_id, deal_id, user_id=None, job_id=None):
    return _run(tenant_id, deal_id, user_id, "run_package_review", job_id=job_id)
