# CHANGES — production hardening

Everything here was found while getting the suite to green after
[the Company Profile pipeline](CHANGES_company_profile_pipeline.md) landed, and
none of it is that change's fault. The pipeline left fourteen failures in one
module. Both root causes turned out to be pre-existing defects that had been
invisible because the tests exercising them skipped, or passed, on the machine
they were written on.

§10 was added afterwards, and is a different kind of entry: everything in it
was found by pointing the pipeline at a real company with a real API key, and
none of it was reachable under `ai_mocked=True`.

**Result: 960 tests, 0 failures, 55 skipped.**

The theme is one this codebase already names repeatedly: *a thing that reports
healthy while doing nothing*. A seeder that writes keys nothing can match. A
diagnostic that checks a call path that no longer exists. A required secret
with a fallback. A config profile deactivated by a query that was aimed at
something else. A breaker sized for a call ten times smaller. Each looked fine
and was not.

---

## 1. The offline assessment seeder wrote keys the workbook does not use

`seed_assessment_config` is the fallback for an install with no filled
assessment workbook. It wrote the sub-sector metrics as `SUB_TAM`,
`SUB_TAM_CAGR`, `SUB_PEER_AGE`, `SUB_PEER_RAISE_MTHS`.

The workbook writes them as `SEC_TAM_SUB`, `SEC_TAM_CAGR_SUB` and so on, and
**the suffix is load-bearing**. The sheet's own band formula opens with

```
LET(key, SUBSTITUTE($C22,"_SUB",""), …)
```

so `SEC_TAM_SUB` is banded against `SEC_TAM`'s rubric. `SUB_TAM` matches
nothing: the parameter exists, carries weight, and can never band.

This is the *exact* defect `fundos/assessment/workbook.py` was written to end —
its module docstring names `SUB_TAM` as one of the four drift symptoms that
motivated reading the sheet directly. v24 fixed the import path and left the
fallback seeder on the old spelling, so any deployment without the workbook
kept scoring companies against four parameters that could never band, plus six
percentile nodes (`PCTL_E_2`, `PCTL_E_3`, `PCTL_E_6`, `PCTL_F_2`, `PCTL_F_3`,
`PCTL_F_6`) it did not create at all. Categories E and F are 30% of every
rating.

Fixed by making the seeder produce the same key contract the importer does:
`_SUB`-suffixed mirrors, and the six percentile nodes flagged
`is_workbook_input=False` so they do not inflate the 80-row input contract the
sheet measures coverage against.

## 2. Two workbook readers never closed the file

`assessment.benchmarks.parse_workbook` and `investors.importer.import_workbook`
both opened with `openpyxl.load_workbook(..., read_only=True)`. In read-only
mode openpyxl holds the `.xlsx` zip archive open until told otherwise, and
neither told it: the first never called `close()` at all, the second called it
only on the success path, so a missing sheet or a bad row leaked the handle.

A leaked handle per import is bad on any platform. On Windows it also holds a
lock, so the caller cannot delete the upload it just parsed — which is what
surfaced it: six tests failed trying to clean up their own fixture.

Fixed at the source rather than in the tests. `parse_workbook` now reads its one
sheet into memory through a helper with a `finally: wb.close()`;
`import_workbook` became a thin wrapper whose `finally` closes the handle
around `_import_from_workbook`, which does the work and never owns the file.

### The test-side half

Six tests wrote an `.xlsx` into an open `tempfile.NamedTemporaryFile`, which
cannot work on Windows regardless of the leak above: the handle is exclusive,
so `Workbook.save()` fails with `PermissionError` before openpyxl is involved.

`tests/conftest_helpers.temp_path()` replaces that idiom — a temp *directory*
has no handle, so the path inside it is free for any library to create, write
and reopen, and the whole directory is removed on exit either way.

## 3. A customer artefact was a hardcoded absolute path

