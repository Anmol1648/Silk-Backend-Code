"""
DealScopedViewSet (Doc 8 §3): (a) resolves deal_id from the URL,
(b) asserts membership(user, deal) else E-AUTHZ-403, (c) applies
.filter(deal_id=...) to get_queryset(). No RLS, no SQL rewriting.
"""
from rest_framework import viewsets
from rest_framework.views import APIView

from fundos.core import scoping
from fundos.core.permissions import require_action, require_membership


class DealScopedMixin:
    required_action = None            # declared per mutating endpoint (Doc 4 §0)
    deal_url_kwarg = "deal_id"

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        deal_id = kwargs.get(self.deal_url_kwarg)
        if deal_id:
            self.deal = require_membership(request.user, deal_id)
            scoping.set_current_deal(self.deal.id)
            if self.required_action and request.method not in ("GET", "HEAD", "OPTIONS"):
                require_action(request.user, self.deal, self.required_action)


class DealScopedAPIView(DealScopedMixin, APIView):
    pass


class DealScopedViewSet(DealScopedMixin, viewsets.GenericViewSet):
    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(deal_id=self.deal.id)


def queue_generation(view, request, kind, task, **task_kwargs):
    """Gap G6: enqueue an async generator and return a pollable job handle
    ({status, jobId, jobStatus, poll}) instead of a bare 202."""
    from rest_framework import status as drf_status
    from rest_framework.response import Response

    from fundos.core.services import jobs

    job = jobs.create_job(view.deal, kind, request.user)
    task.delay(str(view.deal.tenant_id), str(view.deal.id),
               str(request.user.id), job_id=str(job.id), **task_kwargs)
    job.refresh_from_db()   # dev runs eagerly — reflect the final status
    return Response({"status": "queued", "jobId": str(job.id),
                     "jobStatus": job.status,
                     "poll": f"/api/v1/deals/{view.deal.id}/jobs/{job.id}"},
                    status=drf_status.HTTP_202_ACCEPTED)
