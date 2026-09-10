"""Source 1: web-search-grounded research.

Ten calls of ten questions each. Results are held in memory and pushed into the
dossier through the shared
:class:`~fundos.profile.pipeline.dossier.DossierWriter` after **every** batch
completes, so the persisted evidence always reflects the latest finished work
without this module writing any artifact of its own.

A failed batch is recorded as a markdown error stub rather than aborting the
source — partial research still produces a useful dossier. Only an
all-batches-failed result (bad key, no quota, open breaker) fails the run.

Grounding is the point
----------------------
This is the only stage that reaches the open web, so it is also the stage the
grounding gate measures. Every call reports how many searches the provider
actually performed; a batch that answered from the model's recollection is
recorded as ungrounded and says so in the dossier, because an uncited answer
that reads like a researched one is the single most expensive failure this
pipeline can produce.
"""
import logging
import threading

from fundos.profile.pipeline import questions as question_bank
from fundos.profile.pipeline import settings as pipeline_settings
from fundos.profile.pipeline import tracker
from fundos.profile.pipeline.concurrency import run_concurrently

logger = logging.getLogger("fundos.profile")

SOURCE_KEY = "source1"
SOURCE_TITLE = "Source 1: Web Research (search-grounded)"

ROLE = "profile_research_batch"


def _batch_markdown(batch, body, *, searches=0):
    """Wrap one batch's response in a consistent heading.

    Every batch — successful or failed, grounded or not — renders the same
    shape, so a reader skimming headings can tell them apart and the synthesis
    model sees a uniform structure. The grounding line is part of the evidence:
    it tells a later reader whether these answers were retrieved or recalled.
    """
    if searches:
        provenance = f"*Grounded: {searches} web search(es) performed.*"
    else:
        provenance = ("*⚠ Not grounded: the model performed no web search for "
                      "this batch, so these answers come from the supplied "
                      "context and its own recollection. Treat every figure "
                      "here as unverified.*")
    return (
        f"## Batch {batch.index}: {batch.topic}\n\n"
        f"*Feeds profile sections: {batch.covers}*\n\n"
        f"{provenance}\n\n"
        f"{body.strip()}\n"
    )


def _failure_markdown(batch, error):
    """Render a failed batch as an explicit error stub listing its questions.

    This is what makes partial failure legible in the dossier itself rather
    than only in the run record: a reader opening the dossier sees precisely
    which questions have no answer, instead of the batch's heading silently
    vanishing or — far worse — appearing with no data and no explanation, which
    a synthesis pass could mistake for "nothing to report" instead of "we could
    not find out".
    """
    listed = "\n".join(f"{i}. {q}"
                       for i, q in enumerate(batch.questions, 1))
    return (
        f"## Batch {batch.index}: {batch.topic}\n\n"
        f"*Feeds profile sections: {batch.covers}*\n\n"
        f"> **This batch failed and contains no research data.**\n"
        f"> Error: `{error}`\n\n"
        f"Questions that went unanswered:\n\n{listed}\n"
    )


def _render(company_name, website, batches, done, succeeded, searches):
    """Render the whole source section from whatever batches have finished.

    Called after *every* batch completion, so this runs up to ten times per run
    — always over the batches in their fixed configured order, not in whatever
    order they happened to finish, so the dossier is stable and re-readable
    regardless of network timing. An unfinished batch renders as "still
    running" rather than being omitted, so a mid-run read shows the full
    expected shape with gaps, not a shorter document that looks finished.
    """
    total = len(batches)
    header = (
        f"# {SOURCE_TITLE}\n\n"
        f"Company: **{company_name}**  \n"
        f"Website: {website or '_not recorded_'}\n\n"
        f"{succeeded} of {total} research batches returned data "
        f"({question_bank.total_questions(batches)} questions total; "
        f"{len(done)} of {total} batches finished; "
        f"{searches} web search(es) performed in total).\n"
    )
    parts = [header]
    for batch in batches:
        parts.append(done.get(batch.index)
                     or f"## Batch {batch.index}: {batch.topic}\n\n"
                        "_Still running._\n")
    return "\n\n".join(parts)


