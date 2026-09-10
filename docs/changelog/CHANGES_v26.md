# v26 — why search never fired, and the call consolidation

**711 tests pass.** No migrations.

---

## First, two corrections to my own last report

**"74 LLM calls" was my error.** That was the total across all four runs in
the file — I grepped the whole log instead of per run. **A single run makes
14 calls.** Apologies: that sent you looking for a problem an order of
magnitude larger than the real one.

**Config was never the fault.** I suggested `structured_output` was the
blocker. It is not: the seeder already turns it off on `tier.advanced.gemini`
specifically because Gemini rejects `google_search` alongside a
`responseSchema`, and the log line `set max_uses, user_location,
structured_output` is that repair running. Verified in code — `assessment_inputs`
and `company_profile_deep_extract` both resolve to a profile whose
capabilities contain `web_search` and not `structured_output`.

---

## Why no search ever fired

`google_search` on Gemini is a **model-decided tool**. Attaching it grants
permission; it does not cause a search. So the deciding factor is what the
prompt tells the model — and the only sentence it ever received on the subject
was this, appended by the adapter's cost-control layer:

> SEARCH BUDGET: you may run AT MOST 6 web searches… **When the budget is
> spent, answer from what you have** and record anything still unresolved.

Read that holding 24,000 characters of supplied context. It states a ceiling
and explicitly permits answering from context. Nothing says answering from
context alone is the wrong outcome. The model complied — on every call, in
every run — while the tool was correctly attached, the response correctly
parsed and every layer reported healthy.

The role prompts did say "if you have a web-search tool, USE IT", but that
sentence arrived *before* a budget directive that read as a discouragement.

**Fixed.** Roles in `SEARCH_EXPECTED_ROLES` — those whose parameters cannot be
in the supplied material (market size, growth, peer funding, headcount) — now
receive an instruction that leads with the requirement and states the cap as a
bound on it:

> SEARCH FIRST — THIS IS NOT OPTIONAL… **Answering entirely from the supplied
> context is a FAILED response, even if every field is populated.**

Other roles keep the plain cap: pushing a role that *can* be answered from
supplied text into searching would spend the budget for nothing.

**And it can no longer fail silently.** A search-expected role that performs no
search now logs a WARNING naming the role and the resolved profile. Previously
this produced `searches=""` inside a line marked `status="OK"` — the most
expensive silent failure in the pipeline.

---

## Call consolidation

Of the 14 calls, **eleven re-sent the same ~9,300-character bundle**:

| Role | Calls | Completion tokens | Prompt tokens each |
|---|---|---|---|
| `company_profile_records` | 4 | 56, 74, 115, 194 | 2,791 |
| `company_profile_structured` | 3 | 242, 283, 818 | 2,864 |
| `company_profile_section` | 3 | 343, 490, 563 | ~2,755 |

Roughly **31,000 prompt tokens spent re-reading one 6KB website** to produce
about 3,200 tokens of output — for data the deep extract had already read the
sources for.

The consolidated path was running and is default-ON. It skips a section only
when the deep extract **claims** it, and the deep extract did not return these
blocks at all. So the prompt now returns five more —
`products_and_services`, `customers_and_markets`, `competitive_advantages`,
`metrics`, `news` — and the writer stores them before claiming the section.

**Order matters here and is asserted by test:** claiming a section without
writing it would leave it empty, which is worse than the extra call it saves.
An empty block is not claimed, so that section still falls through to its own
call and nothing is silently dropped.

Expected: **14 calls → ~4**, prompt tokens down roughly 70%.

The second-order benefit is larger than the cost saving. Those ten calls ran on
`tier.simple`, which has **no web search**. Folded into the deep extract they
run on `tier.advanced` — so consolidation and the search fix land on the same
calls.

---

## Deploying

No migrations.

```bash
# deploy code, then:
python manage.py seed_platform_config --reset-prompts   # REQUIRED
sudo systemctl restart fundos-web fundos-worker
```

`--reset-prompts` is not optional this time: the fix is largely *in the
prompts*, and a `PromptTemplate` row overrides the shipped default entirely.

**What a healthy run now looks like:**

```
llm.call.company_profile_deep_extract  searches="3"   ← non-empty is the check
llm.call.assessment_inputs             searches="4"
total llm calls: ~4 (was 14)
asked=61 mode="full" written=25-40 benchmarks=6 sub_sector_method="exact"
```

If `searches` is still empty, the new WARNING will name the role and the
resolved profile, which is the first thing to send me.

---

## Still outstanding, and outside this tar ball

* The nine research adapters remain fixtures. With search working the deep
  extract can cover much of what they were meant to supply, but `research`
  payloads stay empty until they are pointed at live feeds or switched off.
* The CKB transaction error still appears in the log (4×).
* 22 `cp_extract.*` config profiles do not exist. Harmless — the fallback is
  correct and now logged — but the consolidation removes most of those call
  sites anyway.
