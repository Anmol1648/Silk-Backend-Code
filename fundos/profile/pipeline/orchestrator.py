"""Runs the full pipeline for one profile and keeps the run record current.

Execution model
---------------
The two sources run concurrently — Source 1 is network-bound (search-grounded
LLM calls), Source 2 is CPU-bound (document parsing and OCR) — so overlapping
them is close to free: neither contends for the other's bottleneck, and a run
takes about as long as the slower of the two rather than their sum. Both write
their markdown through one shared
:class:`~fundos.profile.pipeline.dossier.DossierWriter`, which serialises the
writes and keeps section order fixed. Once they finish, the dossier goes to the
synthesis call.

Outputs
-------
Each run produces two deliverables:

* the **consolidated dossier** — the merged two-source markdown (the evidence)
* the **17-section profile** — written into ProfileSection and the record
  tables (the conclusion)

plus a :class:`~fundos.profile.models.ProfileGenerationRun` row carrying stage
timings, the activity log, per-document extraction detail and the run's cost.

Failure policy
--------------
:func:`run_pipeline` never raises. Any failure is recorded on the run with the
stage it happened at, and **every artifact produced before the failure is
kept**. That is the deliberate trade: a partially-complete run with a readable
dossier is far more useful than a clean rollback, and the sources are
unchanged either way so a retry costs nothing but time.

The one exception is the grounding gate, which is a refusal rather than a
failure — see :func:`_check_grounding`.
"""
import logging

from django.utils import timezone

from fundos.profile import schema
from fundos.profile.pipeline import (dossier as dossier_mod, settings as
                                     pipeline_settings, source1_research,
                                     source2_documents, synthesize, tracker,
                                     writer as profile_writer)
from fundos.profile.pipeline.concurrency import run_concurrently

logger = logging.getLogger("fundos.profile")


class UngroundedGeneration(Exception):
    """Nothing was retrieved, so nothing may be written.

    Deliberately NOT a subclass of anything the generic handlers treat as a
    partial failure — the whole point is that this must not be read as a gap
    for some other code path to fill in. The 13 Aug Deepak Nitrite run
    retrieved 2 characters, searched not at all, and still wrote twelve
    sections from 122 invented fields; those rows were indistinguishable in the
    database from a profile built on real evidence. Refusing to score a run
    while publishing its fabricated dossier is backwards, so this gate guards
    the profile as well as the scorecard.
    """

    def __init__(self, reason, *, mode="none", searches=0,
                 populated_fields=0):
        self.reason = reason
        self.mode = mode
        self.searches = searches
        # Always 0 from this pipeline: the gate runs BEFORE synthesis, so
        # nothing has been generated to discard. It was the old design's
        # headline number ("122 fields returned, nothing retrieved") because
        # that gate fired after the model had already answered. Kept on the
        # class so the reporting helpers that read it still work, and as a
        # reminder of what the reordering bought — refusing before you pay.
        self.populated_fields = populated_fields
        super().__init__(reason)


def _check_grounding(research, documents, *, mocked=False):
    """Decide whether this run retrieved enough to be allowed to publish.

    Reuses :func:`fundos.profile.assessment_extraction.grounding` rather than
    restating the policy, so the profile and the scorecard can never disagree
    about what "grounded" means — the two drifting apart is the specific defect
    that let a refused-to-score run publish a dossier anyway.

    The pipeline's inputs map onto that function's payload shape directly, and
    with better evidence than it used to get: ``searched`` is now a real count
    of searches the provider performed, not an inference.

    :returns: ``(ok, reason, mode)``.
    """
    from fundos.profile.assessment_extraction import grounding

    searches = int((research or {}).get("searches") or 0)
    doc_rows = [d for d in (documents or {}).get("documents") or []
                if d.get("status") == "Processed"]
    payloads = {
        "documents": doc_rows,
        # The research markdown is the retrieved corpus; its size is what the
        # gate weighs when no search was recorded.
        "research": {"web": "x" * int((research or {}).get("text_chars") or 0)}
        if research.get("text_chars") else {},
        "website": {},
    }
    ok, reason, mode = grounding(payloads, searched=bool(searches))

    if not ok and mocked:
        # Mock output is fabricated by construction, so the gate fires on every
        # mocked run and aborts it. The gate exists to stop a FOUNDER being
        # shown invented facts; in mocked mode there is no founder and nothing
        # real to protect, and the only effect would be that development and CI
        # could never exercise the pipeline. Deliberately narrow: the exemption
        # keys off the same flag the adapter uses to decide not to call a
        # provider, so a run that reaches a real model is never exempt.
        return True, "AI is mocked — grounding is not assessable", "mocked"
    return ok, reason, mode


