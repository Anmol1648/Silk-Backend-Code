"""Stage 2 Phase 1 — the Deal Scorecard screen's two endpoints.

THE PATH PARAMETER IS `company_id`, AND IT IS A REAL COMPANY UUID
------------------------------------------------------------------
There is no job id in this system. The reference prototype these endpoints
reproduce addressed a company by the id of the research JOB that built its
dossier, because that is what its filesystem was keyed by; this backend keys
the same thing by the company itself, and `Assessment` is unambiguous about it:

    company = ForeignKey("core.Company", ..., related_name="assessments")
        required — every assessment has one
    deal    = ForeignKey("core.Deal", null=True, on_delete=SET_NULL, ...)
        optional — an assessment outlives the deal it was raised under
    Meta.indexes = [Index(fields=["company", "-created_at"]), ...]

So the scorecard is a statement about the COMPANY, and `company_id` is the
production identifier for it. No `job_id` alias is offered: inventing one would
mean carrying a second name for the same thing, which is how two ids for one
row start disagreeing.

The rest of the assessment API hangs off `deals/{deal_id}`; these two hang off
`companies/{company_id}`, matching the profile routes (C1) — a company may have
no deal, or several concurrent ones (an equity raise and a debt raise at the
same time), and scoping the screen to a deal would have meant choosing one of a
company's raises to be the real one.

Access is membership-based exactly as elsewhere, via the profile app's
`_company_or_404`, which returns 404 rather than 403 so an endpoint never
confirms the existence of a company the caller cannot see.

NEITHER ENDPOINT COMPUTES A SCORE
---------------------------------
The GET reads what `run_scoring` persisted and reshapes it (`phase1`). The POST
writes one number and calls `services.apply_parameter_override`, which is the
same cascade behind the parameter drawer's override: the leaf is re-banded,
every parent above it re-weighted around it, and the overall score and rating
rebuilt by the engine. There is no arithmetic in this file.
"""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fundos.assessment import phase1
from fundos.assessment.models import Assessment, ParameterValue

logger = logging.getLogger(__name__)

#: The scale a reviewer may type on, matching the deal-scoped override route.
#: The automated path stops below this; see `beyondAutomatedCeiling`.
MAX_SCORE = 10.0


def _company(request, company_id):
    from fundos.profile.views import _company_or_404
    return _company_or_404(request, company_id)


def _active_assessment(company):
    """The current assessment for a company.

    'scored' and 'final' both count; 'superseded' never does. The engine never
    overwrites a prior assessment, so the newest non-superseded row is the one
    on screen — the same rule `views._active` applies on the deal-scoped side,
    asked of the company instead.
    """
    return (Assessment.objects.filter(company=company)
            .exclude(status="superseded")
            .order_by("-created_at").first())


def _no_assessment():
    return Response(
        {"error": "E-NOTFOUND-404",
         "detail": ("This company has no assessment yet. Generate one from "
                    "the deal's assessment endpoint before opening Phase 1.")},
        status=status.HTTP_404_NOT_FOUND)


#: The lifecycle the client renders against. One vocabulary, and the only
#: thing a client should branch on — the HTTP status says whether to retry,
#: this says what to show.
#:
#:   GENERATING   a run is in flight; poll
#:   READY        scored, with evidence; render the scorecard
#:   NO_EVIDENCE  the run finished and established nothing; render the reason
#:   FAILED       the run errored; render the reason
#:
#: NOT_STARTED is never returned: a GET that finds no assessment starts one and
#: answers GENERATING, because the client must not have to ask for a pipeline
#: it should not need to know about.
GENERATING, READY, NO_EVIDENCE, FAILED = (
    "generating", "ready", "no_evidence", "failed")

#: Job states that mean a run is already in flight for this deal. A GET that
#: sees one of these must NOT start another.
IN_FLIGHT = ("queued", "running")

#: The contract's keys, with the value each carries when there is nothing to
#: report yet. Present and empty, never absent: a client that destructures the
#: response must not have to branch on whether scoring has finished.
EMPTY_CONTRACT = {
    "company": "", "sector": "", "sub_sector": "", "deal_stage": "",
    "ask_amount": "", "capital_raised": "", "assessment_date": None,
    "overall_score": None, "deal_rating": None, "input_coverage": None,
    "strongest_category": "", "weakest_category": "",
    "executiveSummaryData": {"narrative": "", "tags": []},
    "dealScorecardData": {},
    "diligenceFindingsData": [],
    "topFindingsData": [],
    "bandRecommendationsData": [],
    "structuralFactorsData": [],
    "finalRecommendationData": {},
}


