"""Stage 2 Phase 1 — Deal Scorecard routes.

These did not exist. The engine was built and unreachable; this is the wiring.
"""
from django.urls import path

from fundos.assessment import phase1_views, v2_api, views

urlpatterns = [
    # --- V2 Dedicated Assessment API Surface ---------------------------
    path("companies/<uuid:company_id>/assessment",
         v2_api.V2AssessmentView.as_view(),
         name="v2-assessment"),
    # The chat that sits beside the parameter panel. Company-scoped like the
    # rest of this surface — the panel holds a company id, and making it
    # resolve a deal first to ask about a row it already has open would be
    # friction for nothing. Deal-scoped twins live further down.
    path("companies/<uuid:company_id>/assessment/suggestions",
         v2_api.V2AssessmentSuggestionsView.as_view(),
         name="v2-assessment-suggestions"),
    path("companies/<uuid:company_id>/assessment/qa",
         v2_api.V2AssessmentQAView.as_view(),
         name="v2-assessment-qa"),
    path("companies/<uuid:company_id>/assessment/parameters/<str:ref>",
         v2_api.V2AssessmentParameterDetailView.as_view(),
         name="v2-assessment-parameter-detail"),
    path("assessment/reference",
         v2_api.V2AssessmentReferenceView.as_view(),
         name="v2-assessment-reference"),
    path("assessment/cohorts",
         v2_api.V2AssessmentCohortsView.as_view(),
         name="v2-assessment-cohorts"),

    # --- Phase 1 Deal Scorecard, as the React screen consumes it ---------
    # Company-scoped, not deal-scoped: a company may have several concurrent
    # raises and the scorecard is a statement about the company, not about one
    # of them. See fundos.assessment.phase1_views.
    path("companies/<uuid:company_id>/fundraising/phase1",
         phase1_views.FundraisingPhase1View.as_view(),
         name="fundraising-phase1"),
    path("companies/<uuid:company_id>/fundraising/phase1/override",
         phase1_views.FundraisingPhase1OverrideView.as_view(),
         name="fundraising-phase1-override"),

    path("deals/<uuid:deal_id>/assessment",
         views.AssessmentView.as_view(), name="assessment"),
    path("deals/<uuid:deal_id>/assessment/generate",
         views.AssessmentGenerateView.as_view(), name="assessment-generate"),
    path("deals/<uuid:deal_id>/assessment/parameters/<str:key>",
         views.ParameterDetailView.as_view(), name="assessment-parameter"),
    path("deals/<uuid:deal_id>/assessment/parameters/<str:key>/override",
         views.ParameterOverrideView.as_view(), name="assessment-override"),
    # What to ask about a node of the scorecard, at any level, and the chat
    # that answers it. An override the chat proposes is applied through the
    # existing override route below — one write path, one audit trail.
    path("deals/<uuid:deal_id>/assessment/suggestions",
         views.AssessmentSuggestionsView.as_view()),
    path("deals/<uuid:deal_id>/assessment/qa",
         views.AssessmentQAView.as_view()),
    path("deals/<uuid:deal_id>/assessment/findings",
         views.DiligenceFindingsView.as_view(), name="assessment-findings"),
    path("deals/<uuid:deal_id>/assessment/band-advancement",
         views.BandAdvancementView.as_view(), name="assessment-advancement"),
    path("deals/<uuid:deal_id>/assessment/versions",
         views.AssessmentVersionsView.as_view(), name="assessment-versions"),

    # --- v23 Phase 4 ---------------------------------------------------
    # Stage first in the list because it is first in the workflow: nothing
    # below is meaningful until the scorecard is banded against the right
    # cut-points.
    path("deals/<uuid:deal_id>/assessment/stage",
         views.AssessmentStageView.as_view(), name="assessment-stage"),
    path("deals/<uuid:deal_id>/assessment/coverage",
         views.AssessmentCoverageView.as_view(), name="assessment-coverage"),
    path("deals/<uuid:deal_id>/assessment/review-log",
         views.ReviewLogView.as_view(), name="assessment-review-log"),
    path("deals/<uuid:deal_id>/assessment/review-log/<uuid:entry_id>",
         views.ReviewLogEntryView.as_view(), name="assessment-review-entry"),
]
