# CHANGES — Company Master Data Pipeline

Company Profile generation is rebuilt. One search-grounded extraction call
becomes a two-source pipeline that researches the company across an editable
100-question bank, reads its uploaded documents properly, merges both into a
durable markdown dossier, and derives the structured 17-section profile from
that dossier in a single call.

Ported from the `app/` FastAPI prototype into Django/Celery. The prototype's
question bank, dossier design, normalizer and OCR policy are kept; its
provider clients, job store and web tier are not — those duplicate machinery
this codebase already has and does better.

---

## 0. The finding this is built on

`CHANGES_v29.md` §7 states the problem and defers the fix:

> **The advanced tier still cannot express "search AND structured output."**
> All four search-required roles declare a schema… "Needs to search" and
> "needs a guaranteed shape" are independent axes; the tier enum bundles them
> and forces a choice, and 100% of search roles are on the losing side.

It names the correct fix — "a two-phase split… an advanced retrieval pass (no
schema, search on) whose output becomes context for a simple structuring pass
(schema on, cheap, no search)" — and declines to do it because it changes
pipeline call counts and cannot be validated without a live key.

That is this change. The conflict is dissolved rather than worked around: the
research pass needs no schema because its output is prose, and the synthesis
pass needs no search because it reconciles a dossier it is handed.

---

## 1. What replaced what

| | Before | After |
|---|---|---|
| Retrieval | 1 call (`company_profile_deep_extract`) | 10 concurrent calls, one per topic, failure-isolated |
| Questions asked | implicit in one prompt | 100, in an admin table |
| Documents | `pypdf` text layer, `.txt`, `.csv`. **DOCX/XLSX/PPTX returned `""`** | MarkItDown + selective OCR |
| Evidence | none — sources compressed into a prompt and discarded | `consolidated.md`, stored per run, downloadable |
| Structuring | same call that did retrieval | separate call over the dossier |
| Progress | profile `status` flips | per-stage timings, activity log, per-file detail |
| Cost visibility | thread-local ledger | same, now correct across worker threads (§5.2) |

Deleted: `_generate_profile_inner`, `_generate_consolidated`,
`generate_deep_profile`, `_consolidated_generation_enabled` (705 lines).
`POST /profile/deep-generate` now runs the pipeline with `force=True`, which
is what a caller reaching for "deep generate" meant.

---

## 2. Everything is configurable from the admin panel

The prototype hardcodes its questions, its section field specs and its
prompts. All three are now tables, seeded from those values and winning over
them once seeded. Code defaults remain as the fallback, so an unseeded install
still works — the established `PromptTemplate` pattern.

| What | Where | Effect of an edit |
|---|---|---|
| The two prompts | AI Prompts → `profile_research_batch`, `profile_synthesis` | next run |
| The 100 questions | Research question batches (inline questions) | next run |
| Section field specs | Company Profile Sections → *Generated output contract* | prompt asks for the new field, normalizer stores it |
| The model | LLM → Config profiles → `profile.research` / `profile.synthesis` | next call |
| Concurrency, OCR, timeouts, dossier cap | Application Settings | next run |

**Adding a field to a section is an admin edit.** `field_spec` is rendered
verbatim into the generation prompt and read back by the normalizer, so there
is no second copy to keep in step. Previously that shape lived in a Python
constant that had to agree with the prompt, the normalizer and the serializer
in four places at once.

### 2.1 A flag mechanism that had never worked

`feature_flags.get_flag` fell back to `AppConfiguration.feature_flags` — a
field that does not exist on the model; the column is `features`. The JSON
branch was therefore unreachable and **every flag without its own column
silently returned its default no matter what an administrator set**, including
`PROFILE_CONSOLIDATED_GEN`. Fixed, and the pipeline's own dials are real
columns rather than blob keys, because a labelled field with help text is the
difference between a setting someone can find and one they have to be told
about.

---

## 3. The LLM layer is deliberately thin

One model, `gemini-2.5-flash`, for both calls. Both pin an explicit
`LLMConfigProfile`, which short-circuits tier resolution entirely
(`llm_generate` only resolves a tier when no profile is supplied). So there is
no tier policy to reason about, no per-tenant tier row, and no judgement
stage — two admin rows an operator can see and edit.

| code | search | structured output |
|---|---|---|
| `profile.research` | **on** | off — it returns markdown |
| `profile.synthesis` | off | off — see §3.2 |

### 3.1 One adapter change: `response_kind="text"`