class FundraisingPhase1View(APIView):
    """GET everything the Deal Scorecard screen renders, in one call.

    One response rather than four. The screen needs all of it to render any of
    it honestly — a score without its coverage, or a parameter without the rule
    that banded it, is the decontextualised number this whole subsystem exists
    to prevent — and the findings and recommendations are derived from the same
    scored rows the tree is built from, so splitting them would mean walking
    the model three times for one page load.

    THE CLIENT NEVER GENERATES AN ASSESSMENT ITSELF
    ------------------------------------------------
    Opening this screen is not a request to run a pipeline, so asking the
    client to notice a 404 and then POST to a deal-scoped endpoint it should
    not need to know about would make the front end responsible for a backend
    sequencing detail. When no assessment exists, this route starts the
    EXISTING one — `jobs.create_job` plus `generate_assessment_task`, the same
    pair `queue_generation` uses everywhere else — and answers 202 with the
    poll handle the rest of the API already speaks.

    It answers 202 with the contract's keys present and empty. It does NOT
    answer 200 with zeroes: a scorecard that renders as though it had been
    computed, when nothing has been, is the one failure mode this endpoint
    must never have. When generation has failed, the failure is reported as a
    failure, with the job's own error attached.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, company_id):
        company = _company(request, company_id)
        assessment = _active_assessment(company)
        if assessment is None:
            return self._start_generation(request, company)

        if not assessment.parameter_values.exists():
            # An assessment row with nothing under it is ambiguous on its own:
            # a run may still be filling it, may have died, or may have
            # finished having established nothing. The job row is what tells
            # the three apart, and the client needs them apart — one says
            # keep polling, one says read the error, one says fix the inputs.
            job = self._latest_job(assessment)
            if job is not None and job.status in IN_FLIGHT:
                return self._queued(company, job, assessment)
            if job is not None and job.status == "failed":
                # A run that died on infrastructure rather than on the deal
                # is worth starting again. "database is locked" is the case
                # that matters: assessment generation runs inline in dev and
                # collides with a profile pipeline that is still writing, and
                # the collision is decided in the first fraction of a second.
                # Left terminal, the screen showed a permanent failure for a
                # company whose evidence was fine, and nothing short of
                # deleting the company got it back.
                if self._retryable(job):
                    logger.info(
                        "PHASE1: last assessment run for deal %s failed on a "
                        "transient error (%s); starting another.",
                        assessment.deal_id, (job.error or "")[:80])
                    return self._start_generation(request, company)
                return self._generation_failed(company, job, job.error,
                                               assessment)
            return self._pending(company, assessment)

        return Response(self._payload(company, assessment))

    #: Failures worth another run, and DELIBERATELY ONLY THESE.
    #:
    #: Local write contention: free to retry, resolves by itself the moment
    #: the other writer commits, and costs nothing but milliseconds.
    #:
    #: Provider errors — 429, 503, timeouts — are excluded on purpose even
    #: though they are also transient. `llm.adapter` already retries them
    #: inside the call (three attempts across the key pool), so a job that
    #: failed with one has ALREADY exhausted them. Retrying here would
    #: duplicate that at a real cost per poll, and would replace the reason
    #: the user needs to see — "every key is rate-limited" — with a spinner
    #: that never resolves. Those belong on screen, not in a loop.
    TRANSIENT_ERRORS = ("database is locked", "database table is locked",
                        "deadlock detected")

    #: How many consecutive transient failures to absorb before showing the
    #: error. Without a ceiling a genuinely stuck environment would restart a
    #: run on every poll, which is the duplicate-run defect the job row exists
    #: to prevent, reintroduced through the failure path.
    MAX_TRANSIENT_RETRIES = 3

    @classmethod
    def _retryable(cls, job):
        """True when this failure is worth another run, and we have budget."""
        from fundos.core.models import GenerationJob

        error = (job.error or "").lower()
        if not any(marker in error for marker in cls.TRANSIENT_ERRORS):
            return False
        recent = list(GenerationJob.objects
                      .filter(deal_id=job.deal_id, kind="assessment")
                      .order_by("-created_at")[:cls.MAX_TRANSIENT_RETRIES])
        return not (len(recent) >= cls.MAX_TRANSIENT_RETRIES
                    and all(j.status == "failed" for j in recent))

    @staticmethod
    def _latest_job(assessment):
        """The most recent assessment run for this assessment's deal."""
        from fundos.core.models import GenerationJob

        if not assessment.deal_id:
            return None
        return (GenerationJob.objects
                .filter(deal_id=assessment.deal_id, kind="assessment")
                .order_by("-created_at").first())

    # -- the contract ----------------------------------------------------

    @staticmethod
    def _payload(company, assessment):
        """Adapt the scored assessment into the fixed Phase 1 contract.

        ONE `Reference` for the whole response. The four blocks read the same
        config tables, and building them independently made the cost of a page
        load scale with the size of the scoring model — see `phase1.Reference`.
        """
        from fundos.assessment import decision
        from fundos.assessment.v2_serializers import (
            _resolve_sectors_from_profile)

        ref = phase1.Reference(assessment)
        scorecard = phase1.deal_scorecard(assessment, ref)
        strongest, weakest = phase1.category_extremes(scorecard["categories"])
        terms = phase1.deal_terms(assessment)

        # The same three blocks V2 serves, from the same functions. This
        # screen used to carry the per-parameter findings only, so a deal
        # whose problem was a defaulted stage or an uncommented override
        # looked clean here and did not there.
        findings = decision.merge_findings(
            phase1.diligence_findings(assessment, ref)
            + decision.attribution_findings(assessment, ref),
            decision.audit_as_findings(assessment))
        top = decision.prioritised_findings(assessment, findings, ref)
        ranked = phase1.band_recommendations(assessment, ref)
        verdict = decision.final_recommendation(assessment, top, ref,
                                                all_findings=findings)

        # The sector is resolved the way V2 resolves it. Reading
        # `assessment.sector` alone reported blank for every company whose
        # sector was only ever recorded on the profile.
        sector = assessment.sector or ""
        sub_sector = assessment.sub_sector or ""
        if not sector or not sub_sector:
            p_sector, p_sub = _resolve_sectors_from_profile(company)
            sector = sector or p_sector
            sub_sector = sub_sector or p_sub

        return {
            **EMPTY_CONTRACT,
            "company": company.name,
            "sector": sector,
            "sub_sector": sub_sector,
            "deal_stage": assessment.deal_stage or "",
            **terms,
            "assessment_date": assessment.assessment_date,
            "overall_score": (float(assessment.overall_score)
                              if assessment.overall_score is not None else None),
            "deal_rating": assessment.rating_band or None,
            # The engine's own coverage figure, not a second count. See
            # `services.run_scoring` for what the denominator is.
            "input_coverage": float(assessment.input_coverage_pct or 0),
            "strongest_category": strongest,
            "weakest_category": weakest,
            "executiveSummaryData": phase1.executive_summary(assessment),
            "dealScorecardData": scorecard,
            "diligenceFindingsData": findings,
            "topFindingsData": top,
            "bandRecommendationsData": [r for r in ranked if r["controllable"]],
            "structuralFactorsData": [r for r in ranked
                                      if not r["controllable"]],
            "finalRecommendationData": verdict,
            "companyId": str(company.id),
            "assessmentId": str(assessment.id),
            "status": READY,
        }


    # -- generation ------------------------------------------------------

    @staticmethod
    def _deal_for(company, user):
        from fundos.profile.views import _default_deal
        return _default_deal(company, user)

    def _start_generation(self, request, company):
        """Start the existing assessment pipeline, at most once.

        IDEMPOTENT BY THE JOB ROW, WHICH IS THE EXISTING MECHANISM
        -----------------------------------------------------------
        The screen polls. Without a guard, every poll while a run is in flight
        would queue another run, and each run re-reads documents and re-calls
        the model — so a user watching a spinner would multiply the cost of
        their own wait. `GenerationJob` already records queued/running per
        (deal, kind) and is indexed for exactly this lookup, so the check is a
        read of state the system already keeps rather than a new lock.

        The task is dispatched with `.delay` and never waited on: extraction
        can take minutes, and this endpoint is a read/status endpoint. (Under
        the dev-only setting `CELERY_TASK_ALWAYS_EAGER` Celery runs the task
        inline, which is why the outcome is re-read below rather than assumed
        pending — that is the dev harness's behaviour, not this endpoint's
        contract.)
        """
        from fundos.assessment.tasks import generate_assessment_task
        from fundos.core.models import GenerationJob
        from fundos.core.services import jobs

        deal = self._deal_for(company, request.user)

        running = (GenerationJob.objects
                   .filter(deal_id=deal.id, kind="assessment",
                           status__in=IN_FLIGHT)
                   .order_by("-created_at").first())
        if running is not None:
            logger.debug("PHASE1: assessment already in flight for deal %s "
                         "(job %s) — not starting another.", deal.id,
                         running.id)
            return self._queued(company, running)

        job = jobs.create_job(deal, "assessment", request.user)
        try:
            generate_assessment_task.delay(
                str(deal.tenant_id), str(deal.id), str(request.user.id),
                job_id=str(job.id))
        except Exception as exc:      # eager mode surfaces failures here
            logger.error("PHASE1: assessment generation failed for company "
                         "%s: %s", company.id, exc, exc_info=True)
            job.refresh_from_db()
            return self._generation_failed(company, job, exc)

        job.refresh_from_db()
        assessment = _active_assessment(company)
        if assessment is not None and assessment.parameter_values.exists():
            return Response(self._payload(company, assessment))
        if job.status == "failed":
            return self._generation_failed(company, job, job.error)
        if assessment is not None:
            # The run finished and established nothing. Reporting that as
            # "still generating" would leave a client polling forever for a
            # scorecard that is never going to arrive.
            return self._pending(company, assessment)
        return self._queued(company, job)

    @staticmethod
    def _identity(company, assessment=None):
        """The contract keys that are knowable before a score exists."""
        out = {**EMPTY_CONTRACT, "company": company.name,
               "companyId": str(company.id)}
        if assessment is not None:
            out.update({
                "sector": assessment.sector or "",
                "sub_sector": assessment.sub_sector or "",
                "deal_stage": assessment.deal_stage or "",
                "assessment_date": assessment.assessment_date,
                "assessmentId": str(assessment.id),
            })
        return out

    @classmethod
    def _queued(cls, company, job, assessment=None):
        from fundos.core.services import jobs

        return Response({
            **cls._identity(company, assessment),
            "status": GENERATING,
            "job": jobs.serialise(job),
            "poll": f"/api/v1/deals/{job.deal_id}/jobs/{job.id}",
            "detail": ("The assessment for this company is being generated. "
                       "Poll the job, then request this screen again."),
        }, status=status.HTTP_202_ACCEPTED)

    @classmethod
    def _generation_failed(cls, company, job, error, assessment=None):
        return Response({
            **cls._identity(company, assessment),
            "status": FAILED,
            "error": "E-GENERATION-502",
            "detail": ("The assessment for this company could not be "
                       "generated, so there is no scorecard to show."),
            "reason": str(getattr(job, "error", None) or error or "")[:1000],
            "poll": f"/api/v1/deals/{job.deal_id}/jobs/{job.id}",
        }, status=status.HTTP_502_BAD_GATEWAY)

    @classmethod
    def _pending(cls, company, assessment):
        return Response({
            **cls._identity(company, assessment),
            "status": NO_EVIDENCE,
            "detail": ("An assessment exists for this company but no evidence "
                       "was established for any parameter, so nothing can be "
                       "scored. Check the generation run's diagnostics."),
        }, status=status.HTTP_202_ACCEPTED)


