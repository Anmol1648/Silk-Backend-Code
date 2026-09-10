"""Diagnostics routes — health, runs, and correlation-ID trace."""
from django.urls import path

from fundos.diagnostics import views

urlpatterns = [
    path("health", views.HealthView.as_view(), name="diag-health"),
    path("admin/diagnostics/runs", views.RunListView.as_view(),
         name="diag-runs"),
    path("admin/diagnostics/trace/<uuid:correlation_id>",
         views.TraceView.as_view(), name="diag-trace"),
]