`llm_generate` was hard-wired to JSON: parse → repair → normalise → validate.
For a role whose output *is* prose that path is not merely unnecessary, it is
destructive — `_extract_json` on a research answer containing a fenced JSON
snippet would return the snippet and discard the answer.

`response_kind="text"` skips those four steps and returns
`{"text", "grounding", "searches"}`. Everything else is untouched: endpoint
resolution, capabilities, budget enforcement, the breaker, search accounting
and the `LLMCallLog` row. That is the entire reason a prose role goes through
this function instead of calling a provider directly, and it is why
`app/llm/gemini_client.py` — which reimplements key rotation, retry, a circuit
breaker and a cost ledger — was not ported.

### 3.2 `profile_research_batch` IS declared search-required

Initially it was not — the reasoning being that `SEARCH_REQUIRED_ROLES` drives
tier coercion, which this role bypasses by pinning a config profile, so
declaring it could only ever abort a run an administrator had deliberately
configured.

The existing suite disagreed, and it was right. `test_v28_tier_policy_and_seed`
and `test_v29_defect_closure` both assert that **any role on the advanced tier
must be declared search-required**, a rule three releases were spent
establishing. Declaring it changes nothing on the happy path (the pinned
profile skips tier resolution) and matters in exactly the two cases where
something has gone wrong: if the config profile is deleted the role falls back
to the *searching* tier rather than silently downgrading, and if search is
switched off on the profile the wire-level contract check refuses the call
instead of billing for an uncitable answer.

The supported way to run a cheaper pass is to deactivate research batches —
each batch is one call — not to strip the tool from a retrieval role.

### 3.3 `profile_synthesis` is NOT given a derived responseSchema

Tempting, and wrong. Its validation contract is `{"sections": dict}` — one
key — and `json_schema_for` projects a **flat** object, so it would send
Gemini a schema saying "return an object with one untyped property": all cost,
no guarantee. This is the same trap v29 §1.3 documents for
`company_profile_deep_extract`, where deriving a schema from a three-key
validation contract would have reduced a 244-field dossier to three
housekeeping keys.

The real shape is three levels deep, is single-sourced in
`fundos/profile/schema.py`, is rendered into the prompt as explicit field
specifications, and is enforced on the way back by `normalize_profile`. That
is a **better** guarantee than a responseSchema, because it coerces
near-misses instead of rejecting them.

---

## 4. Document extraction is real now

The previous extractor read a PDF's text layer, `.txt` and `.csv`, and
returned `""` for everything else, with a comment promising "LibreOffice
headless + openpyxl in deployed envs" — machinery that was never built.

**A founder who uploaded a pitch deck and a financial model contributed
nothing to their profile, and had no way to know**: the file appeared in the
Document Center marked as uploaded. `MaterialAsset.ocr_applied` existed as a
column and was always `False`, because nothing ever ran OCR.

Now: MarkItDown for native text and tables across PDF/DOCX/XLSX/PPTX/CSV/HTML,
plus selective OCR — a PDF page is rendered at 2x and read only when its
native text falls below 120 characters, and every image embedded in a slide is
read, because charts carry numbers that exist only as pixels.

Every file's treatment is decided by a table **before** any work starts,
recorded on the row (`handler`, `ocr_status`, `ocr_reason`, `native_chars`,
`ocr_units_ocred`…) and stated in the dossier. "Was my deck actually read, and
how?" is answerable from the row rather than from logs.

Degrades honestly: no Tesseract on the host means native-only extraction with
the reason recorded once per file — never once per page, which would bury one
environment problem under forty identical lines.

---

## 5. Concurrency, and two things it broke

The ten research batches run in a thread pool, not a Celery chord: the tenant
is a `threading.local`, the dossier is shared mutable state, dev runs Celery
eagerly (so a chord would serialise exactly where it is cheapest to test), and
failure isolation is per batch rather than per task.

`ThreadPoolExecutor` inherits none of Django's ambient state.
`pipeline/concurrency.py` is the single place that handles it, and nothing in
the package may call `submit` directly.

### 5.1 A `Context` cannot be entered twice — caught by a test

The first implementation captured one `contextvars.copy_context()` snapshot
and shared it across the pool. `Context.run()` refuses re-entry, so that works
only while nothing overlaps and **fails precisely when concurrency actually
happens**: the first batch would run and every batch overlapping it would die
with `RuntimeError: cannot enter context`. Now one copy per job.

