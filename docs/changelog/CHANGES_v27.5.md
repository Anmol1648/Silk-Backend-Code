# FundOS backend v27.5 — acting on what was measured

**Six files changed, one migration recovered. No API contract change.**

v27.4 shipped the instrumentation. On 19 August it was used, and the results
overturned two things this codebase believed — one of them a change v27.4 made
on my recommendation.

Everything below is a response to a line in `silk_generation_19AUG26.log` or
`LLM_LOGS_1.txt`. Nothing here is a response to an argument.

---

## 1. H1 is settled: `responseSchema` suppresses `google_search`

`tools/diagnose_gemini_search.py`, gemini-3.5-flash, 8 cells, reading
`groundingMetadata.webSearchQueries`:

| schema | thinking | directive | searches |
|---|---|---|---|
| True | 0 | before | unknown |
| True | 0 | after | unknown |
| True | 2048 | before | unknown |
| True | 2048 | after | unknown |
| False | 0 | before | **4** |
| False | 0 | after | **4** |
| False | 2048 | before | **3** |
| False | 2048 | after | **6** |

**H1 decisive — 0% with a schema, 100% without. H2 no effect. H3 no effect.**

Two halves of this repo asserted opposite answers to this question for six
releases: `diagnose_config` called the combination rejected, the adapter's
warning told operators it was supported. Both were written from reasoning.

The grid is now written into `TIER_CONTRACT` with its date and model string,
and the advanced tier carries `structured_output: False`. A new
`_apply_structured_output()` drops the schema on Gemini and logs why.

Scoped to Gemini, because that is where it was measured. Anthropic and OpenAI
have no equivalent interaction and there is no evidence for degrading their
output shape on a Gemini finding.

> **Watch this on the first live run.** The deep extract's 244 fields now come
> back by instruction plus the existing repair path rather than by schema.
> That path handled the one truncation it saw cleanly, but this is the first
> release where it carries the load. Treat a fall from 235/244 populated
> fields, or `was_repaired` firing on most calls, as a signal to revisit.

## 2. The v27.4 thinking rule was wrong, and it cost real money

v27.4 preserved the thinking budget on every search-required role, reasoning
that a model-decided tool needs deliberation to be chosen. **H2 says that
reasoning was wrong** — without a schema the model searched at budget 0 as
readily as at 2048.

What the rule bought in production:

| | 17 Aug (v27.2) | 19 Aug (v27.4) |
|---|---|---|
| Thinking tokens | 6,266 | 11,509 / 15,075 |
| Cost | ~₹17.5 | ₹21.14 / ₹21.02 |
| Latency | 92s | 150s / 146s |
| `assessment_inputs` | ran | **failed / truncated** |

`NO_THINKING_ROLES` is now excepted by `THINKING_JUSTIFIED_ROLES` —
`{company_profile_judgment, peer_insight, research_synthesis}` — roles that
weigh evidence and reach a conclusion. Deliberately small. Attaching a tool is
no longer a reason to fund thinking, because it never was one.

## 3. `assessment_inputs` had no output ceiling

```
run 1  JSONDecodeError at char 4492, 43.2s        whole assessment lost
run 2  finish_reason=MAX_TOKENS thinking=1945
       completion_tokens=0 was_repaired=true      7 of 30 fields survived
```

The role was absent from `ROLE_MAX_OUTPUT_TOKENS` and inherited the 2,048
default, which does not hold 16–30 parameters with values, confidences and
citations — and held nothing at all once a thinking budget was attached.
Now `8192`, matching the deep extract's comparable output volume.

## 4. C-05 — the transaction abort, found and closed

```
PROFILE: financials→CKB sync failed for financial_summary: An error occurred
in the current transaction. You can't execute queries until the end of the
'atomic' block.
```

Present on every run since v27.1, and chased in the wrong place each time.

It is Django's `TransactionManagementError`, raised when
`connection.needs_rollback` is set — which happens when a statement fails
inside an atomic block and the exception is **caught** rather than allowed to
unwind. Django then refuses every subsequent query until the block exits.

**The sync was never the fault.** It was the next thing to touch the database.

v27.3 correctly savepointed the funding and investor writes and the error
persisted, because an AST scan for the pattern finds **five more sites** —
including `profile.save(update_fields=["judgment"])`, which runs immediately
before the sync and matches the log timestamp to the second. All five now use
a shared `_isolated_write()` helper, and
`test_no_swallowing_write_remains_without_a_savepoint` re-runs the scan so a
sixth cannot be added quietly.

## 5. Accounting holes the 19 Aug run exposed

**A billed failure recorded as free.** `run.economics.role
role="assessment_inputs" prompt_tokens=0 cost_inr=0.0 latency_ms=43218` —
43 seconds of inference the provider generated in full and billed for, which
we simply could not parse. `usage` is now seeded before dispatch so the
failure handler can see what was spent, and the FAIL trace line carries
tokens, `caps_sent`, `thinking_budget`, `finish_reason` and a `billed` flag
distinguishing a billed parse failure from a free connection refusal.

**A truncated call under-reporting its output.** `completion_tokens=0
response_chars=485`. Gemini stops counting candidate tokens at the ceiling but
bills for what it produced. `_completion_from()` estimates from the text when
the provider reports zero *and* text came back — a genuinely empty response
still reports zero.