def _search_is_configured():
    """Can the research batches search at all? ``None`` when undeterminable.

    A pre-flight, not a gate. Web search switched off on the research config
    profile does not make a run impossible — uploaded documents can still
    ground it — but it does mean ten batches will each go out, cost money, and
    come back with nothing retrieved. The grounding gate then refuses the run,
    correctly, and says "nothing was retrieved", which is true and unhelpful:
    the cause is one checkbox and nothing in the log points at it.

    So this runs before the batches and puts the cause in the log. It never
    stops a run: `None` (undeterminable) and `True` are treated alike by the
    caller, because refusing to start on a failed lookup would be worse than
    paying for one run.
    """
    from fundos.llm.adapter import _resolve_config_profile

    try:
        row = _resolve_config_profile(
            pipeline_settings.research_config_profile())
        if row is None:
            return None
        return bool(getattr(row, "web_search", False))
    except Exception as exc:
        logger.debug("PIPELINE: could not resolve search capability: %s", exc)
        return None


def _profile_context(profile):
    """The identity facts every stage needs, read once."""
    company = profile.company
    return {
        "company_name": company.name,
        "website": profile.website_url or getattr(company, "domain", "") or "",
        "tenant_id": company.tenant_id,
    }


def _founder_inputs(profile):
    """Founder records for the dossier header, as supplied. Never raises."""
    try:
        from fundos.profile.models import Founder
        return [{"name": f.name, "designation": f.designation,
                 "linkedinUrl": f.linkedin_url}
                for f in Founder.objects.filter(profile=profile)]
    except Exception:
        logger.debug("could not read founders for the dossier header",
                     exc_info=True)
        return []


