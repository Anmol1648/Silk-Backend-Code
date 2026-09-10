# ADMIN CONFIGURATION — v29

Supersedes ADMIN_CONFIG_v28.md. Two of that document's four items were
correct actions supported by incorrect reasoning; one has been fixed in code
and no longer needs you; one was wrong about a different part of the system.
The corrections are stated inline rather than quietly dropped, because the
reasoning is what you will use next time.

**Three actions. All three take under ten minutes. None requires a deploy.**

---

## Read this first — the finding that changes the v28 advice

`LLMConfigProfile.output_schema` is a JSON field that defaults to `{}`, and
until v29 **nothing in the codebase ever wrote to it** — not the seeder, not
a migration, not the adapter. Only a human editing Django admin could fill
it, and nobody had.

Every provider path guards with `if schema:`. An empty dict is falsy. So:

> **No `responseSchema` was ever sent to Gemini. On any tier. For any role.
> For six releases.** The `structured_output` tick did nothing at all.

This matters because v27.5 and v28 were both built on the measurement that a
`responseSchema` suppresses `google_search`. **That measurement is correct** —
`tools/diagnose_gemini_search.py` sends a real schema, and 0 of 4
configurations searched with one against 4 of 4 without. But it cannot
explain anything in your 19 August logs, because FundOS was not sending one.

Your own log proves it independently:

| Role | Tier | `caps_sent` | Searches |
|---|---|---|---|
| `company_profile_deep_extract` | advanced | `thinking,web_search` | **0** |
| `company_profile_section` | advanced | `thinking,web_search` | **2, 2, 2** |

Same profile, same model, same capabilities, same thinking budget. Opposite
behaviour. The schema was never the variable separating them — it was absent
from both. **The remaining variable is the prompt**, which is what H4 tests.

v29 makes the tick real where it is safe to do so, and makes it say so out
loud where it is not. See DEPLOYMENT_v29.md §6.

---

## 1. Blank the `assessment_inputs` output ceiling — HIGH

**Where:** Admin → LLM Role Bindings → `assessment_inputs` → **Max output
tokens** → clear the field (leave empty) → Save. Do the same for
`company_profile_deep_extract`.

**Unchanged from v28, and still the single highest-value action.** The
adapter resolves the ceiling as:

```python
role_cap   = ROLE_MAX_OUTPUT_TOKENS.get(role) or DEFAULT_MAX_OUTPUT_TOKENS
max_tokens = min(binding_value, role_cap) if binding_value else role_cap
```

A number in this field can only ever **lower** the role's designed budget.
`assessment_inputs` is designed for 8,192 to fix the truncation that killed
it on both 19 August runs:

```
run 1  JSONDecodeError at char 4492 after 43.2s     whole assessment lost
run 2  finish_reason=MAX_TOKENS, thinking=1945,
       completion_tokens=0, was_repaired=true       7 of 30 fields survived
```

If an administrator has typed a number here, `min()` keeps it and the fix
does nothing. The help text — *"Leave EMPTY to use the role's designed output
budget"* — is load-bearing.

**Check it even if you believe it is empty.** This is still the most likely
reason for a release to look like it did not work.

---

## 2. Deactivate the stray `GEMINI` endpoint — HIGH, **and do it BEFORE re-seeding**

**Where:** Admin → LLM Endpoints → the Gemini row that is **not** the one
your role bindings point at → untick **Is active** → Save.

**The ordering correction.** DEPLOYMENT_v28.md told you to re-seed at step 3
and expect the message `gemini already served by 'GEMINI LLM' — not creating
'GEMINI'`, then deactivate the stray afterwards in ADMIN_CONFIG §3. On your
install that message would never have appeared, and the duplicate would have
survived the deploy.