**`searches_unknown` too generous.** Run 2 read `searches=6
searches_unknown=8` when the truth was six searches across three calls and
none at all across the other eight. `company_profile_section` returned
`webSearchQueries` on every call that searched, so on Gemini its absence is
evidence of zero, not absence of evidence. `searches_unknown` now means only
what it was built for: the tool was never attached, so there was nothing to
decline.

## 6. The `search_expected` flag was measurably backwards

19 Aug read `search_expected=false` on `company_profile_section` — the only
role in the system that searched, twice in one run and six times in the next —
and `true` on the two roles that never did. An admin tier override had put
section on `advanced`, which handed it the tool without adding it to any role
list. **It was paid for and never told about.**

The directive and the tail reminder now key off the capability actually
attached rather than a parallel role list. The trace reports both
`search_expected` (what was sent) and `role_expects_search` (what was
declared), so a disagreement is legible instead of hidden behind one boolean.

## 7. The missing migration

`manage.py migrate` reported unapplied changes in `llm` on deployment and
`0013_alter_llmconfigprofile_max_uses` had to be generated on the production
host. The model change landed in v27.3 without its file. It is committed here,
and `test_the_migration_state_is_clean` asserts `makemigrations --check` stays
clean.

## 8. Trace hygiene

`thinking_budget=2048` on one line and `thinking_budget="4000"` on the next,
because the value arrived as whichever type an administrator had typed and the
formatter quotes strings. `_budget_str()` renders a bare int or the literal
`unset`.

---

## H4 — the result H1 does not explain

`company_profile_deep_extract` sent **no** responseSchema on 19 Aug — its
`caps_sent` read `thinking,web_search` — and still searched zero times, while
`company_profile_section`, with identical capabilities, searched 2 and then 6.
Context size is ruled out: run 2's deep extract had a *smaller* context (3,299
chars) than the section calls that searched (5,945).

The remaining difference is the system prompt — 592 chars against 60–79. The
hypothesis is that "populate every field of this structure from the supplied
material" reads as an **extraction** task, and an extraction task is one you
complete from the material you were given, whatever the search directive says.

`diagnose_gemini_search.py` now varies framing as H4 (16 cells; `--no-framing`
runs the original 8). **This matters before the next cycle:** if H4 is real,
dropping the schema will not restore search on the role that most needs it,
and the time would be spent re-testing a variable already ruled out.

---

## Test results

```
Ran 270 tests   LLM, generation, grounding, cost, v27.3/4/5   OK (1 skipped)
Ran 205 tests   config, flows, exports, contract, defects     OK
Ran 199 tests   backfill, QA, benchmarks, engines, isolation  OK
------------------------------------------------------------------------
      674 tests                                               0 failures
```

`test_v23_phase3_phase4` and `test_v24_workbook_config` remain at 6 failures /
3 errors / 54 skipped — identical on the untouched v27.4 tarball, verified by
running both trees. They need a sector workbook fixture that is not in the repo.

**Added:** `tests/api/test_v27_5_measured_fixes.py` (38 tests). Every test
quotes the log line that motivated it.

**Changed:** `test_capabilities_payload_drops_unsupported` asserted that an
advanced-tier Gemini profile keeps both `web_search` and `structured_output`.
Measurement says they are mutually exclusive. Split in two, with the
provider-filtering behaviour it originally covered re-asserted on the simple
tier, where there is no search tool for a schema to suppress.

---

## Still not fixed

**C-03 fan-out.** `llm.context.fanout_trim` now shows the shape of it: 80.6%
of the corpus dropped on each of seven follow-up calls, and
`tokens_in_per_token_out=58.8` on `company_profile_records`. The root fix is
widening the deep-extract schema, which needs live validation. Untouched
deliberately.

**D-01 — nine fixture research adapters.** Unchanged, and still the largest
single constraint on output quality. Two clean runs, 14 sections, 235/244
fields — all from one homepage.

**D-02 — `SectorMapping` has no 'Eyewear' row.** One admin row; Category F
still scores blank for ClearDekho.

**X-01 — frontend skew.** No frontend tree has been supplied with any tarball.
`_abandon_ungrounded_run` now fires on additional paths, so confirming the
client renders an abandoned run as terminal rather than spinning has become
more important.

**Duplicate GEMINI endpoint.** `ignoring duplicate endpoint(s) GEMINI` on
startup. Deactivate the unused row before a fallback sends one vendor's model
string to another's API.

---

## Verifying this release

```bash
python manage.py migrate                   # 0013 applies cleanly
python manage.py check
python manage.py test tests.api.test_v27_5_measured_fixes
python manage.py diagnose_config

python tools/diagnose_gemini_search.py --model gemini-3.5-flash   # 16 cells
```

Then one live generation, and grep:

```bash
grep -E 'run.economics|sources.compression|fanout_trim' var/logs/silk_generation.log
```

Pass conditions on `run.economics`: `searches > 0`, `searches_unknown = 0`,
`cost_inr` non-zero, `concerns="none"`. **Success is `searches > 0`, not the
absence of a warning** — that distinction is what six releases of
healthy-looking logs got wrong.

And confirm `assessment_inputs` completes without `finish_reason=MAX_TOKENS`,
and that `financials→CKB sync failed` is gone.