### 5.2 The cost ledger would have reported 1 call in 11

`llm/ledger.py` is thread-local by design (one book per run, so two tenants'
concurrent generations cannot blend) and its docstring says plainly: *"If that
ever changes, this needs a contextvar."* Making the generation path concurrent
is that change.

A contextvar is not the fix — `copy_context()` copies the mapping, so a worker
still needs the parent's list *by reference*, and a bare contextvar default
would reintroduce the cross-run blending thread-local storage exists to
prevent. Instead the parent hands its book to each worker explicitly
(`ledger.book()` / `ledger.adopt()`). Verified: a run reports 11 calls.

### 5.3 SQLite cannot take concurrent writers

Every call the pipeline makes writes — an `LLMCallLog` row, a progress update,
an activity event — and SQLite serialises writers at the file level. Measured
on the dev database: **nine of ten batches lost to `database table is locked`**.

SQLite is the dev and test backend, so this is not a production constraint; it
is the difference between a pipeline that can be exercised locally and one
that only works after deployment. Concurrency here is a latency optimisation,
never a correctness requirement, so `max_parallelism()` clamps to 1 on SQLite
and `run_concurrently` runs inline rather than spinning up a pool of one —
which also makes the pipeline usable from Django's `TestCase`, whose
uncommitted transaction a separate thread's connection cannot see.

### 5.4 Also handled

Worker threads re-enter the tenant and deal, **clear them on release** (a
pooled thread is reused, and a stale tenant leaking into the next job is a
security issue rather than a correctness one), and close their DB connection —
Django opens one per thread and closes only those it created.

---

## 6. The grounding gate is kept, and fires earlier

`_check_grounding` calls the same
`assessment_extraction.grounding` the scorecard uses, rather than restating
the policy — the two disagreeing is exactly what let a refused-to-score run
publish a fabricated dossier anyway.

It now gets better evidence: `searched` is a real count of searches the
provider performed, not an inference. And it runs **before** synthesis, so an
ungrounded run is refused before the expensive call rather than after. That is
why `UngroundedGeneration.populated_fields` is always 0 here — the old design's
headline number ("122 fields returned, nothing retrieved") described a gate
that fired after the model had already answered.

A refusal is recorded distinctly from a failure (`status="refused"`), because
the remedies are opposite, and neither stamps the sources fingerprint — a
refused run must stay re-runnable without `force`.

---

## 6a. Two regressions the existing suite caught

Worth recording, because both were introduced by this change and neither was
visible from the new tests alone.

**AI generation was deleting founder-entered rows.** `write_profile` routes
through the same `update_section_from_data` a human PATCH uses, and for entity
sections that path is a *full replacement* — correct for a human, who just saw
those rows and edited them, and wrong for a generation run, which has not seen
the founder's own entries and is in no position to remove them. The previous
pipeline was explicit that record tables are "seeded only when empty" for
exactly this reason. `test_clarifications` caught it.

The rule now: **an AI write replaces only its own previous output.** Rows whose
provenance is not `ai_research` are kept, and an AI row whose name a human has
already claimed is skipped. `needs_input` is computed from what the section
*holds* rather than what the write contributed, so a pass whose rows were all
skipped does not ask the founder to redo work they have already done.

> **Superseded on 21 Aug 2026 — skipping the row was too blunt.** It protected
> the human's data by discarding everything researched about that person, so a
> live profile carried its two actual principals with a name, a LinkedIn URL and
> nothing else. A claimed row is now *merged* rather than skipped: blank fields
> are filled, fields a previous AI pass supplied are refreshed, and a field a
> human actually filled is never touched. The rule above is unchanged in intent
> and now applies per field rather than per row. See
> [`CHANGES_profile_data_integrity.md`](CHANGES_profile_data_integrity.md) §6.

**An empty `founders` section reported itself complete.** Adding
`is_founder: True` to the empty template row made `_row_has_value` see a
truthy value where there was no data. Fixed at the root: a boolean is never
evidence that a row carries data, because every row has one. Booleans qualify
a row; they do not populate it.

## 7. The 17-section contract

Adopted from the prototype. Storage keys are unchanged, so there is no data
migration: five sections are renamed on the wire only, and both spellings
resolve so a client mid-migration is not broken by the rename alone.

**Frontend impact, unavoidable:** `recent_news`→`news`,
`investors_and_cap_table`→`investors_cap_table`,
`company_story_and_usp`→`company_story`,
`industry_and_market_research`→`industry_research`.

