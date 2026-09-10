# DEPLOYMENT — FundOS backend v29

Supersedes v28, which supersedes v27.5. **Deploy v29 directly.** It contains
everything in both, plus the corrections below. Do not deploy v28 separately:
its seeder ships an unguarded repair that overwrites an administrator's
choice on every run, and its endpoint-adoption logic does not fire on an
install that already has a duplicate Gemini row — which is the install this
was written for.

---

## 0. Before you start

```bash
# Rotate the Gemini API key if it has ever been pasted into a chat,
# ticket or document. Google AI Studio -> API keys -> delete, regenerate.

export GEMINI_API_KEY='…'
export GOOGLE_AI_API_KEY="$GEMINI_API_KEY"
```

Back up the database. v29 changes seeded configuration rows in place (step 4)
and adds one table; the changes are narrow and reversible, but a snapshot
costs nothing.

```bash
pg_dump -Fc fundos > /d01/backup/fundos_pre_v29_$(date +%Y%m%d).dump
```

---

## 1. **Deactivate the stray `GEMINI` endpoint — BEFORE anything else**

This step moved to the front, and the reason matters.

**Where:** Admin → LLM Endpoints → the Gemini row that is **not** the one
your role bindings point at → untick **Is active** → Save.

v28 put this *after* the re-seed and told you to expect
`gemini already served by 'GEMINI LLM' — not creating 'GEMINI'`. On your
install that line would never have printed. v28's adoption logic ordered
candidates by `("-is_active", "priority", "code")`; with both rows active at
priority 10, `"GEMINI"` sorts first, `existing.code == code`, and adoption was
skipped entirely — leaving the duplicate in place and pointing freshly
created tier profiles at the row nothing uses.

v29 fixes the cause: adoption and provider-selection now share one helper
(`_preferred_endpoint_for_kind`), which prefers the row your role bindings
already reference. But the seeder still refuses to delete a row it did not
create, so an existing stray needs this one manual step — and it must happen
before the seeder runs, or step 4 has two active rows to choose between.

Keep `GEMINI LLM`. It is the row your bindings use and the row the 19 August
logs show serving every call.

---

## 2. Deploy the code

```bash
cd /d01/fundos
tar xzf fundos-backend_v29_defect_closure.tar.gz
cd fundos-backend
pip install -r requirements.txt
```

---

## 3. Migrations

```bash
python manage.py migrate
```

Expect two to apply:

| Migration | What it does |
|---|---|
| `llm/0013_alter_llmconfigprofile_max_uses` | Carried from v27.3 — the one that was missing and had to be generated on the production host. If your host already has a locally generated `0013`, Django reports it as applied and does nothing; the two are identical. |
| `platformcfg/0004_platformflag` | **New in v29.** A one-row-per-repair marker table. It exists so that a *one-time alignment* can be told apart from an *invariant re-applied every run* — see step 4b. |

Then confirm nothing is outstanding:

```bash
python manage.py makemigrations --check --dry-run
```

Expected: `No changes detected`. Anything else, stop and report it — a model
change without a migration file is what made this step manual last time.

---

## 4. Re-seed

```bash
python manage.py seed_platform_config
python manage.py seed_llm_costs
```

The seeder is idempotent. Four things differ from v28:

**a. Endpoint adoption now actually fires.** With the stray deactivated in
step 1, expect:

```
  llm endpoint: gemini already served by 'GEMINI LLM' — not creating 'GEMINI'
```

Your `GEMINI LLM` row survives untouched, and the tier profiles are pointed
at it rather than at a code the seeder happens to prefer.

**b. The schema/search alignment runs ONCE, and only on Gemini.**

```
  llm search: turned OFF structured output on N searching GEMINI profile(s)
  — … This is a ONE-TIME alignment; a later admin change is kept
```

v28 ran this as an unguarded `.update()` across every provider on every seed.
That broke two of its own rules: `tier_contract.py` states explicitly that
the 19 August finding is Gemini-only and must not degrade Anthropic or OpenAI
output shape, and a repair that re-runs forever makes the setting permanently
unsettable — an admin could change it and the next deploy would silently
change it back. It is now recorded in `platform_flag` and does not run again.

If you ever need it to run again on purpose, delete the row:
```sql
DELETE FROM platform_flag WHERE key = 'v29.schema_search_aligned';
```

**c. Thinking budgets are seeded as integers**, including on the *active*
`tier.judgment` row. v28 fixed only the two inactive vendor alternates and
left the row that actually serves the tier as `"4000"`. It never mattered —
`thinking_budget` is a `CharField`, so both persist identically, and the
trace inconsistency was already fixed at render time by v27.5's
`_budget_str()` — but the seed now says what it means.

**d. Four roles that had no tier policy now have one.** `LLM_ROLES` has 33
entries; the policy table had 29. The four missing ones —
`assessment_extraction`, `assessment_rubric_review`,
`investor_classification`, `investor_identity` — resolved to `simple` through
a silent `.get(role, "simple")` fallback, three of them on live call sites.
That is the same defect v28 corrected for `valuation_explainer` and then
reproduced four times, because its test compared the policy table against
`DEFAULT_PROMPTS` (also 29) rather than against the roles the system can
dispatch.

