# ADMIN CONFIGURATION — what to change, and why

Four items. Two are wrong on your install today and are worth changing
whether or not you deploy v28. None requires a deploy.

---

## 1. Remove the `company_profile_section` tier override — HIGH

**Where:** Admin → AI Prompts → `company_profile_section` → **Tier** → clear
the field (leave blank) → Save.

**The problem.** `diagnose_config` reports:

```
Prompt tier overrides   WARN   company_profile_section: simple -> advanced
```

Someone set this role to the advanced tier. The shipped policy puts it on
`simple`, and the shipped policy is right: this role writes profile sections
from material the deep extract has already retrieved. It is extraction. It
does not need to reach the internet.

**Why it matters more now than it did.** Three consequences, in order of
severity:

1. **It loses its structured output.** Since v27.5 the advanced tier drops
   `responseSchema`, because a schema suppresses `google_search` on Gemini
   (measured 19 Aug 2026: 0 of 4 configurations searched with a schema, 4 of 4
   without). The section writer parses structured output. The override is
   therefore degrading correctness, not just adding cost — and this is
   invisible from the admin screen, which still shows the tick.
2. **It funds a thinking budget on every call.** Three section calls a run,
   ~4,600 thinking tokens, billed at the output rate.
3. **It attaches a search tool** the role has no use for.

**A correction to earlier advice.** I previously said to keep this override
because it was the only thing in the system that searched. That was wrong.
The right fix is to get the *deep extract* searching — which is what v27.5 and
the H4 diagnostic are for — not to leave an extraction role on a research
tier as a workaround.

---

## 2. Blank the `assessment_inputs` output ceiling — HIGH

**Where:** Admin → LLM Role Bindings → `assessment_inputs` → **Max output
tokens** → clear the field (leave empty) → Save. Do the same for
`company_profile_deep_extract`.

**The problem.** The adapter resolves the ceiling as:

```python
role_cap  = ROLE_MAX_OUTPUT_TOKENS.get(role) or DEFAULT_MAX_OUTPUT_TOKENS
max_tokens = min(binding_value, role_cap) if binding_value else role_cap
```

A number in this field can only *lower* the role's designed budget. v27.5 gave
`assessment_inputs` a designed ceiling of 8,192 to fix the truncation that
killed it on both 19 August runs:

```
run 1  JSONDecodeError at char 4492 after 43.2s     whole assessment lost
run 2  finish_reason=MAX_TOKENS, thinking=1945,
       completion_tokens=0, was_repaired=true       7 of 30 fields survived
```

If an administrator has typed a number here, `min()` keeps it and **the fix
does nothing**. The field's help text — *"Leave EMPTY to use the role's
designed output budget"* — is load-bearing, not a style note.

**Check it even if you think it is empty.** This is the single most likely
reason for v28 to look like it did not work.

---

## 3. Deactivate the stray `GEMINI` endpoint — MEDIUM

**Where:** Admin → LLM Endpoints → any row for Gemini that is **not** the one
your role bindings point at → untick **Is active** → Save.

**The problem.** You were right that this came from the seed. The seeder
created a canonical `GEMINI` row unconditionally, alongside the `GEMINI LLM`
row you had made by hand — after which two rows read the same
`GEMINI_API_KEY`, both looked usable, and which one served a given call was
arbitrary. The dedupe logic then printed `ignoring duplicate endpoint(s)
GEMINI` on every seed run: the seeder reporting its own output as a problem.

**v28 fixes the cause** — the seeder now adopts an existing provider row
instead of creating a second one. It will not delete a row it did not create,
so a stray `GEMINI` already in your database needs deactivating by hand, once.

Keep `GEMINI LLM`: it is the row your bindings use and the row the 19 August
logs show serving every call.

**Also check** the model string on the row you keep. Preflight reported
`gemini-3.6-flash` while the tier seeds specify `gemini-3.5-flash`. Both are
priced ($7.50 vs $9.00 per M output), so nothing zeroes out — but fix on one
model before comparing costs between runs.

---

## 4. Sector benchmark data — MEDIUM, and NOT what I said earlier

**Where:** `python manage.py import_sector_benchmarks <workbook.xlsx>`

**A correction.** I previously said ClearDekho's rating was "quietly wrong by
a fifth" because `subSector: 'Eyewear'` matched no `SectorMapping` row, and
recommended adding that row. Having read the scoring engine, that was wrong
on both counts.

`weighted_rollup` averages over **scored children only** and redistributes the
weight of a blank across the rest, surfacing each one's `applied_weight`. So a
company with no benchmark group is scored on the four categories that did
resolve, each carrying 25% instead of 20%, and the response says so. The
rating is not wrong. It is computed over less, and it reports that.

The resolver also refuses to fuzzy-match below a high floor, deliberately:
assigning a construction firm to a D2C benchmark group would produce a
confident number against the wrong comparators, which is worse than a blank.

**So the real action is data coverage, not a one-row patch.** If your
portfolio spans construction, electronics or logistics and your workbook is
D2C-oriented, every company outside it scores without category F — correctly,
but with less evidence. Load a workbook covering the sectors you actually
invest in. Adding rows one sector at a time as they appear is how the gap
stays permanently one step behind the portfolio.

**Nothing needs to change in code for a new industry.** The profile pipeline
carries no sector names; `tests/api/test_v28_domain_agnostic_e2e.py` asserts
that with an AST scan, and runs the same generation for construction,
electronics and logistics companies to confirm the pipeline shape does not
branch on domain.

---

## What NOT to change

**Do not seed the `cp_extract.*` config profiles.** Nine are missing and
falling back to the tier default, which produces eighteen INFO lines a run and
is harmless. Creating them means nine more rows to keep coherent with the tier
contract, for no behavioural gain.

**Do not enable more research sources expecting more data.** The toggles in
Admin → Research Sources work, and every one of the nine adapters behind them
returns an empty list pending a feed that was never ratified. `collect_sources`
reports them honestly as `SKIP`. Turning them on changes nothing; the fix is
code, not configuration.

---

## Order

| | Item | Time | Effect |
|---|---|---|---|
| 1 | Blank `assessment_inputs` ceiling | 2 min | Without it, v28's assessment fix is inert |
| 2 | Clear the `company_profile_section` tier | 2 min | Stops an extraction role losing its schema |
| 3 | Deactivate the stray `GEMINI` row | 2 min | Removes an arbitrary endpoint choice |
| 4 | Load a sector workbook | varies | Category F scores for your actual industries |

Items 1–3 are wrong on your install today regardless of version. Do them
before the first live run, or the run will not tell you what v28 changed.