def _call_batch(batch, company_name, website, total_batches, user=None):
    """One research call. Returns ``(markdown_body, searches)``.

    Goes through :func:`fundos.llm.adapter.llm_generate` rather than a provider
    SDK, so this call gets the same endpoint resolution, key handling,
    fallback, budget enforcement, circuit breaker and ``LLMCallLog`` row as
    every other call in the system. ``response_kind="text"`` is what keeps the
    markdown intact: research output is prose with inline citations, and
    forcing it through the JSON parse/repair/validate path would destroy it.
    
    ENHANCED LOGGING (v2.1): Each batch logs as separate trace event with
    character count and search count for granular diagnostics.
    """
    from fundos.llm.adapter import llm_generate
    from fundos.profile.trace import current_trace, OK, FAIL
    import time

    numbered = "\n".join(f"{i}. {q}"
                         for i, q in enumerate(batch.questions, 1))
    
    trace = current_trace()
    batch_started = time.monotonic()
    
    try:
        result = llm_generate(
            role=ROLE,
            context={
                "company": {"name": company_name, "website": website},
                "company_name": company_name,
                "website": website,
            },
            section_context={
                "company_name": company_name,
                "website": website or "",
                "topic": batch.topic,
                "covers": batch.covers,
                "count": str(len(batch.questions)),
                "questions": numbered,
                "batch_index": str(batch.index),
                "batch_total": str(total_batches),
            },
            config_profile=pipeline_settings.research_config_profile(),
            response_kind="text",
            calling_context=f"profile.research.batch{batch.index}",
            user=user,
        )
        body = (result or {}).get("text") or ""
        searches = int((result or {}).get("searches") or 0)
        sources = (result or {}).get("grounding") or []
        if sources:
            lines = "\n".join(f"- {s}" for s in sources)
            body = f"{body}\n\n#### Grounding sources\n\n{lines}\n"
        if not body.strip():
            raise RuntimeError("the model returned no text for this batch")
        
        # Log successful batch
        elapsed_ms = int((time.monotonic() - batch_started) * 1000)
        if trace:
            trace.event(
                f"research.batch.{batch.index}",
                status=OK,
                ms=elapsed_ms,
                topic=batch.topic,
                chars=len(body),
                searches=searches,
                questions_count=len(batch.questions)
            )
        
        logger.info(
            "RESEARCH batch %d/%d (%s): OK in %dms, %d chars, %d search(es)",
            batch.index, total_batches, batch.topic, elapsed_ms, 
            len(body), searches
        )
        
        return body, searches
        
    except Exception as e:
        elapsed_ms = int((time.monotonic() - batch_started) * 1000)
        error_msg = str(e)[:150]
        
        # Log failed batch
        if trace:
            trace.event(
                f"research.batch.{batch.index}",
                status=FAIL,
                ms=elapsed_ms,
                topic=batch.topic,
                error=error_msg,
                error_type=type(e).__name__,
                questions_count=len(batch.questions)
            )
        
        logger.warning(
            "RESEARCH batch %d/%d (%s): FAIL in %dms. Error: %s",
            batch.index, total_batches, batch.topic, elapsed_ms, error_msg
        )
        
        raise