---

## 5. Verify configuration

```bash
python manage.py diagnose_config
```

Work through anything not `PASS`. Three checks matter most:

* **LLM role bindings** — `assessment_inputs` must have an EMPTY output
  ceiling. See ADMIN_CONFIG_v29.md §1. Without this, the fix for the
  truncated assessment is inert. This is still the most likely reason for a
  release to look like it did nothing.
* **Prompt tier overrides** — should read `no prompt overrides the shipped
  tier`. If it reports `company_profile_section: simple -> advanced`, see
  ADMIN_CONFIG_v29.md §3 — but note that two of v28's three stated reasons
  for clearing it were wrong.
* **Research sources** — will report honestly that most adapters are
  fixtures. Expected; not a deployment failure.

---

## 6. What changed about `structured_output`, and what did not

Read this before interpreting anything in §7.

`LLMConfigProfile.output_schema` is a JSON field defaulting to `{}`, and
until v29 nothing ever wrote to it. Every provider path guards with
`if schema:`, and an empty dict is falsy, so **no `responseSchema` was ever
sent to Gemini — on any tier, for any role, for six releases.**

The 19 August measurement is correct: `tools/diagnose_gemini_search.py` sends
a real schema, and 0 of 4 configurations searched with one against 4 of 4
without. But it cannot explain your production logs, because production was
not sending a schema. Your own log settles it independently —
`company_profile_deep_extract` (0 searches) and `company_profile_section`
(2–6 searches) ran on the same profile with identical `caps_sent`. The schema
was absent from both.

v29 does two things about this:

1. **Makes the tick real where it is safe.** A blank `output_schema` is now
   filled from the role's own contract in `fundos/llm/schemas.py`. A schema
   is a property of a ROLE; `output_schema` sits on a PROFILE, and one tier
   profile serves eleven roles — which is very likely why the field was never
   populated. It was in the wrong place.

2. **Gates that hard, because the naive version is dangerous.** `SCHEMAS` is
   a *validation* contract, not a description of the whole response, and a
   `responseSchema` is *prescriptive*. `company_profile_deep_extract` returns
   244 fields and declares 3 — deriving a schema there would have cut the
   dossier to three housekeeping keys, silently, with a clean parse and a
   healthy log. Only roles in `schemas.SCHEMA_IS_COMPLETE_OUTPUT` get one; it
   currently holds `readiness_summary` and `peer_insight`, each verified
   against the code that consumes it.

For every other role the tick still has no wire effect — and now says so,
once per call, at INFO:

```
LLM: structured output is ON for role X but no schema will be sent …
```

If a provider rejects a derived schema, the call retries once without it
rather than failing, mirroring the existing `thinkingConfig` fallback.

---

## 7. Restart

```bash
sudo systemctl restart fundos-api
sudo systemctl restart fundos-worker
sudo systemctl status fundos-api fundos-worker
```

---

## 8. Settle H4 before the first production run

Sixteen API calls, no deploy, and it answers the question v27.5 and v28 both
left open: **why `company_profile_deep_extract` does not search.**

```bash
python tools/diagnose_gemini_search.py --model gemini-3.5-flash
```

Given §6, treat H1 (schema) as answered for the *API* and irrelevant for
*FundOS*. The live hypothesis is H4 — that the deep extract's system prompt
suppresses retrieval independently of everything else. Reading the grid is in
TESTING_v29.md §5. Record the answer in `fundos/llm/tier_contract.py` beside
the 19 August grid, with the date and model string.

---

## 9. First live generation

```
Admin -> Application Configuration -> AI mocked = OFF
```

Pick a company whose website is over ~30,000 characters, so the compression
reporting is exercised. Then:

```bash
grep -E 'run.economics|sources.compression|fanout_trim|CONTRACT_VIOLATION' \
     var/logs/silk_generation.log | tail -40
```

Pass conditions are in TESTING_v29.md §6.

**Set expectations honestly.** v29 fixes the assessment failure, the CKB
transaction error, the tier-policy gaps and the peer mis-grouping. It does
**not** fix `searches=0` on the deep extract, and neither did v28 — the
mechanism v28 blamed was never active. Do not read a green run as having
solved retrieval; read `searches` on the deep extract specifically.

---

## Rollback

```bash
sudo systemctl stop fundos-api fundos-worker
cd /d01/fundos && tar xzf fundos-backend_v27_5_measured_fixes.tar.gz
pg_restore -c -d fundos /d01/backup/fundos_pre_v29_YYYYMMDD.dump
sudo systemctl start fundos-api fundos-worker
```

Neither migration needs reversing. `llm/0013` is an `AlterField` widening a
column to nullable, which older code tolerates. `platformcfg/0004` adds a
table nothing else references — older code simply never reads it. The seed
changes are data and are restored with the dump.