```python
WORKBOOK = "/mnt/user-data/uploads/Tyreplex_Deal_Assessment_v21_1.xlsx"
```

in two test modules. The workbook is customer data and is not committed, so
this resolved on exactly one machine. `test_v24` skipped cleanly without it;
`test_v23` silently fell back to the seeder and asserted against a different
key set, which is how §1 stayed hidden.

Now `FUNDOS_ASSESSMENT_WORKBOOK`. Both modules skip or fall back cleanly, and
`test_v23` passing without the workbook is the assertion that §1 stays fixed:
if the offline seeder drifts from the sheet again, that module fails.

## 4. Prod and UAT could boot with the development signing key

`base.py` gives `SECRET_KEY` the fallback `"insecure-dev-key-change-me"` so a
fresh checkout runs, and `FUNDOS_JWT_SECRET` defaults to `SECRET_KEY`. Neither
`prod.py` nor `uat.py` overrode either.

A deployment that omitted the variables would therefore boot normally, serve
traffic, and sign every access and refresh token with a string printed in this
repository — forgeable by anyone who can read the source, for any user of any
tenant. `manage.py check` passes. Tokens verify. Nothing looks wrong.

Both are now `required=True` in `prod` and `uat`, so a missing variable is a
startup failure. `deploy/befundos.env.template` already asked for both; only
the settings failed to enforce it.

`tests/api/test_gemini_thinking.SigningKeyTests` pins this, and the `required`
dict in `ProductionLoggingTests` is now the readable statement of what a
deployment must supply — adding a required variable without adding it there
fails.

## 5. Diagnostics that described the previous architecture

`diagnose_config` and `profile/trace.py` still reported on the retired
one-shot-extract design. Each of these was a check that could only ever say
"fine":

| Was | Now |
|---|---|
| `PROFILE_ROLES` listed `company_profile_deep_extract` as ESSENTIAL | the two pipeline roles are essential; the per-section roles are RECOMMENDED, because they serve the Regenerate button, not a build |
| `_flags()` reported on `PROFILE_CONSOLIDATED_GEN`, a flag that no longer selects anything | reports the question bank size and the effective vs configured concurrency, and says *why* they differ on SQLite |
| `_tier_chain("advanced")` failed unless `company_profile_deep_extract` resolved to advanced | removed — resolving a tier's chain and checking which roles land on it are different questions, and asking both here made a role mismatch short-circuit every other check behind it |
| `_output_ceilings` only inspected profile roles | inspects every role with a designed cap; severity still comes from whether an essential one is affected |
| `preflight.config` logged the endpoint and model of the retired role, on every run | logs the research and synthesis bindings — the two a run actually dispatches |
| `trace.diagnose()`'s "answered from memory" rule keyed on a call that never happens | keys on the retrieval roles collectively, so it fires when the batches ran and none of them searched |

`RETIRED_ROLES` is now explicit in `diagnose_config`, and both binding checks
skip it. The roles stay declared, bound and prompted so a rollback still
resolves them — but a diagnostic must not present them as healthy
infrastructure, because nothing would notice if they broke.

`feature_flags.profile_consolidated_generation()` is deleted; it was its only
caller.

## 6. A pre-flight check lost in the port, restored

The previous generator had `_search_can_ground()`: before spending anything, it
asked whether the resolved config profile could search at all. The pipeline was
ported without it, and nothing called it any more.

Its purpose is worth more now, not less. With web search off on
`profile.research`, ten batches each go out, cost money, and retrieve nothing;
the grounding gate then correctly refuses the run and reports "nothing was
retrieved" — true, and useless, because the cause is one checkbox and nothing
points at it.

`orchestrator._search_is_configured()` restores the check and puts the cause in
the activity log *before* the batches run. It never stops a run: documents can
still ground one, and an undeterminable lookup is treated as "proceed".

## 7. The judgement stage is unreachable, and now says so

