# CHANGES — v29 (defect closure)

v28 fixed three real things and was built on a fourth that was not happening.
This release keeps the three, corrects the fourth, and closes the defects the
v28 audit did not reach because it stopped at the layer where the symptom
appeared.

---

## 0. The finding that reframes v27.5 and v28

`LLMConfigProfile.output_schema` is a `JSONField(default=dict)`. Nothing in
the codebase has ever written to it — not the seeder, not a migration, not
the adapter. Only a human editing Django admin could populate it.

`capabilities_payload()` therefore emitted `{"structured_output": {"schema":
{}}}`, and every provider path guards with `if so and so.get("schema")`. An
empty dict is falsy.

> **No `responseSchema` has ever been sent to Gemini. On any tier. For any
> role. For six releases.**

The 19 August measurement — schema present: 0 of 4 searched; schema absent:
4 of 4 — is **correct**. `tools/diagnose_gemini_search.py` builds its own
payload and sends a genuine schema. It is a true fact about the Gemini API
and an inapplicable one for FundOS, which was not sending one.

The production log settles it without reference to the diagnostic at all:

```
company_profile_deep_extract   caps_sent="thinking,web_search"   searches=0
company_profile_section        caps_sent="thinking,web_search"   searches=2 (×3)
```

Same config profile, same model, same thinking budget, same capabilities.
Opposite behaviour. The schema was absent from both, so it cannot be the
discriminator. The remaining variable is the prompt — which is H4, and which
neither v27.5 nor v28 addressed.

**Consequence for planning:** v28's headline objective (`searches > 0` on the
deep extract) was not achieved by v28, and could not have been. v29 does not
achieve it either. It removes the false explanation so the real one can be
measured.

---

## 1. `llm/adapter.py`

### 1.1 The repair call lost the role's output ceiling — **run 1's failure**

`_parse_json_with_repair` dispatched without `role`, so `_dispatch` resolved
`ROLE_MAX_OUTPUT_TOKENS.get("")` and fell to `DEFAULT_MAX_OUTPUT_TOKENS`
(2048). The roles that need repairing are the search-required ones — they get
no schema, so their JSON is obtained by instruction — and those are precisely
the roles designed for 8192.

```
19 Aug run 1:
  llm.call.assessment_inputs  FAIL
  JSONDecodeError: Expecting ',' delimiter: line 64 column 6 (char 4492)
```

4,492 characters is roughly 1,100 tokens of the ~2,048 a repair could emit
once its prompt was counted. The repair never had room to finish, and failed
for the same reason as the call it was repairing. `role` is now passed
through.

### 1.2 Truncated JSON was discarded instead of salvaged

Both 19 August assessment failures were truncation, not corruption: valid
JSON with the tail missing. Raising `JSONDecodeError` threw away every
extracted field to punish an unclosed brace, then paid for a repair call to
get them back.

New `_salvage_truncated_json` walks the text tracking string state and
bracket depth, truncates at the last complete element, and closes the open
brackets. It is tried **before** dispatching a repair, and again on the
repair's own output. Handles commas inside strings and escaped quotes.
Deliberately conservative: it never invents a value, so the worst case is
fewer fields, never wrong ones.

### 1.3 `structured_output` made real, or made honest

A blank `output_schema` is now filled from the role's contract via
`schemas.json_schema_for(role)`. A schema is a property of a ROLE;
`output_schema` sits on a PROFILE, and one tier profile serves eleven roles —
which is very likely why the field was never populated. It was
architecturally in the wrong place.

**Gated, because the naive version is actively dangerous.** `SCHEMAS` is a
*validation* contract, not a description of the whole response, and a
`responseSchema` is *prescriptive*:

```
SCHEMAS["company_profile_deep_extract"] = {"optional": {needsInput, missing, conflicts}}
the same call on 19 Aug:  populated_fields=235  total_fields=244
```

Deriving a schema there would have sent Gemini a three-property object and
reduced the entire dossier to three housekeeping keys — silently, with a
valid parse and a healthy log. Worse than the inert tick it was fixing.

Only roles in `schemas.SCHEMA_IS_COMPLETE_OUTPUT` get a derived schema. It
holds `readiness_summary` and `peer_insight`, each verified against the code
that reads it. Every other role logs an INFO line naming itself whenever the
tick has no wire effect, so the admin screen and the runtime stop disagreeing
in silence.

### 1.4 `SEARCH_BENEFICIAL_ROLES` contained two things that are not roles

`company_profile_consolidated` is a generation MODE; `market_research` is a
section key and a config-profile suffix. Neither is in `LLM_ROLES`, so
neither could ever equal `role` at a call site, and the no-search warning
they were added to raise could never fire. They read as coverage and provided
none.

