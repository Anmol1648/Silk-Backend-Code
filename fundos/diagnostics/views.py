"""Diagnostics API.

`health` is open to any authenticated user — it says whether the system is
working, not what is in it. The run list and the trace are staff-only, because
run inputs can carry company identifiers.
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fundos.diagnostics.models import DiagEvent, RunRecord
from fundos.diagnostics.services import health


class HealthView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(health())


class _StaffOnly(APIView):
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not (request.user.is_staff or request.user.is_superuser):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Staff only.")


class RunListView(_StaffOnly):
    def get(self, request):
        qs = RunRecord.objects.all()
        for field in ("kind", "status"):
            value = request.query_params.get(field)
            if value:
                qs = qs.filter(**{field: value})
        deal_id = request.query_params.get("dealId")
        if deal_id:
            qs = qs.filter(deal_id=deal_id)
        return Response({"items": [{
            "id": str(r.id), "correlationId": str(r.correlation_id),
            "kind": r.kind, "subject": r.subject, "status": r.status,
            "counts": r.counts, "error": r.error,
            "configVersion": r.config_version,
            "startedAt": r.started_at, "durationMs": r.duration_ms,
        } for r in qs[:100]]})


class TraceView(_StaffOnly):
    """Everything that happened under one correlation ID.

    This is the answer to "why did this company's score come out wrong" — a
    filter rather than a reconstruction.
    """

    def get(self, request, correlation_id):
        runs = RunRecord.objects.filter(correlation_id=correlation_id)
        events = DiagEvent.objects.filter(
            correlation_id=correlation_id).order_by("created_at")[:500]
        if not runs and not events:
            return Response({"error": "E-NOTFOUND-404",
                             "detail": "No activity for that correlation ID."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({
            "correlationId": str(correlation_id),
            "runs": [{"id": str(r.id), "kind": r.kind, "subject": r.subject,
                      "status": r.status, "counts": r.counts,
                      "error": r.error, "startedAt": r.started_at,
                      "durationMs": r.duration_ms} for r in runs],
            "events": [{"level": e.level, "component": e.component,
                        "stage": e.stage, "message": e.message,
                        "data": e.data, "at": e.created_at,
                        "location": e.source_location} for e in events],
        })