`_apply_judgment_stage` — a second pass in which a stronger model adjudicated
conflicting figures and wrote the investment thesis — is called by nothing. The
synthesis call produces `investment_thesis` from the dossier, and the pipeline
is pinned to one model throughout, which is the opposite of what a
second-opinion frontier-model pass is for.

**Not deleted, and this is deliberate.** Whether a profile should get an
independent adjudication pass over disagreeing sources is a product question,
not a defect, and everything it needs — the `company_profile_judgment` role,
the `tier.judgment` config profile, its prompt, its tests — is intact, so
reinstating it is wiring one call back in.

What was wrong was that nothing said it was dead. Its tests pass and assert
real behaviour, which reads as coverage of a live feature. The block now opens
by stating it is unreachable, why, and what it would take to bring back.

**This is a decision to make, not a finished item.**

## 8. Repository hygiene

- **`app/` deleted.** The standalone FastAPI prototype the pipeline was built
  from. Nothing in `fundos/` or `tests/` imported it; it was 30 files of
  superseded code. (Archived outside the tree before deletion, since this
  checkout is not under version control.)
- **`.gitignore` added.** There was none, which is why `__pycache__`,
  `staticfiles/`, `celerybeat-schedule`, `var/` and a virtualenv were all
  sitting in the tree.
- **`README.md` replaced.** The file at the repository root described a React
  frontend that is not in this repository — wrong project, wrong stack, wrong
  run instructions.
- **45 markdown files at the root** moved into `docs/{changelog,operations,
  configuration,testing}/` with an index at `docs/README.md`. `API_CONTRACT.md`
  stays at the root; it is the document most often linked to.
- **Build and cache artefacts removed**: `__pycache__` throughout (both a 3.11
  and a 3.13 generation), `staticfiles/` (regenerated by `collectstatic`),
  `celerybeat-schedule` (a binary shelf of last-run times).
- `requirements.txt`: `python-pptx` was listed twice.
- `tools/diagnose_gemini_search.py` reframed around
  `profile_research_batch`. Its H1/H4 findings are the evidence behind the
  pipeline's retrieval/synthesis split, so the script is worth keeping
  runnable — it is how `searches > 0` gets confirmed.

**`var/` was left alone.** It holds a dev database, uploaded documents and a
4.5 MB generation log — local working state, not build output. It is now
gitignored so it will not ship; deleting it would have destroyed local work.

## 9. Company creation 500'd on a domain another tenant already held

Found running the Company Profile workflow end to end through the real HTTP
surface, the same way a frontend would: `POST /api/v1/companies` with an
explicit `domain` raised an unhandled `IntegrityError` on
`uq_company_domain_live` whenever another tenant already held that domain —
`domain` is unique **platform-wide**, not per tenant.

This is not a new class of defect. `ProfileOnboardView` hit the identical
constraint when deriving a domain from a submitted website URL (QA 24-Jul
issue 5, `tests/api/test_qa_24jul_fixes.py::OnboardingIsRepeatable`) and was
fixed there — check availability, drop the field rather than crash on a
clash. `onboarding.create_company`, the handler behind `POST /companies`, was
never given the same guard, so the identical crash was waiting on a second,
undocumented entry point.

Two things were wrong once traced through, not one:

* **The availability check itself was blind to the collision it exists to
  catch.** `Company.objects` is `TenantManager` — every query through it is
  scoped to the caller's own tenant (`fundos/core/models/base.py`), so a
  pre-check written against it can never see another tenant's row. This is
  the same limitation `ProfileOnboardView`'s pre-check has always had; it
  survives there only because a savepoint-and-retry catches what the
  pre-check misses. `onboarding._domain_or_blank` now queries
  `Company.all_objects` instead, so the common case is caught without ever
  reaching the database's constraint.