Emptied, and `_validate_role_sets()` now checks every role set against
`LLM_ROLES` at import time.

### 1.5 A rejected schema no longer costs the whole call

Now that a derived schema can reach the wire, `_run_gemini` retries once
without it on a 400 mentioning schema or JSON — mirroring the existing
`thinkingConfig` fallback. The response is validated and repaired downstream
regardless, so the schema is an optimisation, never a requirement.

---

## 2. `llm/default_prompts.py`

### 2.1 Four dispatchable roles had no tier policy

`LLM_ROLES` has **33** entries. `ROLE_TIER_DEFAULTS` and `DEFAULT_PROMPTS`
have **29**. The four absent roles have no shipped prompt — their callers
pass `system` and `prompt` directly — and three are live:

| Role | Call site | Tier assigned | Reasoning |
|---|---|---|---|
| `assessment_extraction` | `assessment/extraction.py:279` | simple | Lifts answers out of documents already parsed and supplied |
| `investor_classification` | `investors/classify.py:69` | simple | Assigns a supplied record to a supplied vocabulary |
| `investor_identity` | `investors/identity.py:178` | simple | Candidates are retrieved by rapidfuzz **before** the call; promoting it to advanced would attach a search tool to an identity match and invite resolving names against the open web |
| `assessment_rubric_review` | — | judgment | Weighs an extracted answer set against a rubric and decides whether it holds |

This is the same defect v28 corrected for `valuation_explainer` and then
reproduced four times over, because its test used the wrong denominator
(§5.1).

### 2.2 An unknown role is now reported

`get_tier` logged nothing and returned `"simple"` via `.get(role, "simple")`.
An omission and a decision rendered identically — the exact thing the policy
table exists to prevent. The fallback is unchanged (a wrong tier beats a
failed generation) but now emits `LLM TIER POLICY GAP` naming the role.

---

## 3. `platformcfg/management/commands/seed_platform_config.py`

### 3.1 Endpoint adoption did not fire on the install it was written for

v28 ordered candidates by `("-is_active", "priority", "code")`. With `GEMINI`
and `GEMINI LLM` both active at priority 10, `"GEMINI"` sorts first,
`existing.code == code`, adoption was skipped, the duplicate survived — and
`endpoint_codes["GEMINI"]` stayed `"GEMINI"`, pointing freshly created tier
profiles at the row nothing uses while `_usable_endpoints` sent traffic to
the other.

Two dedupe rules in one file, disagreeing: `_usable_endpoints` weighs "is a
role binding already pointing here" and the adoption step did not. Both now
call `_preferred_endpoint_for_kind`, which applies that rule once.

### 3.2 The schema alignment was an invariant pretending to be a repair

```python
# v28
LLMConfigProfile.objects.filter(web_search=True, structured_output=True) \
    .update(structured_output=False)
```

No provider filter, no guard, run on every seed. Two problems:

* `tier_contract.py` states explicitly that the finding is Gemini-only and
  that there is "no evidence to justify degrading their output shape" for
  Anthropic and OpenAI. The seeder degraded them anyway.
* With no "only where unset" guard — unlike the `max_uses` repair three lines
  below, which has one — an administrator could never make the opposite
  choice stick. A repair that re-runs forever is a policy in the wrong layer,
  and it teaches operators to route around the seeder.

Now Gemini-scoped and recorded as one-time in the new `platform_flag` table.

### 3.3 Corrected a false causal claim

`_repair_capabilities` asserted Gemini "rejects" the combination and that the
call "fails outright". It does not — the measurement shows the call
succeeding and not searching, which is the opposite failure mode and the
reason it went unnoticed for six releases. `tier_contract.py` had it right;
this file contradicted it in the same release.

### 3.4 The active judgement row kept a quoted budget

v28 fixed `thinking_budget` on the two **inactive** vendor alternates and
left `tier.judgment` — the row that serves the tier — as `"4000"`. Moot
either way (`thinking_budget` is a `CharField`, and v27.5's `_budget_str()`
already coerced at render time) but corrected so the seed says what it means.

---

## 4. `strategy/services.py` — the other half of domain-agnosticism

`generate_peers` scores every company against `_FIXTURE_PEERS`: five
hardcoded companies, four SaaS and one Fintech. When nothing cleared the
Group A bar it fell back to `promoted = [0]` — the single top scorer promoted
into "Closest Comparables" **regardless of score**, with a rationale line
asserting it was the closest available comparable.

For a construction, electronics or logistics company that is
"Peer Alpha — Vertical SaaS" presented as the reference set.

