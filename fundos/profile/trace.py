"""
Step-by-step diagnostic trace for Company Profile generation.

Why this exists
---------------
When a generated profile comes back empty there are roughly a dozen places the
pipeline can fail, and almost all of them fail QUIETLY: a research adapter
raises inside a try/except and is logged at WARNING; the AI is in mocked mode
and returns plausible-looking stub content; no LLM endpoint is bound; the model
returns valid JSON with every field blank; the fan-out writes to a section key
that no longer exists. Every one of those produces the same visible symptom —
"the profile has no data" — and none of them announces itself.

This module makes each step state, on one line, what it did and whether it
worked. Every line carries the same run id, so a single `grep` reconstructs a
whole generation:

    GEN[3f9a2c] step=sources.website        status=OK    ms=812  chars=48210
    GEN[3f9a2c] step=sources.research.market status=FAIL ms=3     error="..."
    GEN[3f9a2c] step=llm.call.profile_research_batch status=OK ms=41022 ...

It also DIAGNOSES. A log that says "failed" still needs an expert to read it;
`diagnose()` matches the recorded steps against known failure signatures and
emits a plain-language cause and remedy, so the log answers "what do I fix?"
rather than only "what broke?".

Design notes
------------
* Tracing must never break generation. Every public method swallows its own
  errors — a diagnostic that takes down the thing it is diagnosing is worse
  than no diagnostic.
* The trace is exported as JSON onto ProfileGenerationRun.diagnostics, so it
  survives the log rotation window and can be read back per company.
* `current_trace()` uses a context variable so deeply nested code (the LLM
  adapter) can attach detail without every call site passing a trace object.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import time
import uuid

logger = logging.getLogger("fundos.generation")

_current: contextvars.ContextVar = contextvars.ContextVar(
    "silk_generation_trace", default=None)

# Step outcomes.
OK, FAIL, SKIP, WARN = "OK", "FAIL", "SKIP", "WARN"

_MAX_ERR = 400


def current_trace():
    """The trace for the generation running on this task, or None."""
    try:
        return _current.get()
    except Exception:          # pragma: no cover - contextvar edge
        return None


def _fmt(value):
    """Render a value for a single-line log record."""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\n", " ").replace('"', "'")
    if len(text) > _MAX_ERR:
        text = text[:_MAX_ERR] + "…"
    return f'"{text}"'



# A run given less than this much source material has effectively been given
# nothing — the floor is deliberately low, since a real website fetch alone
# runs to thousands of characters.
UNGROUNDED_SOURCE_CHARS = 2000
# …and one that fills this many fields from that has recalled them.
UNGROUNDED_FIELD_COUNT = 20

# Roles whose job is to RETRIEVE. If none of them searched, the run has no
# external evidence beyond whatever the documents supplied, however complete
# the resulting profile looks.
RETRIEVAL_ROLES = ("profile_research_batch", "company_profile_deep_extract")


def _status_code(message):
    """Pull an HTTP status code out of an error message, or None.

    Deliberately anchored: a bare `"400" in err` also matches a model named
    gpt-400, a byte count, or a timestamp.
    """
    m = re.search(r"\b([45]\d\d)\b(?=\s|:|,|\.|$)", str(message))
    return int(m.group(1)) if m else None


class GenerationTrace:
    """Records one generation run, step by step."""

    def __init__(self, *, profile=None, company=None, user=None,
                 mode="", trigger=""):
        self.run_id = uuid.uuid4().hex[:6]
        self.profile = profile
        self.company = company
        self.user = user
        self.mode = mode
        self.trigger = trigger
        self.started = time.monotonic()
        self.steps: list[dict] = []
        self._token = None

    # -- lifecycle -----------------------------------------------------
    def __enter__(self):
        self._token = _current.set(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self.event("generation.abort", FAIL, error=repr(exc))
        self.log_diagnosis()
        try:
            if self._token is not None:
                _current.reset(self._token)
        except Exception:
            pass
        return False

    def log_diagnosis(self):
        """Write the verdict to the log. Always safe to call."""
        try:
            logger.info("GEN[%s] complete: steps=%d failures=%d ms=%d",
                        self.run_id, len(self.steps), len(self.failures()),
                        self.elapsed_ms())
            for i, f in enumerate(self.diagnose(), 1):
                # NONE is "no fault"; INFO is context on a healthy run. Neither
                # is something to act on, and logging them at WARNING put two
                # lines in front of every operator grepping for real problems
                # — which is how the real ones stop being read.
                if f["severity"] in ("NONE", "INFO"):
                    label = "OK" if f["severity"] == "NONE" else "INFO"
                    logger.info("GEN[%s] DIAGNOSIS %d [%s] %s",
                                self.run_id, i, label, f["cause"])
                else:
                    logger.warning(
                        "GEN[%s] DIAGNOSIS %d [%s] %s | EVIDENCE: %s | "
                        "FIX: %s", self.run_id, i, f["severity"], f["cause"],
                        f["evidence"], f["remedy"])
        except Exception:
            pass

    # -- recording -----------------------------------------------------
    def event(self, name, status=OK, **fields):
        """Record a completed step."""
        try:
            record = {"step": name, "status": status, **fields}
            self.steps.append(record)
            line = " ".join(f"{k}={_fmt(v)}" for k, v in record.items())
            log = (logger.error if status == FAIL else
                   logger.warning if status == WARN else logger.info)
            log("GEN[%s] %s", self.run_id, line)
        except Exception:
            pass
        return self

    @contextlib.contextmanager
    def step(self, name, **fields):
        """Time a step and record OK/FAIL automatically.

        Yields a dict the body can add measurements to (`out["chars"] = n`),
        which are merged into the record.
        """
        started = time.monotonic()
        out: dict = {}
        try:
            yield out
        except Exception as e:
            ms = int((time.monotonic() - started) * 1000)
            self.event(name, FAIL, ms=ms, error=f"{type(e).__name__}: {e}",
                       **{**fields, **out})
            raise
        else:
            ms = int((time.monotonic() - started) * 1000)
            status = out.pop("status", OK)
            self.event(name, status, ms=ms, **{**fields, **out})

    def skip(self, name, reason, **fields):
        return self.event(name, SKIP, reason=reason, **fields)

    def fail(self, name, error, **fields):
        return self.event(name, FAIL, error=str(error), **fields)

    # -- reading back --------------------------------------------------
    def find(self, prefix):
        return [s for s in self.steps if s["step"].startswith(prefix)]

    def first(self, name):
        for s in self.steps:
            if s["step"] == name:
                return s
        return None

    def failures(self):
        return [s for s in self.steps if s["status"] == FAIL]

    def elapsed_ms(self):
        return int((time.monotonic() - self.started) * 1000)

    # -- diagnosis -----------------------------------------------------
    def diagnose(self):
        """Match the recorded steps against known failure signatures.

        Returns a list of {severity, cause, evidence, remedy}. Ordered so the
        FIRST entry is the most likely root cause, because a reader acting on
        one thing should act on that one.
        """
        findings = []

        def add(severity, cause, evidence, remedy):
            findings.append({"severity": severity, "cause": cause,
                             "evidence": evidence, "remedy": remedy})

        preflight = self.first("preflight.config") or {}
        llm_calls = self.find("llm.call")
        sources = self.first("sources.summary") or {}
        written = self.first("sections.summary") or {}

        # --- 1. Mocked AI --------------------------------------------
        if preflight.get("ai_mocked") is True:
            add("CRITICAL",
                "The AI is in mocked (demonstration) mode, so no real model "
                "was called. Every section was filled from canned sample "
                "content or left blank.",
                "preflight.config reported ai_mocked=true",
                "Admin → Application Configuration → switch OFF 'AI mocked'. "
                "Then regenerate WITH FORCE, or the unchanged-sources check "
                "will return the previous result.")

        # --- 2. No usable LLM endpoint --------------------------------
        if preflight.get("llm_bindings") == 0:
            add("CRITICAL",
                "No AI endpoint is bound to any role, so generation could not "
                "call a model at all.",
                "preflight.config reported llm_bindings=0",
                "Admin → LLM Integration: create an endpoint, set its API key, "
                "and bind it to the profile roles.")
        elif preflight.get("llm_endpoints_active") == 0:
            add("CRITICAL",
                "AI endpoints exist but none is active, so no model could be "
                "reached.",
                "preflight.config reported llm_endpoints_active=0",
                "Admin → LLM Integration: mark the endpoint active.")

        # --- 3. LLM call failures -------------------------------------
        failed_calls = [c for c in llm_calls if c["status"] == FAIL]
        if failed_calls:
            err = str(failed_calls[0].get("error", ""))
            # Match on the STATUS CODE and on whole words, never on a bare
            # substring of the whole message. The message contains a URL, and
            # the Gemini URL ends in "generateContent" — which contains
            # "rate". Every Gemini failure of every kind therefore matched
            # the rate-limit branch and was reported as a billing problem,
            # sending the operator to the provider's console while the real
            # fault (a rejected request payload) went unexamined.
            low = err.lower()
            code = _status_code(err)
            words = set(re.findall(r"[a-z]+", low))

            if code in (401, 403) or "api key" in low or "unauthor" in words:
                add("CRITICAL",
                    "The AI provider rejected our credentials.",
                    f"llm call failed: {err[:200]}",
                    "Admin → LLM Integration: re-enter the API key for the "
                    "endpoint. If the key was shared or committed anywhere, "
                    "reissue it at the provider first.")
            elif code == 429 or "quota" in words or "billing" in words or \
                    "rate limit" in low or "ratelimit" in low:
                add("CRITICAL",
                    "The AI provider refused the request for rate-limit, "
                    "quota or billing reasons.",
                    f"llm call failed: {err[:300]}",
                    "Check the account's billing status and rate limits at the "
                    "provider, then retry.")
            elif code == 400:
                add("CRITICAL",
                    "The AI provider REJECTED THE REQUEST ITSELF — this is a "
                    "configuration or payload fault, not a billing one.",
                    f"llm call failed: {err[:300]}",
                    "The provider's own explanation is in the evidence above; "
                    "it names the offending field. The usual causes are a "
                    "capability combination the model does not allow (on "
                    "Gemini, web search cannot be combined with a response "
                    "schema — Admin → Config profiles: turn structured "
                    "output OFF on the searching profile), a model string "
                    "the endpoint does not serve, or an output ceiling "
                    "below the thinking budget.")
            elif code == 404 or ("model" in words and "not" in words):
                add("CRITICAL",
                    "The configured model name is not recognised by the "
                    "endpoint it was sent to — usually a model from one vendor "
                    "pointed at another vendor's API.",
                    f"llm call failed: {err[:200]}",
                    "Admin → LLM Integration: confirm the model string matches "
                    "the endpoint's provider.")
            elif "timeout" in low or "timed out" in low:
                add("HIGH",
                    "The AI call timed out. A search-grounded research batch "
                    "is a long request, and synthesis over a full dossier is "
                    "longer.",
                    f"llm call failed: {err[:200]}",
                    "Increase the endpoint's request timeout, or deactivate "
                    "research batches (Admin → Research batches) so each run "
                    "does less work.")
            elif "connection" in low or "resolve" in low or "network" in low:
                add("CRITICAL",
                    "The server could not reach the AI provider over the "
                    "network.",
                    f"llm call failed: {err[:200]}",
                    "Check outbound internet access and any firewall or proxy "
                    "rules on the application server.")
            else:
                add("CRITICAL",
                    "The AI call failed.",
                    f"llm call failed: {err[:200]}",
                    "Read the error above. If it is not self-explanatory, send "
                    "this trace to the development team.")

        # --- 4. Model answered but said nothing -----------------------
        empty_calls = [c for c in llm_calls
                       if c["status"] == OK and c.get("populated_fields") == 0]
        if empty_calls and not failed_calls:
            add("HIGH",
                "The AI responded successfully but every field came back "
                "empty. It was given nothing to work from, or it could not "
                "verify anything and correctly declined to invent content.",
                f"{len(empty_calls)} call(s) returned 0 populated fields",
                "Check the source steps above. If website/documents/research "
                "are all empty or failed, fix those first — the model cannot "
                "report facts it was never given.")

        # --- 4b. Answered from memory rather than from sources ----------
        # THE most important rule here, and the one that fires on a run that
        # otherwise looks perfect. Retrieval that issues no search, over almost
        # no source material, followed by a densely populated profile, means
        # the company was recalled from training weights. Every figure in it is
        # unciteable and undated, and nothing else in the trace distinguishes
        # that from a well-grounded answer: finish_reason=STOP,
        # was_repaired=false, status=OK.
        #
        # An investment committee cannot act on a recollection, so this is
        # reported as a fault even though no step failed.
        #
        # The orchestrator's grounding gate refuses the run outright when
        # NOTHING was retrieved. This is the weaker, subtler case the gate lets
        # through: retrieval ran, returned prose, and simply never searched.
        retrieval = [c for c in llm_calls
                     if c["status"] == OK
                     and any(c["step"].endswith(r) for r in RETRIEVAL_ROLES)]
        if retrieval:
            searches = 0
            for call in retrieval:
                raw = str(call.get("searches") or "").strip()
                if raw.isdigit():
                    searches += int(raw)
            supplied = int(sources.get("total_chars") or 0)
            populated = sum(int(c.get("populated_fields") or 0)
                            for c in llm_calls)
            calls = len(retrieval)
            plural = "call" if calls == 1 else "calls"
            if not searches and supplied < UNGROUNDED_SOURCE_CHARS \
                    and populated >= UNGROUNDED_FIELD_COUNT:
                add("CRITICAL",
                    "The profile was written FROM THE MODEL'S MEMORY, not "
                    "from retrieved sources. It will read as authoritative "
                    "and cannot be cited, dated or verified — treat every "
                    "figure in this run as unverified.",
                    f"{calls} retrieval {plural} issued no web search "
                    f"(searches=0) over {supplied} characters of source "
                    f"material, yet {populated} fields were populated",
                    "Two things to check, in order. (1) Search grounding: "
                    "Admin → LLM → Config profiles → 'profile.research' — web "
                    "search ON and structured output OFF (Gemini rejects the "
                    "two together). (2) Sources: whether any document was "
                    "readable, and whether the website step above succeeded.")
            elif not searches and supplied < UNGROUNDED_SOURCE_CHARS:
                add("HIGH",
                    f"Retrieval issued no web search and had almost no source "
                    f"material to work from.",
                    f"{calls} retrieval {plural}, searches=0, "
                    f"{supplied} characters supplied",
                    "Check web search is enabled on the 'profile.research' "
                    "config profile, and work through the source steps above.")

        # --- 5. Sources ------------------------------------------------
        if sources:
            has_any = any(int(sources.get(k) or 0) > 0
                          for k in ("website_chars", "documents_chars",
                                    "research_chars"))
            if not has_any:
                add("CRITICAL",
                    "No research material of any kind reached the AI. "
                    "Generation ran on the company name, website address and "
                    "country alone, which cannot produce a populated profile.",
                    "sources.summary showed website, documents and research "
                    "all empty",
                    "Work through the individual source steps above; each "
                    "records its own reason. The usual causes are: research "
                    "sources switched off, no outbound internet access, or the "
                    "document worker not running.")

        website = self.first("sources.website")
        if website and website["status"] == FAIL:
            err = str(website.get("error", "")).lower()
            if "403" in err or "forbidden" in err:
                add("MEDIUM",
                    "The company's own website refused our request. Some sites "
                    "block automated visitors.",
                    f"sources.website failed: {website.get('error')}",
                    "Nothing to fix in configuration. Upload the company's "
                    "materials instead so the profile has a source.")
            elif "certificate" in err or "ssl" in err or "tls" in err:
                add("HIGH",
                    "The company website is reachable but its TLS "
                    "certificate could not be verified by this server. This "
                    "is a trust-store problem HERE, not a network problem.",
                    f"sources.website failed: {website.get('error')}",
                    "On the application server: sudo yum update "
                    "ca-certificates && sudo update-ca-trust, then "
                    "pip install --upgrade certifi in the virtualenv. "
                    "Certificate verification is deliberately never "
                    "disabled — an unauthenticated source cannot be cited.")
            elif "resolve" in err or "connection" in err or "timeout" in err:
                add("HIGH",
                    "The company website could not be reached from the "
                    "server.",
                    f"sources.website failed: {website.get('error')}",
                    "Check the address is correct and that the server has "
                    "outbound internet access.")
            else:
                add("MEDIUM", "Reading the company website failed.",
                    f"sources.website failed: {website.get('error')}",
                    "Check the website address recorded against the company.")

        # WHAT THIS CAN AND CANNOT CONCLUDE.
        #
        # This reads the state at the START of the run, before the pipeline's
        # own reader has run. The pipeline (profile.pipeline.source2_documents)
        # downloads each file and converts it INLINE — it does not use Celery
        # and does not need a broker. So "nothing analysed yet" at this point
        # is normal for a first run and is not, on its own, a fault.
        #
        # The previous version called this CRITICAL and told the operator to
        # start Celery and Redis. That advice was wrong twice over: the
        # pipeline had usually just read the files perfectly (one run recovered
        # 552,000 characters from a financial model while this line reported
        # analysed=0), and the worker it named is not in that path at all. It
        # sent people to restart infrastructure that was never involved.
        #
        # The only genuine fault is the pipeline itself failing to read the
        # files, and `sections.summary` / the pipeline's own document events
        # report that directly. This stays as INFO for context.
        docs = self.first("sources.documents")
        if docs and docs.get("uploaded", 0) and not docs.get("analysed", 0):
            add("INFO",
                "Documents had not been read at the point sources were "
                "collected. The pipeline reads them inline during the run, so "
                "this is expected on a first generation.",
                f"sources.documents uploaded={docs.get('uploaded')} "
                f"analysed=0 pending={docs.get('pending')}",
                "No action needed unless the run's document events also show "
                "failures. The background worker is not involved in reading "
                "documents for a profile run.")

        research_fails = [s for s in self.find("sources.research.")
                          if s["status"] == FAIL]
        if research_fails:
            add("HIGH",
                f"{len(research_fails)} web research source(s) failed, so that "
                "part of the profile had nothing to draw on.",
                "; ".join(f"{s['step']}: {str(s.get('error'))[:120]}"
                          for s in research_fails[:4]),
                "If the error mentions a keyword or type error, it is a "
                "software fault — report it. If it mentions network or "
                "credentials, check outbound access and the provider key.")

        disabled = self.first("preflight.research_sources")
        if disabled and disabled.get("enabled_count") == 0:
            add("CRITICAL",
                "Every web research source is switched off, so no research was "
                "attempted.",
                "preflight.research_sources reported enabled_count=0",
                "Admin → Research Sources: enable website, market, competitor, "
                "industry and public funding.")

        # --- 6. Nothing written ---------------------------------------
        if written and written.get("sections_written") == 0 and not findings:
            add("HIGH",
                "The pipeline completed but wrote no sections.",
                "sections.summary reported sections_written=0",
                "Check the section write steps above for the reason each was "
                "skipped. If they report an unknown section key, run "
                "'python manage.py seed_platform_config'.")

        unknown = [s for s in self.find("sections.write")
                   if "unknown section" in str(s.get("error", "")).lower()]
        if unknown:
            add("CRITICAL",
                "Sections were rejected because the system does not have them "
                "registered.",
                f"{len(unknown)} section(s) rejected as unknown",
                "Run 'python manage.py seed_platform_config' on the server. "
                "It is safe to re-run.")

        # INFORMATIONAL NOTES ARE NOT FAULTS.
        #
        # The gate below decides whether anything went wrong. It used to test
        # `not findings`, which meant ANY appended note suppressed the "no
        # fault detected" verdict — so adding a single explanatory INFO line
        # would make every healthy run look unexplained. Severity is what
        # decides; INFO and NONE carry context, not faults.
        faults = [f for f in findings
                  if f.get("severity") not in ("INFO", "NONE")]
        if not faults:
            # DELIBERATE OUTCOMES ARE NOT FAULTS.
            #
            # Two of these fire routinely and both used to land on the
            # catch-all below, which reads "no fault signature matched — send
            # this to the development team". A correct skip and a correct
            # lock refusal were being reported as unexplained failures, and
            # the lock additionally logged an ERROR with a full traceback.
            # Anyone grepping ERROR found two false positives per
            # double-click, which is how the real ones stop being read.
            skipped = self.first("generation.skipped")
            if skipped:
                add("NONE",
                    "Nothing to do — the sources have not changed since the "
                    "last successful run, so the previous output still "
                    "stands. This is the cache working, not a failure.",
                    skipped.get("reason", "sources unchanged"),
                    "Regenerate with force=true to override, or add a "
                    "document / fix a failing source to change the inputs.")
                return findings

            locked = self.first("generation.lock")
            if locked:
                add("NONE",
                    "A generation for this company was already running, so "
                    "this duplicate request stopped rather than interleaving "
                    "with it. The first run is unaffected.",
                    locked.get("note", "generation already in progress"),
                    "No action needed. Wait for the running generation to "
                    "finish; its result is the one that counts.")
                return findings

            if written.get("sections_written", 0) > 0:
                add("NONE",
                    "No fault detected. Generation completed and wrote "
                    "sections.",
                    f"sections_written={written.get('sections_written')}",
                    "If the profile still looks thin, the company may simply "
                    "have little public information. Upload more documents.")
            else:
                add("MEDIUM",
                    "No specific fault signature matched.",
                    "See the full step list.",
                    "Send this trace to the development team.")
        return findings

    # -- output --------------------------------------------------------
    def to_dict(self):
        return {
            "runId": self.run_id,
            "mode": self.mode,
            "trigger": self.trigger,
            "companyId": str(getattr(self.company, "id", "")) or None,
            "companyName": getattr(self.company, "name", "") or None,
            "elapsedMs": self.elapsed_ms(),
            "steps": self.steps,
            "failures": len(self.failures()),
            "diagnosis": self.diagnose(),
        }

    def report(self):
        """Human-readable report — what the admin actually reads."""
        lines = []
        lines.append("=" * 72)
        lines.append(f"PROFILE GENERATION TRACE  run={self.run_id}")
        lines.append(f"Company : {getattr(self.company, 'name', '(unknown)')}")
        lines.append(f"Mode    : {self.mode or '(unknown)'}   "
                     f"Trigger: {self.trigger or '(unknown)'}")
        lines.append(f"Duration: {self.elapsed_ms()} ms   "
                     f"Steps: {len(self.steps)}   "
                     f"Failures: {len(self.failures())}")
        lines.append("=" * 72)
        lines.append("")
        lines.append("STEPS")
        lines.append("-" * 72)
        for s in self.steps:
            extra = " ".join(f"{k}={v}" for k, v in s.items()
                             if k not in ("step", "status"))
            lines.append(f"  [{s['status']:<4}] {s['step']:<38} {extra}")
        lines.append("")
        lines.append("DIAGNOSIS")
        lines.append("-" * 72)
        for i, f in enumerate(self.diagnose(), 1):
            lines.append(f"  {i}. [{f['severity']}] {f['cause']}")
            lines.append(f"     Evidence : {f['evidence']}")
            lines.append(f"     Fix      : {f['remedy']}")
            lines.append("")
        return "\n".join(lines)

    def persist(self, run=None):
        """Attach the trace to the ProfileGenerationRun row, if there is one."""
        try:
            if run is None:
                return
            payload = self.to_dict()
            existing = getattr(run, "diagnostics", None)
            if isinstance(existing, dict):
                existing["trace"] = payload
                run.diagnostics = existing
            else:
                run.diagnostics = {"trace": payload}
            run.save(update_fields=["diagnostics"])
        except Exception as e:      # never let diagnostics break the run
            logger.warning("GEN[%s] could not persist trace: %s",
                           self.run_id, e)


# ---------------------------------------------------------------------
# Convenience helpers used from instrumented code
# ---------------------------------------------------------------------
def trace_event(name, status=OK, **fields):
    """Record on the active trace, if any. Safe to call from anywhere."""
    t = current_trace()
    if t is not None:
        t.event(name, status, **fields)


def payload_size(value):
    """Approximate serialised size of a payload, for source measurements."""
    try:
        if value is None:
            return 0
        if isinstance(value, str):
            return len(value)
        return len(json.dumps(value, default=str))
    except Exception:
        return -1


def count_populated(data):
    """How many leaf fields in a model response actually carry a value.

    A response can be schema-valid, log as a success, and contain nothing —
    which is one of the hardest failures to spot from cost and latency alone.
    """
    filled = 0
    total = 0

    def walk(node):
        nonlocal filled, total
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            if not node:
                total += 1
            for v in node:
                walk(v)
        else:
            total += 1
            if node not in (None, "", 0, False, [], {}):
                filled += 1

    try:
        walk(data)
    except Exception:
        return -1, -1
    return filled, total