def run_pipeline(profile, run, *, user=None, trace=None):
    """Execute the pipeline for one profile. Never raises.

    :param profile: The CompanyProfile to generate.
    :param run: A ProfileGenerationRun row, already created by the caller so a
        run that dies before its first write is still visible.
    :param user: Who triggered it, for provenance on the written rows.
    :param trace: The active GenerationTrace, so pipeline events land in the
        diagnostic log alongside the rest of the run.
    :returns: A result dict for the API — sections written, source failures,
        completeness, and the dossier's location.
    """
    from fundos.config.feature_flags import ai_mocked
    from fundos.llm.adapter import llm_call_count, llm_calls_since

    facts = _profile_context(profile)
    tracker_ = tracker.RunTracker(run, trace=trace)
    calls_at_start = llm_call_count()
    stage = tracker.QUEUED

    writer = dossier_mod.DossierWriter(
        run, facts["company_name"], facts["website"],
        _founder_inputs(profile))

    try:
        started = timezone.now()
        tracker_.update(started_at=started, stage=tracker.RESEARCHING)
        tracker_.event(
            f"pipeline started for {facts['company_name']}. Sources 1-2 run "
            f"concurrently.")

        # --- Sources 1-2, concurrently -----------------------------------
        # Both stages are marked started up front: they genuinely run at the
        # same time, so a stage rail that lit them sequentially would be
        # lying about what the run is doing.
        for source_stage in (tracker.RESEARCHING, tracker.PROCESSING_DOCUMENTS):
            tracker_.stage_started(source_stage)

        # Say so BEFORE spending the batches, not after the gate refuses them.
        if _search_is_configured() is False:
            tracker_.event(
                "web search is OFF on the '"
                f"{pipeline_settings.research_config_profile()}' config "
                "profile — the research batches cannot retrieve anything, and "
                "this run will only be publishable if the uploaded documents "
                "ground it. Admin -> LLM -> Config profiles.")

        results = run_concurrently(
            [
                lambda: source1_research.run(
                    run, writer, company_name=facts["company_name"],
                    website=facts["website"], tracker_=tracker_, user=user),
                lambda: source2_documents.run(profile, writer,
                                              tracker_=tracker_),
            ],
            # Two sources, so two workers — clamped by what the database can
            # take, which is 1 on SQLite. The research batches fan out again
            # inside Source 1 under their own configured concurrency.
            max_workers=min(2, pipeline_settings.max_parallelism()),
            thread_name_prefix="profile-source")

        research, documents = _collect_sources(results, tracker_)
        tracker_.update(progress={"researching": research,
                                  "processing_documents": documents})

        # A source failing outright is recorded, but does not by itself stop
        # the run: one source is enough to build a profile from, and which one
        # survived is exactly what the dossier shows.
        #
        # Same {source, error} shape `collect_sources` uses, so the run row and
        # the API response carry one failure format rather than two.
        failures = [{"source": name, "error": summary["error"]}
                    for name, summary in (("research", research),
                                          ("documents", documents))
                    if summary.get("error")]
        if research.get("error") and documents.get("error"):
            raise RuntimeError(
                "Both sources failed. Research: "
                f"{research['error']} | Documents: {documents['error']}")

        # --- Grounding gate ------------------------------------------------
        grounded, why, mode = _check_grounding(research, documents,
                                               mocked=ai_mocked())
        if not grounded:
            raise UngroundedGeneration(
                why, mode=mode,
                searches=int(research.get("searches") or 0))
        if mode == "website_only":
            tracker_.event(f"grounding is degraded: {why}")

        # --- Consolidate ----------------------------------------------------
        stage = tracker.CONSOLIDATING
        tracker_.stage_started(stage, "merging the two sources into one dossier")
        consolidation = writer.finalize()
        tracker_.update(dossier_uri=consolidation["storage_uri"],
                        dossier_chars=consolidation["characters"])
        tracker_.stage_finished(
            stage,
            note=f"dossier written ({consolidation['characters']:,} characters "
                 f"from {len(consolidation['sections_present'])} source(s))")

        # --- Synthesize -----------------------------------------------------
        stage = tracker.SYNTHESIZING
        tracker_.stage_started(
            stage,
            "sending the dossier for the structured profile — this is a "
            "single large call and typically the longest uninterrupted wait")
        generated, synthesis = synthesize.run(
            run, company_name=facts["company_name"],
            website=facts["website"], dossier=writer.text,
            documents=documents.get("documents") or [], user=user)
        tracker_.stage_finished(
            stage,
            note=f"synthesis complete: {synthesis['sections_populated']} of "
                 f"{synthesis['sections_total']} sections populated")
        _report_corrections(tracker_, synthesis)
        # Merged, not replaced: `progress` already carries both sources'
        # summaries and overwriting it would trade one stage's record for
        # another's rather than keeping both.
        tracker_.update(progress={**(run.progress or {}),
                                  "synthesizing": synthesis})

        # --- Write ----------------------------------------------------------
        stage = tracker.WRITING
        tracker_.stage_started(stage)
        written = profile_writer.write_profile(
            profile, generated, user=user,
            model_version=pipeline_settings.synthesis_config_profile())
        tracker_.stage_finished(
            stage,
            note=f"{len(written['written'])} section(s) saved, "
                 f"{len(written['empty'])} left empty, "
                 f"{len(written['failed'])} could not be stored")

        _check_benchmark_match(profile, tracker_)
        completeness = _finalise(profile)
        tracker_.stage_started(tracker.COMPLETED)
        tracker_.update(
            status="succeeded",
            finished_at=timezone.now(),
            sections_generated=written["written"],
            sections_skipped=written["failed"],
            sections_needing_input=written["empty"],
            completeness_pct=completeness,
            # Appended, not replaced: the row already carries whatever failed
            # while the source bundle was collected, and overwriting it would
            # trade one set of failures for another rather than reporting both.
            source_failures=list(run.source_failures or []) + failures,
            llm_calls=llm_calls_since(calls_at_start))
        tracker_.event(
            f"pipeline finished: {len(written['written'])} of "
            f"{synthesis['sections_total']} sections populated from a "
            f"{consolidation['characters']:,}-character dossier")

        return {
            "generated": written["written"],
            "skipped": written["failed"],
            "needsInput": written["empty"],
            "sourceFailures": failures,
            "completenessPct": completeness,
            "mode": "pipeline",
            "dossier": {"storageUri": consolidation["storage_uri"],
                        "characters": consolidation["characters"]},
            # The dossier TEXT, not just its location.
            #
            # The assessment extraction that runs after this pipeline was
            # reading uploaded documents from MaterialAsset.summary — a
            # separate, LLM-written condensation that is empty whenever the
            # critique role fails, and thin even when it does not. So a deck
            # this pipeline had just read 22,882 characters out of reached
            # the Deal Scorecard as nothing, and the scorecard scored ~10 of
            # 61 parameters on a company whose documents were sitting right
            # here, already parsed.
            #
            # Passed in memory rather than re-read from storage: it is the
            # exact text synthesis was given, so the profile and the
            # scorecard are derived from ONE body of evidence instead of two
            # that can disagree.
            "dossierText": writer.text,
            "research": research,
            "documents": documents,
            "llmCalls": llm_calls_since(calls_at_start),
            "runId": str(run.id),
        }

    except UngroundedGeneration as exc:
        # A refusal, not a crash. Recorded distinctly so an operator can tell
        # "we declined to publish" from "it broke", because the remedies are
        # opposite: fix the source, then regenerate with force.
        logger.error("PIPELINE: refusing to publish an ungrounded profile for "
                     "%s — %s", profile.company_id, exc.reason)
        tracker_.event(f"REFUSED: {exc.reason}. Nothing was written. Fix the "
                       f"source that failed above, then regenerate with "
                       f"force=true.")
        _record_failure(tracker_, stage, str(exc.reason),
                        status="refused", llm_calls=llm_calls_since(calls_at_start))
        _reset_status(profile)
        return {
            "generated": [], "skipped": [], "needsInput": [],
            "sourceFailures": [{"source": "grounding", "error": exc.reason}],
            "completenessPct": float(profile.completeness_pct or 0),
            "mode": "refused", "refusedReason": exc.reason,
            "groundingMode": exc.mode, "runId": str(run.id),
        }

    except Exception as exc:
        logger.exception("PIPELINE: run failed during %s for %s", stage,
                         profile.company_id)
        # Keep whatever evidence exists. A dossier that explains where the run
        # stopped is the most useful thing a failed run can leave behind.
        try:
            salvage = writer.finalize()
            tracker_.update(dossier_uri=salvage["storage_uri"],
                            dossier_chars=salvage["characters"])
        except Exception:
            logger.debug("could not persist the partial dossier",
                         exc_info=True)
        _record_failure(tracker_, stage, f"{type(exc).__name__}: {exc}",
                        llm_calls=llm_calls_since(calls_at_start))
        _reset_status(profile)
        return {
            "generated": [], "skipped": [], "needsInput": [],
            "sourceFailures": [{"source": stage, "error": str(exc)}],
            "completenessPct": float(profile.completeness_pct or 0),
            "mode": "failed", "error": str(exc), "runId": str(run.id),
        }