This is precisely what the assessment path refuses to do. `resolve_sub_sector`
declines to fuzzy-match below a high floor on the stated grounds that putting
a construction firm on a D2C benchmark group "produces a confident number
against the wrong comparators, which is worse than a blank". Two halves of
one product, opposite philosophies, one of them audited.

Banding is now `group_peers_by_score` — pure, dependency-free, directly
testable. The **relative** promotion survives, because the complaint behind
it is real (Tester Issue 15: an absolute bar leaves Group A empty for genuine
near-misses). It is bounded by `PEER_PROMOTION_FLOOR = 45.0`, below which
Group A is left honestly empty with a log line explaining why.

### 4.1 This reverses a previously-accepted test — read before deploying

`tests/api/test_issue_report_2076_fixes.py::PeerGroupA::test_issue15_group_a_never_empty`
asserted **"Group A must never be empty"** (Tester Issue 15 / TC-049). Its
scenario is an **agritech** company against the SaaS fixture universe, so the
only way to satisfy it was to fabricate a comparable. The old contract and
the corrected one cannot both hold.

The test has been **split in two**, so that the half of Issue 15 that was
right keeps its protection and the half that was wrong is stated as its own
contract:

| Test | Asserts |
|---|---|
| `test_issue15_group_a_is_populated_for_an_in_universe_company` | A company INSIDE the peer universe still gets Closest Comparables when nothing clears the absolute >=60 bar. This is the real complaint behind Issue 15 and it still holds. |
| `test_issue15_does_not_fabricate_a_comparable_out_of_sector` | Nothing below the floor is ever presented as a Closest Comparable to an agritech company. |

**This is a product decision, not a bug fix, and it is yours to overturn.**
Set `strategy.services.PEER_PROMOTION_FLOOR = 0` to restore the pre-v29
behaviour exactly. The default is the safe reading: an empty Group A with a
logged reason is an honest answer, while a Vertical SaaS company presented to
an agritech founder as their closest comparable is a confident wrong one —
and it is the kind of wrong that gets quoted in an investor conversation.


---

## 5. Tests

### 5.1 The v28 coverage test could never fail

```python
missing = sorted(set(DEFAULT_PROMPTS) - set(ROLE_TIER_DEFAULTS))
```

Both are 29 entries maintained in the same file. The assertion was
tautologically empty, which is how four roles stayed invisible while the
release notes claimed full coverage. Re-keyed on `LLM_ROLES`, plus a reverse
check that the table names no role that does not exist.

### 5.2 New — `tests/api/test_v29_defect_closure.py`, 31 tests

Tier policy coverage; role sets naming real roles; the repair path keeping
its ceiling; seven truncation-salvage cases including commas inside strings
and escaped quotes; the deep extract being asserted *outside* the schema
allowlist; endpoint adoption agreeing with provider selection; peer banding
behaviour; seed idempotence.

Two peer tests were initially written as source-text scans (`assertNotIn
"promoted = [0]"`) and failed against the explanatory comment. That was the
right failure: a test that reads source text tests the comment, not the
behaviour. Replaced with behavioural assertions against
`group_peers_by_score`.

### 5.3 v28 suite grew 23 → 26

Added `test_the_schema_alignment_does_not_touch_other_providers` and
`test_an_administrator_may_re_enable_schema_after_the_alignment`. The second
is the one that matters: it asserts a repair cannot re-run forever and make a
setting unsettable.

---

## 6. Migration

`platformcfg/0004_platformflag` — a key/timestamp marker table so a **one-time
alignment** can be told apart from an **invariant re-applied every run**.
v28 conflated the two, which is what made §3.2 possible.

---

## 7. Deliberately NOT done

**The advanced tier still cannot express "search AND structured output."**
All four search-required roles declare a schema:

| Role | Tier | Declares schema | Gets one |
|---|---|---|---|
| `company_profile_deep_extract` | advanced | yes | no |
| `assessment_inputs` | advanced | yes | no |
| `peer_insight` | advanced | yes | no (dropped on Gemini) |
| `research_synthesis` | advanced | yes | no |

"Needs to search" and "needs a guaranteed shape" are independent axes; the
tier enum bundles them and forces a choice, and 100% of search roles are on
the losing side.

The correct fix is a two-phase split for `company_profile_deep_extract` and
`assessment_inputs`: an advanced retrieval pass (no schema, search on) whose
output becomes context for a simple structuring pass (schema on, cheap, no
search). That restores the stated rule rather than breaking it — retrieval is
retrieval, extraction is extraction.

It is **not** in v29 because it changes pipeline call counts and cannot be
validated without a live API key and a real run. §1.1 and §1.2 address the
failures actually observed; the split is the follow-up, and it should be done
after H4 is answered, since H4 may change what the retrieval pass needs to
look like.