### 7.1 Two retirements that keep their substance

Four sections leave the contract. Two are pure removals
(`ai_company_summary`, already inactive per CR-09; `leadership_detail`,
superseded by `founders[].background`). The other two are **not** LLM output,
and deleting them would have lost working behaviour:

- **`key_people`** — merged into `founders`, which now carries `is_founder`.
  The `KeyPerson` table and `/profile/records/key_people` are untouched;
  `section_writer._replace_people` splits the one wire list back into two
  tables on write and the serializer merges them on read. Presenting one list
  is deliberate: a model knows who runs a company, not which of our two tables
  a person belongs in.
- **`derived_multiples`** — still computed by `compute_profile_ratios`. The
  prompts explicitly forbid the model from producing ratios; nothing about
  that changes. It simply stops being its own top-level section.

Both are deactivated, never deleted — content is preserved and re-activating
the row in admin restores the section, builders included.

---

## 8. Cost

**~1 grounded call per run → ~10**, plus one large synthesis call. Gemini
bills search grounding per *request* (~$35/1k), so this is roughly a 10× rise
in the dominant line item. Controls are built in rather than retrofitted:

- individual batches are deactivatable in admin — the most direct lever,
  because each batch *is* one billed call;
- concurrency, the model and the dossier cap are admin fields;
- `_enforce_budget` / `TenantLLMBudget` runs per batch, so a tenant budget
  stops a run mid-flight **and the dossier written so far is still persisted**;
- the dossier cap is now actually enforced. The prototype's `MAX_DOSSIER_CHARS`
  was documented and read by nothing, which its own config file admits — a cap
  nothing enforces is worse than no cap, because it advertises a bound the
  system does not have.

---

## 9. New API

```
GET /companies/{id}/profile/runs            recent runs
GET /companies/{id}/profile/runs/{runId}    stages, timings, activity log,
                                            per-document extraction detail,
                                            and a signed dossier download
```

One response rather than several endpoints, so a client polling a
twenty-minute run makes one request regardless of how far along it is.

---

## 9a. Existing tests that changed, and why

Twenty-one assertions in the existing suite moved. Every one is recorded here,
because "the test was updated" is the sentence behind most silently weakened
guarantees.

**Renamed keys (7).** `recent_news`→`news`, `industry_and_market_research`→
`industry_research`, and so on. The assertion is unchanged; only the key is.

**Retired sections (6).** `leadership_detail` and `derived_multiples` are
deactivated, not deleted, so those tests now address them through
`serialize_section` and re-activate the row first — which makes them assert
something *stronger* than before: that "deactivated, not deleted" is a real
promise and re-activating a section restores a working read/write path.
`key_people` tests read the same rows back out of `founders`.

**Source-text scans (4).** Three asserted the shape of
`_generate_profile_inner` / `_generate_consolidated` by reading their source
for substrings and except-clause ordering. CHANGES_v29 §5.2 makes the case
against that form directly — "a test that reads source text tests the comment,
not the behaviour" — and all three broke on a deletion rather than a
regression. Replaced with behavioural equivalents:

| Was asserted by reading source | Now asserted by running it |
|---|---|
| the counting window opens before sources and closes after assessment | reported `llmCalls` equals the ledger, and includes a call dispatched after the pipeline returns |
| `UngroundableCall` is caught before the generic fallback | an ungrounded run returns `mode="refused"` and writes no section |
| an empty block is not "claimed" | a section the model left empty is reported empty, not written and not failed |

**Test drove a path that no longer exists (1).**
`test_offshape_response_still_populates` fakes a dispatcher keyed on a section
label in the prompt, then ran a full profile build. The pipeline's two prompts
carry no section label, so every branch fell through and the test would have
passed or failed for reasons unrelated to its subject. It now drives the
normalizer through section *regeneration*, which is the surviving caller of
those roles.

**A rule I got wrong (2).** See §3.2 — the suite was right that an
advanced-tier role must be declared search-required.

**Fixing that test found a real bug (1).** Routing regeneration by section type
(see below) was needed to make it pass, and is a defect fix in its own right.

### A defect found on the way

`regenerate_section` sent **every** section to the prose generator, so
regenerating a structured section (`products_services`, `competitors`,
`financial_summary`, …) wrote a prose summary into `content` and left
`structured` — and therefore the section's `data` — empty. That is the Gap2
defect ("populated in content but empty in sections.data") still live on the
regenerate path, where it was less visible because it affects one section at a
time. It now dispatches by what the section stores, as the full run always did.

