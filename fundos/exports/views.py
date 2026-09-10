"""
Export endpoints (Gaps G4/G14 — Doc 4 contract):

  POST /deals/{id}/teaser/export   {"format": "pdf|pptx|docx"}
  POST /deals/{id}/deck/export     {"format": "pptx|pdf|notes"}
  POST /deals/{id}/model/export    {"format": "xlsx|csv|pdf"}   (xlsx = live formulas)
  POST /deals/{id}/im/export       {"format": "pdf|docx|watermarked"}
  GET  /deals/{id}/readiness/report?format=pdf
  GET  /deals/{id}/strategy/report?format=pdf
  GET  /deals/{id}/review/report?format=pdf

Each returns {"downloadUrl", "storageUri", "format", "expiresInSeconds"} —
a short-lived signed URL from the configured storage backend.
"""
from rest_framework.response import Response

from fundos.core.api.base import DealScopedAPIView
from fundos.exports import service


class _ExportView(DealScopedAPIView):
    required_action = "materials.generate"
    default_format = "pdf"
    exporter = None

    def post(self, request, deal_id):
        fmt = (request.data.get("format") or self.default_format).lower()
        return Response(type(self).exporter(self.deal, fmt,
                                            user=request.user))


class TeaserExportView(_ExportView):
    exporter = staticmethod(service.export_teaser)


class DeckExportView(_ExportView):
    default_format = "pptx"
    exporter = staticmethod(service.export_deck)


class ModelExportView(_ExportView):
    default_format = "xlsx"
    exporter = staticmethod(service.export_model)


class ImExportView(_ExportView):
    exporter = staticmethod(service.export_im)


class ReadinessReportView(DealScopedAPIView):
    def get(self, request, deal_id):
        return Response(service.export_readiness_report(self.deal,
                                                        user=request.user))


class StrategyReportView(DealScopedAPIView):
    def get(self, request, deal_id):
        return Response(service.export_strategy_report(self.deal,
                                                       user=request.user))


class ReviewReportView(DealScopedAPIView):
    def get(self, request, deal_id):
        return Response(service.export_review_report(self.deal,
                                                     user=request.user))
