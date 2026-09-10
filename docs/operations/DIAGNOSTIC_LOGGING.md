# Diagnostic logging — where to look when a profile comes back empty

## The log file

**There is nothing to switch on.** The trace is unconditional: every
generation writes to the `fundos.generation` logger on every run, and the
file handler is registered when the process starts. If the file is empty,
nothing has been generated since the last restart — that is not a fault.

```
<FUNDOS_DATA_DIR>/logs/silk_generation.log
```

The directory resolves in this order:

1. `FUNDOS_LOG_DIR`, if set.
2. `FUNDOS_DATA_DIR/logs`, if `FUNDOS_DATA_DIR` is set.
3. Otherwise derived from the code path. Under a release-per-deploy layout
   (`/d01/fundos/releases/<date>/…`) that is `/d01/fundos/shared/data/logs`,
   deliberately OUTSIDE the release folder so history survives a deployment.
   With no release marker in the path it is `<application root>/var/logs`.

Three things have to be true, and `diagnose_config` checks all three under
**Diagnostic log**:

* the process is on a settings module that EXTENDS the base `LOGGING` dict
  rather than replacing it (replacing it drops the `generation_file` handler
  and the four `fundos.*` loggers, which is what happened before v18);
* the directory is writable by the service user — `base.py` wraps the
  `mkdir` in a bare `try/except` and falls back to console-only silently;
* both `fundos-web` **and** `fundos-worker` have been restarted since the
  deployment. Generation runs in Celery, so the worker writes most of these
  lines, and logging configuration is read once at process start.

To confirm without waiting for a generation:

```bash
python manage.py shell -c "
import logging
lg = logging.getLogger('fundos.generation')
print(lg.handlers)
lg.info('GEN[selftst] step=\"logging.selftest\" status=\"OK\"')"
```

A `RotatingFileHandler` in that list means the trace is being captured.

Rotates at 20 MB, keeps 10 files (~200 MB retained). It carries the whole
Company Profile pipeline: the configuration state at the start of each run,
every source, every AI call, every section write, and a plain-language verdict.

**This is the file to send when reporting a generation problem.** One run is
self-contained, so a single `grep` is usually enough:

```bash
LOG=/d01/fundos/shared/data/logs/silk_generation.log   # see the paths above

grep "GEN\[e7c755\]"   "$LOG"      # one whole run
grep "DIAGNOSIS"       "$LOG"      # verdicts only
grep 'status="FAIL"'   "$LOG"      # failures only
tail -100              "$LOG"      # the last run
```

## What a run looks like

```
GEN[e7c755] step="preflight.config" status="WARN" ai_mocked=true llm_bindings=0 …
GEN[e7c755] step="sources.deal" status="OK" dealId="fcc1f68b…"
GEN[e7c755] step="sources.website" status="FAIL" url="https://…" error="ConnectionError: …"
GEN[e7c755] step="sources.documents" status="OK" uploaded=3 analysed=0 pending=3 note="documents uploaded but not yet read…"
GEN[e7c755] step="sources.research.market" status="OK" ms=412 chars=1180
GEN[e7c755] step="sources.summary" status="OK" website_chars=0 documents_chars=0 research_chars=230 …
GEN[e7c755] step="llm.call.company_profile_deep_extract" status="OK" endpoint="ANTHROPIC" ms=41022 populated_fields=0 …
GEN[e7c755] step="sections.write.company_story" status="OK" chars=1840
GEN[e7c755] step="sections.summary" status="OK" sections_written=9
GEN[e7c755] DIAGNOSIS 1 [CRITICAL] … | EVIDENCE: … | FIX: …
```

Every line of one run shares the same six-character run id.

## The DIAGNOSIS lines

The log does not just record what happened — it states the probable cause and
the fix, ordered with the most likely root cause first. It recognises, among
others: demonstration mode left on; no AI endpoint bound; rejected credentials;
rate limit or billing; a model name pointed at the wrong vendor; timeouts; no
outbound network; documents uploaded but never read; research sources switched
off; a model that answered successfully with every field empty; and sections
rejected because the registry has not been seeded.

## Two commands

```bash
python manage.py diagnose_config
```
Checks every setting the pipeline depends on and prints PASS/FAIL/WARN with the
remedy for each. Exits non-zero if a CRITICAL check fails, so it can gate a
deployment script. Run it after every deploy.

```bash
python manage.py diagnose_profile --company "Terracotta Living"
python manage.py diagnose_profile --company "Terracotta Living" --generate
python manage.py diagnose_profile --company "Terracotta Living" --json
```
Reports on one company: the current configuration, the last recorded run, which
sections hold data, and the diagnosis. `--generate` runs a fresh traced
generation (this costs an AI call). `--json` for machine reading.

## Also stored in the database

Each run's full trace is written to `ProfileGenerationRun.diagnostics` under the
`trace` key, so history survives log rotation and can be read per company
without server access.

## A note on why this was needed

Generation can fail in about a dozen places and nearly all of them failed
*quietly*: a research adapter raising inside a `try/except` and logged at
WARNING; mocked mode returning plausible stub content; a model returning valid
JSON with every field blank. All produced the identical visible symptom — an
empty profile — and none announced itself. The trace makes each step state its
outcome, so the failing step is named rather than inferred.