def _check_benchmark_match(profile, tracker_):
    """Say so when the generated sub-sector matches no benchmark group.

    ``resolve_sub_sector`` returning ``(None, "unresolved")`` is the correct
    outcome — assigning a company to the wrong peer group scores it against
    strangers, and its own docstring argues persuasively that blank beats
    wrong. But "correct" is not the same as "fine": category F of the
    assessment is then dropped from the denominator and its weight
    redistributed, and **nothing anywhere told anyone**. On a live profile the
    cause was a single field — the model answered a scalar taxonomy field with
    five comma-separated terms, and every lookup stage including fuzzy missed
    it.

    So the failed match becomes a visible gap on the section that owns the
    field: the founder is asked to pick a sub-sector, which is a thirty-second
    fix, instead of the scorecard quietly losing a fifth of its inputs.

    Never raises. A profile is a valid deliverable whether or not the
    assessment tables are even installed.
    """
    from fundos.profile.models import ProfileSection

    try:
        from fundos.profile.assessment_extraction import resolve_sub_sector
    except Exception:
        return

    try:
        storage_key = schema.storage_key_for("company_profile")
        section = ProfileSection.objects.filter(
            profile=profile, section_key=storage_key, is_active=True).first()
        if section is None:
            return
        label = str((section.structured or {}).get("sub_sector") or "").strip()
        if not label:
            return

        group, method = resolve_sub_sector(label)
        if group:
            tracker_.event(
                f"sub-sector '{label}' matched the benchmark group "
                f"'{group}' ({method})")
            return

        note = (
            f"'{label}' does not match any sector benchmark group, so this "
            f"company is scored without peer comparables (assessment category "
            f"F is excluded and its weight redistributed). Set a sub-sector "
            f"that matches the benchmark list to restore it.")
        tracker_.event(f"WARNING: {note}")
        notes = [n for n in (section.missing_notes or [])
                 if "benchmark group" not in str(n)]
        notes.append(note)
        section.missing_notes = notes[-20:]
        section.save(update_fields=["missing_notes", "updated_at"])
    except Exception:
        logger.debug("could not check the sub-sector benchmark match",
                     exc_info=True)


