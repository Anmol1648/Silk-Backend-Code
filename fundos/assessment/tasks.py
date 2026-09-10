"""Assessment generation: extract evidence, score, then review.

    documents → ParameterValue rows → run_scoring() → AI rubric review

THE SEPARATION THAT MAKES THE SCORECARD DEFENSIBLE
--------------------------------------------------
Extraction produces FACTS with provenance and never opinions. It writes
`raw_value`, `source_type`, `source_detail`, `source_tier` and `confidence`.
For numeric parameters it must NEVER write `band` or `score` — the engine
bands them from published config, and that is what lets us say "here is the
fact, here is the published threshold" instead of "the AI decided".

The one exception is anchor-scored parameters, where `band_parameter()`
explicitly expects a pre-banded result from an upstream call and re-validates
it against the stored anchors.

THE AI RUBRIC REVIEW (D-08)
---------------------------
The business decision: the review flags and explains, AND MAY RE-BAND WHEN
VERY CONFIDENT. So re-banding is permitted above a high confidence floor, and
constrained so it stays auditable:

  * It only fires at or above REBAND_CONFIDENCE_FLOOR (0.90 by default,
    configurable). Below that it flags and explains, and a human decides.
  * It may move a band by ONE step. A two-step jump is not a threshold
    correction, it is a different opinion, and it goes to a human.
  * `system_band` and `system_score` are left untouched, so the original
    rule-based result survives beside the adjustment forever.
  * Every re-band writes an audit finding naming the old band, the new band,
    the confidence and the reason. A re-band that leaves no trace would make
    the scorecard unreproducible, which is the one property it cannot lose.
"""
import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

REBAND_CONFIDENCE_FLOOR = 0.90
BAND_ORDER = ["poor", "fair", "good", "excellent"]
BAND_SCORES = {"poor": 3.0, "fair": 5.0, "good": 7.0, "excellent": 9.0}


@shared_task(bind=True, name="assessment.generate")
def generate_assessment_task(self, tenant_id, deal_id, user_id, job_id=None,
                             **kwargs):
    """Full pipeline for one deal."""
    from fundos.core.models import Deal
    from fundos.core.scoping import tenant_context
    from fundos.core.services import jobs

    with tenant_context(tenant_id):
        deal = Deal.objects.get(id=deal_id)
        # `jobs` exposes ONE tracking entry point — `run_tracked` — and its
        # module docstring names it as the way a task wraps a service call.
        # This previously called `jobs.get_job`, `jobs.complete_job` and
        # `jobs.fail_job`, none of which exist, so every generation died with
        # an AttributeError at the first line of the try block and the deal
        # never got an assessment at all. `run_tracked` does what those three
        # were reaching for and more: running -> succeeded/failed, the
        # artefact id off the returned Assessment, the error text, the
        # `fundos.generation.*` alert and the requester's in-app notification.
        return str(jobs.run_tracked(
            job_id, generate_assessment, deal, user_id=user_id,
            deal_stage=kwargs.get("deal_stage")).id)


def resolve_initial_stage(deal, requested=None):
    """Pick the stage this assessment will be banded against.

    §2.5 makes stage the most consequential single input: it selects the
    cut-point column for every numeric parameter, so the same 14-month runway
    is Fair at Series A and Good at Growth. Order of preference is the order
    of authority — an explicit human choice, then the deal's own recorded
    stage, then nothing, which `resolve_stage()` defaults loudly.
    """
    from fundos.assessment.models import DEAL_STAGES

    valid = {s for s, _ in DEAL_STAGES}
    if requested:
        match = next((s for s in valid
                      if s.lower() == str(requested).strip().lower()), None)
        if match:
            return match
        logger.warning(
            "ASSESSMENT: requested stage %r is not one of %s — falling back "
            "to the deal's own stage.", requested, sorted(valid))
    for obj in (deal, getattr(deal, "company", None)):
        if not obj:
            continue
        for attr in ("assessment_stage", "stage", "round_stage", "deal_stage"):
            value = getattr(obj, attr, None)
            match = next((s for s in valid
                          if value and s.lower() == str(value).strip().lower()),
                         None)
            if match:
                return match
    return ""