* **The idempotent-reuse branch had no safety net at all.** Resubmitting an
  existing company's name with a newly-taken domain went straight to
  `existing.save()` with nothing to catch a race. It now runs inside a
  savepoint with the same drop-the-domain fallback the creation branch has —
  necessary even with the manager fixed above, since the check-then-write gap
  between two concurrent requests is real, just narrow.

`tests/api/test_qa_24jul_fixes.py` gained three tests: a taken domain on
`POST /companies` returns 201 with the domain dropped; the idempotent-reuse
path does the same rather than corrupting the existing company; and a
regression pin on `_domain_or_blank` itself.

The walkthrough that found this is in
[`../testing/COMPANY_PROFILE_E2E_WALKTHROUGH.md`](../testing/COMPANY_PROFILE_E2E_WALKTHROUGH.md),
which is also the frontend-facing reference for the whole
signup-through-generated-profile call sequence.

---

## 10. Four defects the first live run found, that no amount of mocked testing could

`searches > 0` had never been measured (it was item 1 of this document's own
outstanding list). Measuring it meant a real key and a real company — Zerodha,
because search grounding on an invented name returns nothing and the grounding
gate then refuses the run, proving only that the gate works.

Everything below was invisible under `ai_mocked=True`, and none of it announced
itself as a fault. Three separate runs were needed; each one got further and
uncovered the next.

**Run 1 — the pipeline was not using the model it pins.** The trace said
`gemini-3.6-flash`; the calls went to `gemini-2.5-flash`; neither was the whole
story.

* `_repair_tier` in `seed_platform_config` deactivates the other config
  profiles on a tier when it repoints that tier's provider. Its query matched
  on `tier` alone. `profile.research` and `profile.synthesis` carry a tier
  purely to inherit its capability defaults, so every seed switched them off.
  Nothing failed: `llm_generate` resolves a tier when a pinned profile is
  missing *or inactive*, so the run silently dispatched on the tier's model
  with nothing in the trace to say the pin had been dropped. The query is now
  scoped to the `tier.<tier>.*` codes the function itself writes, and
  `_profile_pipeline_profiles` repairs an already-deactivated row.
* Separately, `preflight_report` reported each role's model from
  `endpoint.default_model` — the fallback for a call that pins *nothing*. So
  the one line in the log written to answer "what will this run use?" named a
  model no call in the run would make. It now reads the pinned profile, and
  reports the pin as `UNRESOLVED` when there isn't one, which is the only
  warning available before the fact.

**Run 2 — six of seventeen sections, reported as success.** The research
profile's search cap was blank, so it inherited `DEFAULT_SEARCH_MAX_USES` (6) —
correct for the role that number was chosen for, one call answering one
question. A research batch asks **ten**. All three calls overran (11, 14, 35
searches), the third tripped the breaker's third strike, and the remaining
seven batches failed instantly against an open breaker.

Nothing there misbehaved. The breaker did exactly what it exists to do, having
been told that a correct call was a runaway. Two changes, because two separate
places imposed that six:

* The blanket `max_uses__isnull=True` repair in `_llm_tiers` now excludes the
  pipeline-owned codes. Filling six in there did not merely under-set the row,
  it made the row look deliberately configured — so the pipeline's own
  repair-the-blank pass then correctly declined to touch it.
* `profile.research` is seeded with a cap sized to the batch
  (`SEARCHES_PER_QUESTION × questions per batch` = 40). On Gemini `max_uses` is
  advisory — the provider does not enforce it (`SEARCH_ENFORCEMENT`) — so the
  number is not a limit but the threshold at which we call the spend
  anomalous. Set below what correct behaviour costs, it stops describing
  anomalies at all. The correction is one-time, stamped with a `PlatformFlag`,
  so an administrator can afterwards set any value including six and keep it.

**Run 3 — ten of ten batches, 254 searches, then everything lost at the last
call.** Synthesis timed out at 120s with the response still streaming.
`LLMEndpoint.timeout_seconds` is one number for every call the endpoint serves,
and it is set for the ordinary ones. A role designed to emit 65,536 tokens over
a 273,000-character dossier is not ordinary, and it is not slow because
anything is wrong.