NARRATED_CORRECTIONS = 12
"""How many normalization corrections get their own activity line.

Enough to see the pattern without burying the stage rail — the run's
``progress`` carries the full list, and the count is always stated."""


def _report_corrections(tracker_, synthesis):
    """Narrate what the boundary had to fix in the model's response.

    These are the cheapest quality signal the pipeline produces and the one it
    used to throw away. Every entry is a place where what the model returned
    and what the contract requires disagreed — a list in a scalar field, a
    citation pointing at an expiring redirect, markdown in a string bound for a
    PDF. Each was corrected, so the stored profile looks right and says
    nothing; without this the only way to notice a prompt had drifted was for
    someone to read a finished document and spot it.

    Best-effort, like everything else on the tracker: losing a progress note
    must never fail a run that is otherwise fine.
    """
    try:
        corrections = list(synthesis.get("corrections") or [])
        total = int(synthesis.get("corrections_total") or 0)
        if not total:
            return
        tracker_.event(
            f"normalization corrected {total} field(s) in the synthesis "
            f"response — each was coerced to the shape its field spec "
            f"declares rather than rejected; a recurring one is a prompt or "
            f"field-spec problem, not a model problem",
            stage=tracker.SYNTHESIZING)
        for line in corrections[:NARRATED_CORRECTIONS]:
            tracker_.event(f"  · {line}", stage=tracker.SYNTHESIZING)
        if total > NARRATED_CORRECTIONS:
            tracker_.event(
                f"  · … and {total - NARRATED_CORRECTIONS} more; the full "
                f"list is on the run's progress record",
                stage=tracker.SYNTHESIZING)
    except Exception:
        logger.debug("could not narrate normalization corrections",
                     exc_info=True)


def _collect_sources(results, tracker_):
    """Split the two source results into ``(research, documents)``.

    ``run_concurrently`` returns an exception in place rather than raising, so
    that one source blowing up does not discard the other's completed work.
    Each is converted into a summary carrying its own ``error`` key, which is
    what lets the caller attribute a failure to the source that caused it
    instead of to whichever ran last.
    """
    stages = (tracker.RESEARCHING, tracker.PROCESSING_DOCUMENTS)
    summaries = []
    for stage, result in zip(stages, results):
        if isinstance(result, BaseException):
            tracker_.stage_finished(stage, ok=False)
            summaries.append({"error": f"{type(result).__name__}: {result}"})
        else:
            tracker_.stage_finished(stage)
            summaries.append(result)
    return summaries[0], summaries[1]


def _finalise(profile):
    """Mark the profile generated and recompute completeness."""
    from fundos.profile.services import (recompute_completeness,
                                         recompute_structured_sections)

    profile.status = "generated"
    profile.last_generated_at = timezone.now()
    profile.save(update_fields=["status", "last_generated_at", "updated_at"])
    recompute_structured_sections(profile)
    recompute_completeness(profile)
    profile.refresh_from_db(fields=["completeness_pct"])
    return float(profile.completeness_pct or 0)


def _reset_status(profile):
    """Return a failed/refused profile to a state the founder can retry from.

    Left at "generating", it would look permanently in-flight and the
    duplicate-run lock would keep declining new attempts.
    """
    try:
        profile.status = "draft" if not profile.last_generated_at \
            else "generated"
        profile.save(update_fields=["status", "updated_at"])
    except Exception:
        logger.debug("could not reset profile status", exc_info=True)


def _record_failure(tracker_, stage, message, *, status="failed", llm_calls=0):
    """Persist the failure against the stage that was active. Never raises."""
    try:
        tracker_.stage_finished(stage, ok=False)
    except Exception:
        pass
    tracker_.update(status=status, error=message[:2000],
                    finished_at=timezone.now(), llm_calls=llm_calls)