def run(run_row, writer, *, company_name, website, tracker_, user=None):
    """Execute every research batch, updating the dossier as each lands.

    Called once per run by the orchestrator, concurrently with Source 2. Up to
    :func:`~fundos.profile.pipeline.settings.research_concurrency` batches run
    at once; each batch is otherwise fully independent, so failures are
    isolated by :func:`_failure_markdown` rather than by any try/except around
    the whole source.

    :returns: A progress summary recorded onto the run row — totals, which
        batch indices failed, and the searches actually performed (the number
        the grounding gate reads).
    :raises RuntimeError: Only if every batch failed. A partial failure never
        raises.
    """
    batches = question_bank.build_batches(company_name, website)
    if not batches:
        # An administrator has deactivated the whole bank. That is an
        # instruction, not a fault: honour it, say so in the dossier, and let
        # the run proceed on documents alone.
        tracker_.event("source 1: the research question bank is empty — no "
                       "web research will be performed (every batch is "
                       "deactivated in admin).")
        writer.set_section(SOURCE_KEY,
                           f"# {SOURCE_TITLE}\n\n_No research batches are "
                           f"configured, so no web research was performed._\n")
        return {"batches_total": 0, "batches_finished": 0,
                "batches_succeeded": 0, "batches_failed": [],
                "questions_total": 0, "searches": 0, "skipped": True}

    concurrency = pipeline_settings.research_concurrency()
    total = len(batches)

    done = {}
    failures = {}
    searches_total = [0]
    # Guards `done`/`failures`/`searches_total` and orders the dossier
    # rewrites triggered from the concurrent batch tasks.
    progress_lock = threading.Lock()

    tracker_.event(
        f"source 1: {total} research batches "
        f"({question_bank.total_questions(batches)} questions), "
        f"{concurrency} at a time",
        stage=tracker.RESEARCHING)

    def run_batch(batch):
        """Run one batch's call, then fold its result into shared state.

        A batch never raises out of this function — any exception from the call
        is caught and turned into a recorded failure, so one bad batch can
        never take its siblings' completed work with it.
        """
        clock = tracker.Elapsed()
        try:
            body, searches = _call_batch(batch, company_name, website, total,
                                         user=user)
            markdown = _batch_markdown(batch, body, searches=searches)
            error = None
        except Exception as exc:
            logger.warning("PIPELINE: research batch %s (%s) failed: %s",
                           batch.index, batch.topic, exc)
            markdown = _failure_markdown(batch, exc)
            searches = 0
            error = str(exc)

        # The dossier write happens INSIDE this lock, not after it. Otherwise
        # two batches can each render a snapshot, then persist them in the
        # opposite order, leaving the stale one stored and silently dropping a
        # completed batch from the final dossier.
        with progress_lock:
            done[batch.index] = markdown
            searches_total[0] += searches
            if error is not None:
                failures[batch.index] = error
            succeeded = len(done) - len(failures)
            writer.set_section(SOURCE_KEY,
                               _render(company_name, website, batches, done,
                                       succeeded, searches_total[0]))
            tracker_.update(batches_total=total,
                            batches_succeeded=succeeded,
                            batches_failed=sorted(failures))
            tracker_.event(
                f"research batch {batch.index}/{total} '{batch.topic}' "
                f"{'failed' if error else 'ok'} after "
                f"{tracker.humanize_duration(clock.seconds)} "
                f"({len(done)}/{total} finished, {succeeded} with data, "
                f"{searches} search(es))",
                stage=tracker.RESEARCHING)

    # Push an initial section so the dossier is well-formed from the first
    # moment it is persisted, even before any batch lands.
    writer.set_section(SOURCE_KEY,
                       _render(company_name, website, batches, done, 0, 0))

    run_concurrently([lambda b=batch: run_batch(b) for batch in batches],
                     max_workers=concurrency,
                     thread_name_prefix="profile-research")

    succeeded = total - len(failures)
    if succeeded == 0:
        first = sorted(failures.items())[0][1] if failures else "unknown error"
        raise RuntimeError(
            f"All {total} research calls failed. First error: {first}")

    return {
        "batches_total": total,
        "batches_finished": len(done),
        "batches_succeeded": succeeded,
        "batches_failed": sorted(failures),
        "batch_errors": {str(k): v for k, v in sorted(failures.items())},
        "questions_total": question_bank.total_questions(batches),
        "searches": searches_total[0],
        # How much research text this source actually produced, counting only
        # batches that returned data. The grounding gate reads it: a provider
        # that returns no search metadata reports `searches=0`, and "unknown"
        # is not the same fact as "none" — a run that came back with nine
        # batches of cited prose has plainly retrieved something, and refusing
        # it on a missing counter would be the gate misfiring on its own blind
        # spot.
        "text_chars": sum(len(markdown) for index, markdown in done.items()
                          if index not in failures),
        "concurrency": concurrency,
    }