`ROLE_TIMEOUT_SECONDS` adds per-role floors, resolved by `resolve_timeout()`.
It is deliberately the same argument `ROLE_MAX_OUTPUT_TOKENS` already makes
about output budgets, applied to time: a floor, not an override — a larger
endpoint value still wins, and what an administrator cannot do is set a value
below what a role provably needs, because that is not a cost control but a
guaranteed failure.

**Run 4 — fourteen of seventeen sections, and two of the three missing ones
were a bug.** With research and synthesis both complete, `news` and
`funding_history` came back empty. The dossier had pages of dated news, so the
loss was on the write side:

```
PIPELINE: could not write section news: ['“2024-08” value has an invalid date format.']
PIPELINE: could not write section funding_history: ['“2010-08” value has an invalid date format.']
```

Sources date things as precisely as they know them, and for a news item or a
funding round that is routinely a month — "August 2024", not "14 August 2024".
`_coerce_entity_value` passed the string through with the comment *"Django
parses on save"*; `DateField` rejects `2024-08`; and because `write_profile`
isolates at section granularity, one month-precision row took every other row
in its section with it. Seventeen news items lost to one date.

The decision this needed had already been made and recorded — in
`FundingRound.date_precision`, whose docstring says *"Rounds are routinely
announced with month precision, and the month IS the fact. The date is stored
on the first of the period and this says how much of it was actually known."*
Nothing had ever written to that column. `section_writer._as_date` now
implements it: `YYYY-MM` → the first of the month, `YYYY` → the first of the
year, an unparseable value → no date rather than no row.

`spec_serializer` renders the date back at the precision it was reported with,
so a round announced in August 2010 reads as `"2010-08"` and not as a 1 August
the source never claimed. This keeps the read-edit-PATCH round trip exact —
writing `"2010-08"` back yields the same date and the same precision — and it
adds no field to the §7 row: the precision is carried by the string's own
shape. **A client parsing `date` strictly must now accept `YYYY` and `YYYY-MM`
alongside a full ISO date.**

**One thing that looked like a defect and was not.** `LLMModelCost` had no row
for `gemini-2.5-flash`, so every call recorded zero cost. That is
`seed_llm_costs`, documented in the README and in both deployment runbooks, not
having been run on the dev database. The command already derives the pair from
the configured config profiles and needed no change.

Regression cover: `tests/api/test_profile_pipeline.py::SeededConfigProfileTests`
(both seeder defects, plus that an administrator's cap survives a re-seed),
`::RoleRegistrationTests` (the timeout floors) and
`::ReportedDatePrecisionTests` (partial dates, and that one of them no longer
costs its section); `tests/api/test_generation_trace.py::PreflightTests` (the
reported model matches the pin, and an unhonourable pin says so).

---

## What is still outstanding

1. **`write_profile` isolates at section granularity, not row.** §10 run 4 was
   caused by a bad date, but amplified by this: any single unstorable row — a
   headline past 512 characters, say — still discards every other row in its
   section. Row-level isolation is a real change (`_replace_entity_rows`
   deletes the section's existing rows before writing, so a partial write
   needs thinking about) and is deliberately not made here.
2. **`NewsItem` has no `date_precision` column.** `FundingRound` does, and now
   records it. A news item reported as "August 2024" is stored on the 1st with
   nothing saying so, so it serialises as a full ISO date. Adding the column
   is a migration plus a serializer change, and is the obvious follow-up.
3. **The judgement stage** (§7) — reinstate it or delete it.
4. **Single-section regeneration** still calls the per-section roles against a
   live source bundle rather than re-synthesising from the stored dossier. See
   [`CHANGES_company_profile_pipeline.md`](CHANGES_company_profile_pipeline.md)
   §11.