## 10. Verification

`tests/api/test_profile_pipeline.py` — 68 tests, all passing. Grouped by the
property each protects. The normalizer cases encode observed model behaviour —
a flattened wrapper, an omitted section, an object where an array was asked
for — rather than imagined behaviour.

Two of them caught bugs in this change before it left the branch: the
shared-`Context` defect (§5.1) and the ledger's thread-locality (§5.2).

End-to-end in mocked mode, on a clean database:

```
migrate · seed_platform_config · generate_profile(force=True)

  section schema: 17 completed, 3 deactivated
  research bank:  10 batches, 100 questions
  profile pipeline: 2 config profiles on gemini-2.5-flash

  mode=pipeline  batches=10/10  dossier=5,482 chars  llm_calls=11
  stages recorded: researching, processing_documents, consolidating,
                   synthesizing, writing, completed
  activity events: 25
  API contract:    17 sections, 16 populated
                   (document_center empty — nothing was uploaded)
```

`llm_calls=11` is the number to look at: ten research batches dispatched from
worker threads plus one synthesis call, all attributed to the run. Before §5.2
it would have read 1.

**Full suite: 933 tests, 0 failures, 55 skipped.**

It did not start there. This change left fourteen failures in
`test_v23_phase3_phase4`, and both causes turned out to be defects in code this
change never touched — see
[`CHANGES_production_hardening.md`](CHANGES_production_hardening.md) §1 and §2.
Briefly: the offline assessment seeder wrote sub-sector keys the workbook does
not use, and two workbook readers never closed the file they opened. Neither
was introduced here; both were hidden behind a fixture path that resolved on
one machine.

Running it yourself needs the dependencies installed. Python 3.11 with
`requirements.txt` is the documented target — 3.13 works for the test suite but
is outside what `Django==4.2.11` supports.

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
DJANGO_SETTINGS_MODULE=fundos.settings.dev .venv/bin/python manage.py migrate
DJANGO_SETTINGS_MODULE=fundos.settings.dev .venv/bin/python manage.py seed_platform_config
DJANGO_SETTINGS_MODULE=fundos.settings.dev .venv/bin/python manage.py test tests
```

**Verified against a live model on 21 Aug 2026.** This section previously read
"not yet verified", and that is no longer true — the disagreement between this
file and
[`../testing/COMPANY_PROFILE_E2E_WALKTHROUGH.md`](../testing/COMPANY_PROFILE_E2E_WALKTHROUGH.md)
was on the project's own headline open question, which is the worst place for
two documents to differ.

The measurement that matters — `searches > 0` on the research batches, which
CHANGES_v29 records as never having been achieved — came back **169** on a real
`GEMINI_API_KEY` against Zerodha: 10/10 batches, 11 LLM calls, a
258,792-character dossier, 16/17 sections, ~11 minutes wall clock. §0's
reasoning about why the two-phase split should work is now evidence rather than
reasoning. The walkthrough has the full call sequence and the numbers.

Two later passes are what that run bought, and neither was reachable from a
mocked one:
[`CHANGES_production_hardening.md`](CHANGES_production_hardening.md) §10 records
the four defects only measuring `searches` could find, and
[`CHANGES_profile_data_integrity.md`](CHANGES_profile_data_integrity.md) records
seven more found by reading the profile the run produced — a wrong-unit
financial table, a scalar field holding a list, and citations that expire.

---

## 11. Deliberately not done

**Single-section regeneration still uses the old per-section roles.**
`POST /profile/sections/{key}/regenerate` and the field-level regenerate call
`company_profile_section` / `company_profile_field` against the live source
bundle, exactly as before. The right design is to re-synthesise that one
section from the stored dossier — one cheap call, no new research, and
coherent with the rest of the profile because it reads the same evidence. That
is a self-contained follow-up; leaving those roles in place keeps two live
endpoints working rather than shipping them broken.

Consequently `company_profile_section`, `company_profile_structured` and
`company_profile_field` are retained rather than retired as originally planned.
`company_profile_deep_extract` is no longer called by anything but its prompt
and role are also left in place, so the two can be retired together once
regeneration moves onto the dossier.

**The `app/` directory is untouched.** It is the prototype this was ported
from, and keeping it until the ported pipeline has run against a live key
means the reference implementation is still there to compare against. It is
not imported by anything in `fundos/` and can be deleted once §10's live
verification is done.