class FundraisingPhase1OverrideView(APIView):
    """POST a reviewer's score for one parameter; get the new total back.

    Addressed by REF CODE (`A.1.a`), because that is what the scorecard shows
    and what a reviewer is looking at when they disagree. A ref code is not
    guaranteed unique — the sector mirrors put two inputs on E.1 — so an
    ambiguous one is refused with both candidates named rather than resolved by
    picking whichever row the database returned first. `inputKey` addresses a
    row exactly and is accepted too.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, company_id):
        from fundos.assessment.services import (apply_parameter_override,
                                                 band_score)

        company = _company(request, company_id)
        assessment = _active_assessment(company)
        if assessment is None:
            return _no_assessment()

        ref = str(request.data.get("parameter_ref")
                  or request.data.get("inputKey") or "").strip()
        if not ref:
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": "parameter_ref is required.",
                 "fields": {"parameter_ref": "Name the parameter to override."}},
                status=status.HTTP_400_BAD_REQUEST)

        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            # Refused at the point of entry rather than recorded and flagged
            # afterwards. The integrity checks count uncommented overrides and
            # block sign-off on them; refusing here is the same rule enforced
            # earlier, and it costs the reviewer one sentence at the moment
            # they still remember their reasoning.
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": "A reason is required for every override.",
                 "fields": {"reason": "Explain why the system score is wrong."}},
                status=status.HTTP_400_BAD_REQUEST)

        raw = request.data.get("override_score")
        if raw is None:
            raw = request.data.get("score")
        if raw is None:
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": "override_score is required."},
                status=status.HTTP_400_BAD_REQUEST)
        try:
            score = float(raw)
        except (TypeError, ValueError):
            return Response({"error": "E-VALIDATION-400",
                             "detail": "override_score must be a number."},
                            status=status.HTTP_400_BAD_REQUEST)
        if not 0 <= score <= MAX_SCORE:
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": f"override_score must be between 0 and {MAX_SCORE:g}."},
                status=status.HTTP_400_BAD_REQUEST)

        matches = self._resolve(assessment, ref)
        if not matches:
            return Response(
                {"error": "E-NOTFOUND-404",
                 "detail": (f"No parameter {ref} on this scorecard. Overrides "
                            f"apply to assessed parameters, not to the "
                            f"category rows above them.")},
                status=status.HTTP_404_NOT_FOUND)
        if len(matches) > 1:
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": (f"{ref} identifies {len(matches)} parameters "
                            f"({', '.join(sorted(m.input_key for m in matches))})"
                            f". Send inputKey to name one exactly."),
                 "candidates": sorted(m.input_key for m in matches)},
                status=status.HTTP_400_BAD_REQUEST)

        pv = matches[0]
        before = (float(assessment.overall_score)
                  if assessment.overall_score is not None else None)

        assessment = apply_parameter_override(assessment, pv, score=score,
                                              reason=reason, user=request.user)
        pv.refresh_from_db()

        total = assessment.overall_score
        ceiling = band_score("Excellent",
                             getattr(assessment, "tenant_id", None))
        return Response({
            "success": True,
            "parameter_ref": ref,
            "inputKey": pv.input_key,
            "recalculated_total_score": (round(float(total), 2)
                                         if total is not None else None),
            "updated_band": assessment.rating_band or None,
            "previous_total_score": round(before, 2) if before is not None else None,
            "parameterBand": phase1.normalise_band(pv.band),
            # The rule's own verdict, kept beside the reviewer's so the size of
            # the disagreement stays readable rather than becoming a number
            # somebody changed.
            "systemBand": phase1.normalise_band(pv.system_band),
            "systemScore": (float(pv.system_score)
                            if pv.system_score is not None else None),
            # Excellent is the best band the rules can reach, and its worth is
            # a configured number — read it rather than restating it, so a
            # retuned scoring key moves this flag with it.
            "beyondAutomatedCeiling": bool(
                ceiling is not None and score > float(ceiling)),
            "coveragePct": float(assessment.input_coverage_pct or 0),
        })

    @staticmethod
    def _resolve(assessment, ref):
        """Find the assessed parameter a reviewer means by `ref`.

        Resolved through CONFIG, not through `ParameterValue.ref_code`. The
        scorecard prints the ref code the config declares; the column on the
        value row is a copy that only some write paths populate, so matching on
        it makes an override addressable or not depending on which source
        happened to answer the parameter. A screen that shows `A.1.d` and an
        override that cannot find `A.1.d` is the same row disagreeing with
        itself.

        `ref` may be a ref code or an input key; an input key is tried second
        because it is the exact address and a ref code is what the screen
        shows.
        """
        from fundos.assessment.phase1 import _param_config

        config = _param_config(getattr(assessment, "tenant_id", None))
        keys = [key for key, cfg in config.items() if cfg.ref_code == ref]
        if not keys and ref in config:
            keys = [ref]

        rows = list(ParameterValue.objects.filter(
            assessment=assessment, input_key__in=keys)) if keys else []
        if rows:
            return rows
        # Fall back to the stored copies, so a value written by a path that
        # set ref_code but has no config row is still reachable.
        return (list(ParameterValue.objects.filter(assessment=assessment,
                                                   ref_code=ref))
                or list(ParameterValue.objects.filter(assessment=assessment,
                                                      input_key=ref)))
