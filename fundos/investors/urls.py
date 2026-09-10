"""Stage 3 — Investor Discovery routes.

Deal-scoped, following the codebase's existing pattern. The match itself is a
GET because it is a query, not a mutation — every filter is a parameter and
nothing is stored, so a discovery view is bookmarkable and shareable.
"""
from django.urls import path

from fundos.investors import views

urlpatterns = [
    path("deals/<uuid:deal_id>/investors/filters",
         views.DiscoveryFiltersView.as_view(), name="investor-filters"),
    path("deals/<uuid:deal_id>/investors/search",
         views.DiscoverySearchView.as_view(), name="investor-search"),
    path("deals/<uuid:deal_id>/investors/<uuid:investor_id>",
         views.InvestorDetailView.as_view(), name="investor-detail"),
    path("deals/<uuid:deal_id>/investors/shortlist",
         views.ShortlistView.as_view(), name="investor-shortlist"),
    path("deals/<uuid:deal_id>/investors/export",
         views.DiscoveryExportView.as_view(), name="investor-export"),
    path("admin/investors/match-preview",
         views.MatchPreviewView.as_view(), name="investor-match-preview"),
]