v28's adoption logic sorted candidate rows by `("-is_active", "priority",
"code")`. With `GEMINI` and `GEMINI LLM` both active at priority 10, `"GEMINI"`
sorts first on `code`, so `existing.code == code`, adoption was skipped
entirely — and `endpoint_codes["GEMINI"]` stayed `"GEMINI"`, pointing freshly
created tier profiles at the row nothing uses while traffic went to the other
one. Two dedupe rules in one file, disagreeing: `_usable_endpoints` weighs
"is a role binding already pointing here", and the adoption step did not.

**v29 fixes the cause** — both paths now call one helper,
`_preferred_endpoint_for_kind`, which prefers the row your bindings already
use. But the seeder still will not delete a row it did not create, so a stray
already in your database needs deactivating by hand, once.

Keep `GEMINI LLM`: it is the row your bindings use and the row the 19 August
logs show serving every call.

**Also check** the model string on the row you keep. Preflight reported
`gemini-3.6-flash` while the tier seeds specify `gemini-3.5-flash`. Both are
priced ($7.50 vs $9.00 per M output), so nothing zeroes out — but settle on
one model before comparing costs between runs.

---

## 3. Clear the `company_profile_section` tier override — MEDIUM

**Where:** Admin → AI Prompts → `company_profile_section` → **Tier** → clear
the field (leave blank) → Save.

**Still the right action. Two of v28's three reasons were wrong.**

`diagnose_config` reports:

```
Prompt tier overrides   WARN   company_profile_section: simple -> advanced
```

The shipped policy puts this role on `simple` and the shipped policy is
right: it writes profile sections from material the deep extract already
retrieved. It is extraction. Under your own rule it belongs on simple.

What v28 said, and what is actually true:

| v28 claim | Status |
|---|---|
| "It loses its structured output… degrading correctness" | **False.** There was no schema to lose — see the finding above. |
| "It funds a thinking budget on every call (~4,600 tokens)" | **True on 19 Aug, already fixed in code.** The role is in `NO_THINKING_ROLES` and not in `THINKING_JUSTIFIED_ROLES`, so since v27.5 the adapter strips thinking and sets `budget_tokens: 0` on Gemini — even if you leave the override in place. The ₹6.45 comes back from the deploy, not from you. |
| "It attaches a search tool the role has no use for" | **True, and now the only reason.** |

So: do it for tier coherence and to stop attaching a search tool to an
extraction role, but do not expect a correctness change or the cost saving —
you get the cost saving either way.

**The v28 correction that still stands:** an earlier version of this advice
said to *keep* the override because it was the only thing in the system that
searched. That was wrong. The right fix is to get the deep extract searching,
which is what H4 is for — not to leave an extraction role on a research tier
as a workaround.

---

## 4. Sector and peer data — NOT what v28 said

**Where:** `python manage.py import_sector_benchmarks <workbook.xlsx>`

v28's analysis of the **assessment** path was correct and is unchanged:
`weighted_rollup` averages over scored children only, redistributes a blank
category's weight across the rest, and surfaces each one's `applied_weight`.
A company with no benchmark group is scored on the four categories that did
resolve, each carrying 25% instead of 20%, and the response says so. The
rating is not wrong — it is computed over less, and it reports that. The
resolver deliberately refuses to fuzzy-match below a high floor, because
assigning a construction firm to a D2C benchmark group produces a confident
number against the wrong comparators.

**But the peer universe did the exact opposite, and v28 did not look.**

`fundos/strategy/services.py` scores every company against `_FIXTURE_PEERS` —
five hardcoded companies, four SaaS and one Fintech. When no candidate
cleared the Group A bar, the code fell back to promoting the single top
scorer regardless of its score, with a rationale line asserting it was the
closest available comparable. For a construction, electronics or logistics
company that put **"Peer Alpha — Vertical SaaS" in Closest Comparables**.

That is precisely the outcome ADMIN_CONFIG_v28 §4 congratulates the system
for avoiding. Two halves of one product, opposite philosophies, one of them
audited.

**v29 fixes it in code.** `group_peers_by_score` now applies a floor: genuine
near-misses are still promoted (the original complaint behind that fallback
is real), and below the floor Group A is left honestly empty with a log line
explaining why. Nothing for you to configure.

**Still true, and still the real action for benchmarks:** load a workbook
covering the sectors you actually invest in. Adding rows one sector at a time
as they appear is how the gap stays permanently one step behind the
portfolio. Nothing needs to change in code for a new industry.

---

## What NOT to change

**Do not seed the `cp_extract.*` config profiles.** Nine are missing and
falling back to the tier default, which produces eighteen INFO lines a run
and is harmless. Creating them means nine more rows to keep coherent with the
tier contract, for no behavioural gain.

**Do not enable more research sources expecting more data.** The toggles in
Admin → Research Sources work, and every one of the nine adapters behind them
returns an empty list pending a feed that was never ratified.
`collect_sources` reports them honestly as `SKIP`. Turning them on changes
nothing; the fix is code, not configuration.

**Do not tick `structured_output` expecting a schema.** For most roles it is
still deliberately inert — see DEPLOYMENT_v29.md §6 for which roles send one
and why the rest do not. v29 logs an INFO line naming the role whenever the
tick has no wire effect, so the admin screen and the runtime no longer
disagree in silence.

---

## Order

| | Item | Time | Effect |
|---|---|---|---|
| 1 | Blank `assessment_inputs` + `deep_extract` ceilings | 2 min | Without it, the assessment fix is inert |
| 2 | Deactivate the stray `GEMINI` row — **before re-seeding** | 2 min | Lets adoption fire; removes an arbitrary endpoint choice |
| 3 | Clear the `company_profile_section` tier | 2 min | Stops a search tool on an extraction role |
| 4 | Load a sector workbook | varies | Category F scores for your actual industries |

Items 1–3 are wrong on your install today regardless of version. Do them
before the first live run, or the run will not tell you what changed.
