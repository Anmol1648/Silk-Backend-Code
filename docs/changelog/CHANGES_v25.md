# v25 — Stage 1 repaired, Stage 2 made deterministic

**702 tests pass.** No migrations; this is code and config only.

---

## The scoring pattern, stated plainly

You asked for the pattern to be clarified. Having read all 21 sheets, it is
**entirely algorithmic** — four steps, no judgement anywhere in the chain:

```
1. VALUE      one typed number or band per input key        ← the only LLM work
2. BAND       compare against the stage's cut-point
                 Higher-better:  ≥ Exc → Excellent, ≥ Good → Good, ≥ Fair → Fair, else Poor
                 Lower-better:   ≤ Exc → Excellent, …
                 Range:          inside [min,max] → Excellent, ±15% → Good, ±50% → Fair
                 Anchor:         the band whose written definition the evidence meets
                 Percentile:     ≥8 → Excellent, ≥6 → Good, ≥4 → Fair, else Poor
3. SCORE      Excellent 9 · Good 7 · Fair 5 · Poor 3        ← a lookup, nothing more
4. ROLL UP    average children → sub-item → category → weighted sum
                 blanks excluded from the denominator, weight redistributed
                 rating ladder: ≥8 Excellent, ≥7 Very Good, ≥6 Good, ≥5 Average, else Challenging
```

Every number in steps 2–4 now comes from the workbook rather than from Python.
The only genuine language task in the system is step 1: turning a website or a
PDF financial model into typed, sourced values. **Steps 2–4 are arithmetic**,
and a model there adds variance, not judgement.

## Stage 2 no longer calls a model

`run_rubric_review` sent the finished scorecard to an LLM and let it **re-band
parameters at high confidence**. That is the one operation scoring must never
do: the number moves while the audit trail still cites the rubric that no
longer produced it, and the same inputs can score differently on Tuesday than
on Monday.

Replaced by `fundos/assessment/review.py` — six deterministic checks that
**describe without re-scoring**: implausible values for a unit, reference rows
contradicting what they cross-check, categories resting on a couple of
answers, percentiles drawn from a sample below the workbook's floor, and
manual overrides surfaced rather than buried.

`llm_generate` calls in `fundos/assessment/tasks.py`: **1 → 0**. Asserted by a
test that patches `llm_generate` to raise and then runs scoring and review.
The only remaining model call in the assessment app is document extraction —
reading an uploaded PDF, which is Stage 1 work that happens to be triggered
from Stage 2.

---

## Stage 1 — four fixes, each against a specific line in your log

**1. The grounding gate measured the wrong thing.** It counted characters
against a floor of 400. Plum cleared it with 6,020 characters of marketing
website, and the deep extract then populated **229 of 231 fields** from it with
no search. A marketing page does not contain a founder's years in industry or a
peer's last raise, so those came from recollection and the gate passed them.

Now the test is **retrieval, not volume**: a search performed, a document
uploaded, or a research adapter that returned facts. A website-only run is
**degraded rather than accepted whole** — it may answer what a website can
evidence (company age, named CXO seats, patents, advisors) and is not asked
market or history questions at all. `mode` is reported as `full`,
`website_only` or `none`.

**2. Answers vanished uncounted.** The run reported 61 asked, 35 missing, 1
written — leaving **25 answers unaccounted for**. They hit a silent `continue`
when the key missed the numeric lookup, and no counter recorded it, which is
why the log could not say what went wrong. Anchors answered in `values` are
now **accepted rather than dropped** — where a model puts an answer is a
formatting detail — and two counters were added: `unknown_key_rejected` and
`unparseable_band`. Every asked parameter is now written, missing, or counted
in a rejection bucket.

**3. The sector vocabulary was unconstrained.** The model returned
`"Corporate Employee Health Benefits"` and `"Digital Healthcare /
Teleconsultation"` — good descriptions matching nothing, leaving category F
blank at 20% of the rating. It was never shown the 18 sectors and 33 groups it
was expected to choose from. That was our omission. Both lists are now passed
in context as a **closed vocabulary** with an explicit instruction to copy an
entry exactly.

**4. Spacing decided a 20%-weight lookup.** `"Health Tech"` failed against the
sheet's `"Healthtech"`. Matching now folds case and punctuation on both the
sector and sub-sector paths. An unknown label still resolves to nothing —
loosened matching must not become guessing, since a wrong group scores a fifth
of the rating against the wrong population.

---

## Still on your side

**Web search is still off.** All 74 LLM calls in the last log show
`searches=""`, and all nine research adapters report `SKIP — fixture adapter`.
Until that changes, every run is `website_only`, and v25 will now say so
plainly instead of producing a full-looking profile from one marketing page.

Admin → LLM → Config profiles → the advanced profile: **web_search ON,
structured_output OFF** (Gemini rejects both together).

Two smaller items also visible in the log and outside this tar ball: the CKB
transaction error still appears (4 times), and 22 `cp_extract.*` config
profiles do not exist, so every section falls back to the tier default and the
per-section tuning is not being applied anywhere.

---

## Deploying

No migrations. Same as v24 otherwise:

```bash
# deploy code, then:
python manage.py import_assessment_workbook "$WB" --config-version 2 --activate
python manage.py import_sector_benchmarks "$WB"
python manage.py seed_platform_config --reset-prompts
sudo systemctl restart fundos-web fundos-worker
```

Then fix web search before regenerating anything.

**What a healthy run now looks like:**

```
asked=61 mode="full" written=25-40 bands=8-15 benchmarks=6
unknown_key_rejected=0 unsourced_rejected=0-3 sub_sector_method="exact"
```

`mode="website_only"` with a low `asked` means search is still off. A non-zero
`unknown_key_rejected` means the prompt and the workbook have drifted — a
config fix, and now a visible one.
