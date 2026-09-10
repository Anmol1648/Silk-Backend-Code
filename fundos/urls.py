"""
Root URL configuration (Doc 4 API design).

Deal scoping is in the URL for readability, but AUTHORITY comes from
membership checks — the tenant is fixed at login in the JWT, never derived
from URLs (Doc 2 §0.2).
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from fundos.core.api import admin_views, views as core
from fundos.exports import views as exports
from fundos.materials import views as m3
from fundos.platformcfg import views as cfg
from fundos.profile import views as profile
from fundos.profile import records as profile_records
from fundos.readiness import views as m1
from fundos.strategy import views as m2

V1 = "api/v1"
DEAL = f"{V1}/deals/<uuid:deal_id>"
COMPANY = f"{V1}/companies/<uuid:company_id>"

urlpatterns = [
    # Django admin. `django.contrib.admin` was installed and the models
    # registered, but no URL was ever mounted — so /admin returned 404
    # regardless of the web-tier proxy (QA 24-Jul issue 9). Mounted on the
    # canonical trailing-slash path; Django's APPEND_SLASH redirects /admin.
    path("admin/", admin.site.urls),

    # --- Auth & identity (M0) ---
    path(f"{V1}/auth/otp/request", core.OtpRequestView.as_view()),
    path(f"{V1}/auth/otp/verify", core.OtpVerifyView.as_view()),
    path(f"{V1}/auth/refresh", core.TokenRefreshView.as_view()),
    path(f"{V1}/auth/signup", core.SignupView.as_view()),          # G2
    path(f"{V1}/me/contexts", core.MeContextsView.as_view()),
    path(f"{V1}/contexts/switch", core.ContextSwitchView.as_view()),  # G3

    # --- Onboarding: companies & deals (G2) ---
    path(f"{V1}/companies", core.CompaniesView.as_view()),
    path(f"{V1}/companies/<uuid:company_id>",
         core.CompanyDetailView.as_view()),
    path(f"{V1}/companies/<uuid:company_id>/deals",
         core.CompanyDealsView.as_view()),
    # Company logo. The client called this from day one; the route was never
    # mounted, so every upload 404'd.
    path(f"{V1}/companies/<uuid:company_id>/logo",
         core.CompanyLogoView.as_view()),
    # --- Platform configuration (admin-owned, read-only to clients) ---
    path(f"{V1}/config/app", cfg.AppConfigView.as_view()),
    path(f"{V1}/config/countries", cfg.CountriesView.as_view()),
    path(f"{V1}/config/buckets", cfg.BucketsView.as_view()),
    path(f"{V1}/config/profile-sections",
         cfg.ProfileSectionsConfigView.as_view()),
    path(f"{V1}/config/ui-copy", cfg.UiCopyView.as_view()),
    path(f"{V1}/config/lookups", cfg.LookupsView.as_view()),

    # --- Stage 1: Company Profile (company-scoped per C1) ---
    path(f"{COMPANY}/profile", profile.ProfileView.as_view()),
    path(f"{COMPANY}/profile/onboard", profile.ProfileOnboardView.as_view()),
    path(f"{COMPANY}/profile/review", profile.ProfileReviewView.as_view()),
    path(f"{COMPANY}/profile/deep-generate",
         profile.ProfileDeepGenerateView.as_view()),
    path(f"{COMPANY}/profile/qa", profile.ProfileQAView.as_view()),
    # Generation runs. A profile build is a long, multi-stage job with real
    # sub-progress; before these routes the only signal a client had was the
    # profile's status flipping, which cannot tell "still researching" from
    # "wedged". The detail view also serves the run's dossier — the source
    # material the profile was actually derived from.
    path(f"{COMPANY}/profile/runs", profile.ProfileRunsView.as_view()),
    path(f"{COMPANY}/profile/runs/<uuid:run_id>",
         profile.ProfileRunDetailView.as_view()),
    path(f"{COMPANY}/profile/sections/<str:section_key>",
         profile.ProfileSectionView.as_view()),
    path(f"{COMPANY}/profile/sections/<str:section_key>/regenerate",
         profile.ProfileSectionRegenerateView.as_view()),
    path(f"{COMPANY}/profile/sections/<str:section_key>/history",
         profile.ProfileSectionHistoryView.as_view()),
    # Numeric/structured forms (Revenue Model, Company Metrics, Financial
    # Summary) — stored on the section's `structured` JSON, validated
    # server-side.
    path(f"{COMPANY}/profile/sections/<str:section_key>/structured",
         profile.ProfileStructuredFormView.as_view()),
    # Field-level AI regeneration of a single field/row.
    path(f"{COMPANY}/profile/sections/<str:section_key>/fields/"
         f"<str:field_key>/regenerate",
         profile.ProfileFieldRegenerateView.as_view()),
    # Field-level source citations (Req 5).
    path(f"{COMPANY}/profile/fields/<str:field_id>/sources",
         profile.ProfileFieldSourcesView.as_view()),
    # Generic list-record CRUD (Key People, Competitors, Funding History,
    # Recent News) — one view, driven by records.RECORD_TYPES.
    path(f"{COMPANY}/profile/records/<str:section_key>",
         profile_records.ProfileRecordView.as_view()),
    path(f"{COMPANY}/profile/records/<str:section_key>/<uuid:record_id>",
         profile_records.ProfileRecordView.as_view()),
    path(f"{COMPANY}/founders", profile.FoundersView.as_view()),
    path(f"{COMPANY}/founders/<uuid:founder_id>",
         profile.FounderDetailView.as_view()),
    path(f"{COMPANY}/cash-position", profile.CashPositionView.as_view()),
    path(f"{COMPANY}/fundraise-recommendation",
         profile.FundraiseRecommendationView.as_view()),
    # C6 — manual headline terms; lets a founder skip the guided strategy.
    path(f"{COMPANY}/deal-targets", profile.DealTargetsView.as_view()),
    path(f"{COMPANY}/documents", profile.CompanyDocumentsView.as_view()),
    path(f"{COMPANY}/documents/<uuid:document_id>",
         profile.CompanyDocumentsView.as_view()),
    path(f"{COMPANY}/default-deal",
         profile.CompanyDefaultDealView.as_view()),

    path(f"{V1}/notifications", core.NotificationsView.as_view()),
    path(f"{V1}/notifications/<uuid:notification_id>/read",
         core.NotificationReadView.as_view()),

    # --- Masterplan / stages / members / CKB (M0) ---
    path(f"{DEAL}/masterplan", core.MasterplanView.as_view()),
    path(f"{DEAL}/stage-state", core.StageStateView.as_view()),
    path(f"{DEAL}/dashboard", core.DashboardView.as_view()),
    path(f"{DEAL}/jobs", core.JobsView.as_view()),                 # G6
    path(f"{DEAL}/jobs/<uuid:job_id>", core.JobDetailView.as_view()),
    path(f"{DEAL}/stages/<int:stage_no>/override",
         core.StageOverrideView.as_view()),
    path(f"{DEAL}/members", core.MembersView.as_view()),
    path(f"{DEAL}/members/<uuid:user_id>", core.MemberDetailView.as_view()),
    path(f"{DEAL}/ckb", core.CkbView.as_view()),
    path(f"{DEAL}/ckb/suggestions/<str:field_key>",
         core.CkbSuggestionActionView.as_view()),
    path(f"{DEAL}/ckb/fields/<str:field_key>",
         core.CkbFieldView.as_view()),   # Doc 4 per-field verb
    path(f"{DEAL}/ckb/assign", core.CkbSectionAssignView.as_view()),

    # --- Stage 1 (M1) ---
    path(f"{DEAL}/research/run", m1.ResearchRunView.as_view()),
    path(f"{DEAL}/research/sources", m1.ResearchSourcesView.as_view()),
    path(f"{DEAL}/materials", m1.MaterialsView.as_view()),
    path(f"{DEAL}/materials/<uuid:material_id>",
         m1.MaterialDetailView.as_view()),
    path(f"{DEAL}/materials/<uuid:material_id>/links/<uuid:link_id>/accept",
         m1.MaterialLinkMetricView.as_view()),
    path(f"{DEAL}/uploads/sessions", m1.UploadSessionView.as_view()),
    path(f"{DEAL}/uploads/sessions/<uuid:session_id>/chunks/<int:chunk_no>",
         m1.UploadChunkView.as_view()),
    path(f"{DEAL}/uploads/sessions/<uuid:session_id>/complete",
         m1.UploadCompleteView.as_view()),
    path(f"{DEAL}/readiness/generate", m1.ReadinessGenerateView.as_view()),
    path(f"{DEAL}/readiness", m1.ReadinessView.as_view()),
    path(f"{DEAL}/readiness/gaps", m1.ReadinessGapsView.as_view()),
    path(f"{DEAL}/readiness/gaps/<uuid:gap_id>/action",
         m1.ReadinessGapActionView.as_view()),
    path(f"{DEAL}/readiness/report",
         exports.ReadinessReportView.as_view()),                   # G4

    # --- Stage 2 (M2) ---
    path(f"{DEAL}/strategy/objectives", m2.ObjectivesView.as_view()),
    path(f"{DEAL}/strategy/peers/generate", m2.PeersGenerateView.as_view()),
    path(f"{DEAL}/strategy/peers", m2.PeersView.as_view()),
    path(f"{DEAL}/strategy/peers/curate", m2.PeerCurateView.as_view()),
    path(f"{DEAL}/strategy/peers/add", m2.PeerRestView.as_view()),
    path(f"{DEAL}/strategy/peers/<uuid:peer_id>",
         m2.PeerDetailView.as_view()),   # GET + DELETE (BR-S2-013)
    path(f"{DEAL}/strategy/raise/generate", m2.RaiseGenerateView.as_view()),
    path(f"{DEAL}/strategy/raise", m2.RaiseView.as_view()),
    path(f"{DEAL}/strategy/raise/scenario",
         m2.RaiseScenarioSelectView.as_view()),
    path(f"{DEAL}/strategy/valuation/generate",
         m2.ValuationGenerateView.as_view()),
    path(f"{DEAL}/strategy/valuation", m2.ValuationView.as_view()),
    path(f"{DEAL}/strategy/instruments", m2.InstrumentsView.as_view()),
    path(f"{DEAL}/strategy/instruments/<uuid:instrument_id>",
         m2.InstrumentSelectView.as_view()),
    path(f"{DEAL}/strategy/dilution/simulate",
         m2.DilutionSimulateView.as_view()),
    path(f"{DEAL}/strategy/blueprint/generate",
         m2.BlueprintGenerateView.as_view()),
    path(f"{DEAL}/strategy/blueprint", m2.BlueprintView.as_view()),
    path(f"{DEAL}/strategy/approve", m2.StrategyApproveView.as_view()),
    path(f"{DEAL}/strategy/profile", m2.StrategyProfileView.as_view()),
    path(f"{DEAL}/strategy/report", exports.StrategyReportView.as_view()),

    # --- Stage 3 (M3) ---
    path(f"{DEAL}/workspace", m3.WorkspaceView.as_view()),
    path(f"{DEAL}/story/generate", m3.StoryGenerateView.as_view()),
    path(f"{DEAL}/story", m3.StoryView.as_view()),
    path(f"{DEAL}/story/approve", m3.StoryApproveView.as_view()),
    path(f"{DEAL}/teaser/generate", m3.TeaserGenerateView.as_view()),
    path(f"{DEAL}/teaser", m3.TeaserView.as_view()),
    path(f"{DEAL}/teaser/export", exports.TeaserExportView.as_view()),
    path(f"{DEAL}/deck/outline", m3.DeckOutlineView.as_view()),
    path(f"{DEAL}/deck/approve-outline",
         m3.DeckApproveOutlineView.as_view()),
    path(f"{DEAL}/deck/slides", m3.DeckSlidesView.as_view()),
    path(f"{DEAL}/deck/export", exports.DeckExportView.as_view()),
    path(f"{DEAL}/model/generate", m3.ModelGenerateView.as_view()),
    path(f"{DEAL}/model/assumptions", m3.ModelAssumptionsView.as_view()),
    path(f"{DEAL}/model/statements", m3.ModelStatementsView.as_view()),
    path(f"{DEAL}/model/export", exports.ModelExportView.as_view()),
    path(f"{DEAL}/im/generate", m3.ImGenerateView.as_view()),
    path(f"{DEAL}/im", m3.ImView.as_view()),
    path(f"{DEAL}/im/export", exports.ImExportView.as_view()),
    path(f"{DEAL}/review/run", m3.ReviewRunView.as_view()),
    path(f"{DEAL}/review", m3.ReviewView.as_view()),
    path(f"{DEAL}/review/report", exports.ReviewReportView.as_view()),
    path(f"{DEAL}/package/approve", m3.PackageApproveView.as_view()),
    path(f"{DEAL}/package", m3.PackageView.as_view()),

    # --- Signed file downloads (local backend; S3/OCI presign natively) ---
    path(f"{V1}/files/<path:remote_path>", core.SignedFileView.as_view()),

    # --- Platform admin ---
    path(f"{V1}/admin/config", admin_views.AppConfigurationView.as_view()),
    path(f"{V1}/admin/alerts", admin_views.AlertDefinitionsView.as_view()),
    path(f"{V1}/admin/alerts/<uuid:alert_id>",
         admin_views.AlertDefinitionDetailView.as_view()),
    path(f"{V1}/admin/alerts/<uuid:alert_id>/evaluate-now",
         admin_views.AlertEvaluateNowView.as_view()),
    path(f"{V1}/admin/llm/endpoints", admin_views.LlmEndpointsView.as_view()),
    path(f"{V1}/admin/llm/bindings", admin_views.LlmBindingsView.as_view()),
    path(f"{V1}/admin/scoring-config", admin_views.ScoringConfigView.as_view()),
    path(f"{V1}/admin/master/<str:table>",
         admin_views.MasterValuesView.as_view()),

    # --- Stage 2 Phase 1: Deal Scorecard -------------------------------------
    # The scoring engine was built, configured and unreachable: no views, no
    # routes, nothing outside tests creating a ParameterValue. This is the
    # wiring, not new scoring logic.
    path(f"{V1}/", include("fundos.assessment.urls")),

    # --- Stage 3: Investor Discovery -----------------------------------------
    path(f"{V1}/", include("fundos.investors.urls")),

    # --- Diagnostics: health, run history, correlation trace -----------------
    path(f"{V1}/", include("fundos.diagnostics.urls")),
]