def _stored_dossier(profile):
    """The consolidated dossier the profile's last run wrote, or "".

    Read from object storage by the URI the run recorded. Never raises: this
    feeds a recovery path, and a storage failure there should degrade the
    retry rather than replace one broken state with an exception.
    """
    try:
        from fundos.profile.models import ProfileGenerationRun

        run = (ProfileGenerationRun.objects
               .filter(profile=profile)
               .exclude(dossier_uri="")
               .order_by("-created_at").first())
        if run is None or not run.dossier_uri:
            return ""
        # Pulled to a temp file: the storage backend hands out files, not
        # bytes, and object storage is not the local filesystem.
        import os
        import tempfile

        from fundos.docs.storage import get_storage

        fd, local = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        try:
            if not get_storage().download_file(run.dossier_uri, local):
                return ""
            with open(local, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        finally:
            try:
                os.unlink(local)
            except OSError:
                pass
    except Exception as exc:
        logger.warning("ASSESSMENT: could not read the stored dossier: %s",
                       exc)
        return ""


def _recover_profile_inputs(assessment, *, user=None):
    """Run step 1's parameter extraction now, if the profile has none.

    WHY THIS EXISTS
    ---------------
    `assessment_inputs` is one LLM call inside a thirteen-minute profile run.
    When it fails — three `gemini 503`s in a row on 27 Aug — the profile
    itself still publishes, `ProfileAssessmentInput` stays empty, and the
    bridge finds nothing to seed. The scorecard then renders every row blank,
    and the only remedy the system offered was the one in the bridge's own
    log line: "Regenerate the company profile." That is 100 research
    questions, a 600,000-character synthesis and about thirty rupees, to
    recover from a single call that a provider refused.

    The profile is already written and its dossier is already stored, so the
    missing call can simply be made again on its own. One call, seconds, and
    the run that failed is not repeated.

    Never raises: this is a recovery path, and an assessment scored from
    uploaded documents alone is still worth having. A failure here leaves
    exactly the state we were already in.
    """
    from fundos.profile.models import CompanyProfile

    profile = CompanyProfile.objects.filter(
        company_id=assessment.company_id).order_by("-updated_at").first()
    if profile is None or profile.status != "generated":
        return None

    logger.warning(
        "ASSESSMENT %s: the profile has no assessment inputs — re-running "
        "step 1's extraction rather than asking for a full regeneration.",
        assessment.id)
    try:
        from fundos.profile.assessment_extraction import (
            extract_assessment_inputs)
        from fundos.profile.services import collect_sources

        payloads, _ = collect_sources(profile, user=user)
        # The dossier the failed run already built and stored. Without it this
        # retry would see only the short per-file summaries and answer far
        # fewer parameters than the run it is recovering — a cheaper call
        # that produces a worse scorecard is not a recovery.
        dossier = _stored_dossier(profile)
        if dossier:
            payloads["dossier"] = dossier
            logger.info("ASSESSMENT %s: recovered the stored dossier "
                        "(%d chars).", assessment.id, len(dossier))
        counts = extract_assessment_inputs(profile, payloads=payloads,
                                           user=user)
        logger.info("ASSESSMENT %s: recovery extraction %s",
                    assessment.id, counts)
        if not counts.get("written"):
            return None
        from fundos.assessment.profile_bridge import seed_from_profile
        bridged = seed_from_profile(assessment, profile=profile)
        logger.info("ASSESSMENT %s: profile bridge after recovery %s",
                    assessment.id, bridged)
        return bridged
    except Exception as exc:
        logger.error(
            "ASSESSMENT %s: recovery extraction failed (%s). Scoring from "
            "uploaded documents alone.", assessment.id, exc, exc_info=True)
        return None


def generate_assessment(deal, *, user_id=None, deal_stage=None):
    """Create a versioned assessment, extract, score and review.

    Never overwrites: a previous active assessment is marked superseded and
    kept. Re-running after new documents arrive should produce a comparable
    second opinion, not the destruction of the first one (D-10).
    """
    from django.contrib.auth import get_user_model
    from fundos.assessment.models import Assessment
    from fundos.assessment.services import run_scoring

    User = get_user_model()
    user = User.objects.filter(id=user_id).first() if user_id else None
    company = deal.company

    previous = (Assessment.objects.filter(deal=deal)
                .exclude(status="superseded").order_by("-created_at").first())

    assessment = Assessment.objects.create(
        tenant_id=getattr(deal, "tenant_id", None),
        company=company, deal=deal, created_by=user,
        deal_stage=resolve_initial_stage(deal, deal_stage),
        sector=getattr(company, "sector", "") or "",
        sub_sector=getattr(company, "sub_sector", "") or "",
        assessment_date=timezone.now().date(),
        status="draft")

    if previous:
        previous.status = "superseded"
        previous.superseded_by = assessment
        previous.save(update_fields=["status", "superseded_by"])
        logger.info("ASSESSMENT: %s superseded by %s", previous.id,
                    assessment.id)

    # Phase 3 — step 1's research reaches step 2's rating. Seeded FIRST so
    # that document extraction, which is tier 1, wins wherever the two
    # overlap: the founder's own financial model outranks a figure researched
    # off a press page, however confident the research was.
    from fundos.assessment.profile_bridge import seed_from_profile
    bridged = seed_from_profile(assessment)
    logger.info("ASSESSMENT %s: profile bridge %s", assessment.id, bridged)

    if not bridged.get("available") or bridged.get("available", 0) <= 6:
        bridged = _recover_profile_inputs(assessment, user=user) or bridged

    from fundos.assessment.extraction import extract_for_assessment
    extraction = extract_for_assessment(assessment)
    logger.info("ASSESSMENT %s: extraction %s", assessment.id, extraction)

    run_scoring(assessment, user=user)
    # Stage 2 is deterministic. The review that used to run here asked a
    # model to re-band finished scores at high confidence — the one operation
    # that breaks the audit chain invisibly, since the number moves while the
    # trail still points at the rubric that no longer produced it.
    from fundos.assessment.review import review as deterministic_review
    _append_findings(assessment, deterministic_review(assessment))

    assessment.refresh_from_db()
    return assessment


# ---------------------------------------------------------------------------
# The AI rubric review pass (D-08)
# ---------------------------------------------------------------------------

REVIEW_SYSTEM = (
    "You are reviewing an automated deal assessment. For each parameter you "
    "are given: the extracted fact, the published rubric thresholds for this "
    "company's funding stage, and the band the system assigned.\n\n"
    "Your job is to spot cases where the published threshold does not fit this "
    "particular company — for example a revenue threshold applied to a company "
    "whose model makes that figure meaningless, or a stage that looks wrong "
    "for the evidence.\n\n"
    "You may propose moving a band by ONE step, and only when you are highly "
    "confident. If you are not sure, flag it with your reasoning and leave the "
    "band alone. A flag is useful; a confident wrong adjustment is not.\n\n"
    'Return JSON only: {"stageAssessment":{"agrees":true|false,'
    '"suggested":"","reasoning":""},"findings":[{"inputKey":"",'
    '"action":"flag"|"reband","proposedBand":"poor|fair|good|excellent",'
    '"confidence":0.0-1.0,"reasoning":"","ask":""}]}'
)


def run_rubric_review(assessment):
    """Deprecated. Stage 2 no longer calls a model.

    Kept so existing callers and tasks do not break, and routed to the
    deterministic checks. Scoring is arithmetic against published thresholds:
    a model here added variance, not judgement, and could silently re-band a
    score whose audit trail still cited the rubric.
    """
    from fundos.assessment.review import review as deterministic_review

    findings = deterministic_review(assessment)
    _append_findings(assessment, findings)
    return findings


def _append_findings(assessment, findings):
    if not findings:
        return
    existing = list(assessment.audit_findings or [])
    assessment.audit_findings = existing + findings
    assessment.save(update_fields=["audit_findings"])


def _coerce(raw):
    import json
    if isinstance(raw, dict):
        if isinstance(raw.get("content"), dict):
            return raw["content"]
        if isinstance(raw.get("content"), str):
            raw = raw["content"]
        else:
            return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip().strip("`")
    if s.lower().startswith("json"):
        s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None
