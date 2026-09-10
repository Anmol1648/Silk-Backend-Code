# CHANGES — Company Profile data integrity

Read from a real generated profile (Zyla Health, run of 21 Aug 2026) rather
than from the code. Every defect below shipped a **plausible** value — a number
in the wrong unit, a citation that resolves today, a founder record that looks
merely thin — which is the class this system is least equipped to notice on its
own. A refused run announces itself. A profile that is quietly wrong does not.

Also: research concurrency 3 → 5.

---

## 0. What the profile showed

| Field | Stored | Should have been |
|---|---|---|
| `financial_summary[].revenue_m` | `8.6` (INR crore, per its own observations) | USD millions, or INR with `currency` set |
| `financial_summary[].pat_m` / `currency` | absent on all 11 rows | requested by the schema on every row |
| `company_profile.sub_sector` | five comma-separated terms | one term |
| `news[].link` | `vertexaisearch.cloud.google.com/grounding-api-redirect/…` | the publisher's own URL |
| `competitors[].revenue` | `""` beside `28.5` beside `null` | `number \| null` |
| `competitors[].description` / `website` | `""` on all six | emitted and writable since CR-12, never asked for |
| `founders[0..1].background` | `""` (the two actual principals) | researched, like everyone else |
| `revenue_model` | emptied 51 minutes after generation, no record | either data, or a signal |

Nothing on the run record indicated any of it.

---

## 1. Markdown was reaching the exports

A model told to return JSON still writes prose the way it writes prose:
`"**Zyla Health** is India's *highest-rated* care management platform"`. Those
strings are rendered into PDF, PPTX and DOCX, none of which interpret markdown,
so emphasis added for a human reader becomes literal punctuation in an
investor-facing document.

`fundos/profile/sanitize.py` is the new boundary. Everything the synthesis call
returns passes through it before storage: markup stripped, control characters
dropped, strings and containers bounded.

Stripping is deliberately conservative. The failure that matters is not a
surviving asterisk — it is a **mangled figure**, because a damaged number is
indistinguishable from a researched one and there is no second copy to check it
against. Every rule requires a well-formed pair with non-space content inside
it, so `EBITDA * 2` and `gross_margin_pct` come through untouched. A
`[label](url)` becomes `label (url)` rather than bare `label`: the URL is
evidence, and discarding it throws away the only citation the model gave.

## 2. Coercion is driven by the field's own spec

`sanitize.coerce_to_spec` reads the field's specification string — the same text
`schema_prompt_block` renders into the prompt — and coerces against it. A field
added in Django admin is therefore type-checked on the way back with no second
declaration to maintain, which is the property the schema module already
guaranteed for the prompt and the normalizer and did not have for types.

- `number|null` → a number or `None`. Never `""`, never a numeric string. This
  is the competitors defect: three spellings of "absent" across two columns of
  the same six rows, so a client doing arithmetic gets `NaN` on some of them.
- `one of: …` → snapped onto a permitted value, or cleared. No fuzzy matching:
  these fields drive bucket selection and benchmark lookup, where a near-miss is
  worse than a blank.
- anything describing a URL → validated (§4).
- `sub_sector` / `macro_sector` → narrowed to one term (§3).

**A field the model omitted stays omitted.** Materialising every declared field
as an empty value looks helpful and lies twice: a row of empty strings is not
distinguishable from one the model deliberately left blank, and
`_replace_people` reads a *missing* `is_founder` as `True` — so filling it in as
`False` would file every unflagged person as a key person and empty the section
a reader checks for the team. Clients get field structure from the GET-path row
templates, which is the layer that owes them a shape to render. (Two existing
tests caught this; the tests were right.)

## 3. A list in a scalar field cost the scorecard, silently

`sub_sector` came back as `"Telemedicine, Patient Engagement, Medical AI,
InsurTech, Personalized Care Management"`. Every lookup stage in
`resolve_sub_sector` — exact, clubbed group, name, then fuzzy at a ≥88 floor —
misses a five-term concatenation. It returned `(None, "unresolved")`, which is
**correct**: its docstring argues, persuasively, that assigning a company to the
wrong peer group is worse than leaving category F blank.

But correct is not the same as fine. Category F is then dropped from the
denominator and its weight redistributed, and nothing anywhere said so. Two
changes:

- the prompt now states why the field is scalar and what a list costs;
- the normalizer narrows to the first term and **records what it discarded**;
- `orchestrator._check_benchmark_match` resolves the stored value after every
  run and, on a miss, writes a plain-language gap onto the section's
  `missing_notes` and the activity log.

A thirty-second fix by the founder, instead of a fifth of the scorecard quietly
going missing.

## 4. Citations that expire

Every `news[].link` on the live profile was a Gemini grounding redirect. Those
expire in roughly a month, so what is stored will be dead links in the Document
Center and in any exported IM or teaser — worse than no link, because it looks
checkable.

`sanitize.clean_url` rejects them by host and returns the reason. The publisher
name in `source` is the durable citation and survives. Both prompts now ask for
the publisher's own URL and explain why.

The same validator closes a real hole on a path that had none:
`logo.resolve_logo` stored a caller-supplied URL **verbatim** (`return
url[:1024]`) into a column the frontend renders as `<img src>` and the exporters
embed in a PDF. `javascript:` and `data:text/html` execute in the viewer's
origin — every teammate who opens the company afterwards is a target, not just
the submitter — and a private-network URL turns the field into an SSRF trigger
for the server-side exporters. Now: http(s) only, resolvable public host,
length-bounded, rejection logged with its reason.

## 5. A forecast was being valued as revenue

`_sync_financials_to_ckb` chose "the most recent fiscal year that has a
revenue". On eleven rows running to FY30, four flagged `is_estimate`, that
selected the **FY30 projection** — so a founder saving the Financial Summary
form pushed a forecast into the field the valuation engine reads.

- Actuals only. `_is_estimate_row` reads the explicit flag, then falls back to
  how the year is labelled (`FY27E`, `FY 2028 (P)`, `FY29 projected`) — a row
  that says so in its own text while carrying no flag is still a projection.
- An all-estimates table syncs **nothing**, and says so at WARNING. Silence is
  what made the old behaviour invisible: the CKB simply held a number.
- The currency travels with the figure. Every money field in this system is a
  triple `(value, ccy, basis)` precisely so a number cannot be read in the wrong
  denomination, and this path was writing the value alone — which is how INR
  crore reached a field whose readers assume USD.
- `_fy_key` normalises the year. `"".join(digits)` read `FY24` as 24 and
  `FY 2024` as 2024, so one row written the long way sorted above every other
  year — and the function picks the *last* row. Mixed conventions in one table
  are normal: a model exports "FY 2024", a founder types "FY24".
- Each field syncs in its own savepoint, so one value the CKB's numeric rules
  reject no longer costs the fields after it in iteration order.

### 5.1 PAT was being stored as a growth rate

`_fin_row_to_spec` did `pick("growth_pct", "pat", "grossMarginPct")`. A
profit-after-tax landed in a percentage column: a loss of −6.32 crore was stored
and rendered as **−6.32% YoY growth**. A plausible number, in the right shape,
describing something else entirely.

Falling back across units is never a recovery. The fallback is gone, and
`pat_m`, `currency`, `is_estimate`, `gross_margin_pct` and
`ev_revenue_multiple` are all carried through — the schema asked for them on
every run and this function discarded them.

`STRUCTURED_FORMS["financial_summary"]` gains `pat`, `isEstimate` and
`growthPct`, because the form validator is a **filter**: a key it does not know
is dropped on save. The fields were being requested at one end and discarded at
the other. `growthPct` gets `_growth_pct`, not `_pct`: a company can grow 300%
in a year and one in this database did, and validating growth as a 0–100 share
rejects the true figure while accepting only an understated one.

`fiscalYear`'s `max_len` goes 9 → 16, so `"FY 2024-25"` stops 422-ing.

## 6. A founder's name is a research lead, not a finished record

The write path skipped any row whose name a human had claimed. That protected
the human's data by **discarding everything researched about them**, so on the
live profile the two actual principals carried a name, a LinkedIn URL and empty
`role` and `background`, while a part-time co-founder — not named at onboarding,
so not suppressed — carried a full researched paragraph.

The dossier header was telling the model the same thing: *"do not infer anything
beyond what is written here"*. It obeyed.

Both ends now say the opposite:

- the header presents supplied founders as **research leads** — research them as
  thoroughly as anyone discovered independently, return them fully populated,
  correct a title the evidence contradicts, and include everyone the sources
  show. The unverified label stays, because a typed-in name is not evidence of
  anything; it now constrains *trust*, not *effort*;
- `_enrich_row` merges an AI row into a claimed human row: it fills fields that
  are blank, refreshes fields a previous AI pass supplied, and never touches a
  field a human actually filled.

Per-field provenance is recorded in `source_ref` (`ai_filled:designation,…`),
because provenance is per row and this is per field. Without it, the first AI
value written into a blank `background` would be indistinguishable from
something the founder typed, and every later run would decline to refresh it —
the enrichment would work exactly once and then freeze.

`_replace_people` now routes by where a human filed someone, not by
`is_founder`. A person the founder entered as a Founder whom the research
reports as a non-founder executive would otherwise be enriched in neither table
— the founders pass sees a name it does not have, the key-people pass creates a
second record — and the profile would show the same person twice with different
detail.

`_as_bool` takes an explicit `default`. A missing boolean and a false one are
different facts, and `bool(None)` collapsing to `False` is what would invert the
documented "an unflagged person counts as a founder" rule.

## 7. Structured sections get the rule the record tables always had

"An AI write replaces only its own previous output" lived in
`_replace_entity_rows` and covered the record tables only. Sections stored in
`ProfileSection.structured` had no equivalent, so a full pipeline run silently
overwrote a founder's hand-edited Business Model or Financial Summary.
`regenerate_section` guards this with a confirm prompt; the full run had no such
gate and simply wrote through.

`_merge_structured_for_ai`: **scalars merge, arrays do not.** A blank field is a
gap the research should close; an array a human curated is authoritative *as a
whole*, because its meaning is in which rows are present and in what order, and
merging by index would interleave two people's lists into one neither intended.

And when a person empties a populated section, `missing_notes` records it. The
history snapshot already made the old value recoverable — what was missing was
the *signal*, because a section reading empty is otherwise indistinguishable
from one the research looked at and found nothing for, and those call for
opposite responses.

## 8. Corrections are reported, not just made

A sanitizer that silently improves its input is indistinguishable from one that
silently damages it. Every correction the boundary makes is collected, counted,
carried onto the run's `progress`, and the first twelve are narrated into the
activity log.

These are the cheapest quality signal the pipeline produces and it used to throw
them away. Each entry is a place where the model's output and the contract
disagreed; a recurring one is a prompt or field-spec problem, not a model
problem. Without this, the only way to notice a prompt had drifted was for
someone to read a finished document and spot it.

## 9. Concurrency 3 → 5

The ten research batches are independent and network-bound, so this is the one
lever that shortens a run without changing what it costs — Gemini bills search
grounding per request, not per second. The live run took ~11 minutes with the
research stage dominating.

Not 10. The ceiling worth respecting is the provider's per-minute quota, not the
batch count: at ten the whole bank lands in one burst and a 429 costs a retry on
every batch at once, which is slower than not having fanned out.

The migration moves **only rows still holding exactly the previous shipped
default**, because it cannot distinguish "3 because nobody touched it" from "3
because an operator chose it" — the two are the same integer. Any other value is
left alone, and it is reversible. An operator who wants 3 sets it again and no
later migration compares against it.

Unchanged on SQLite, which still clamps to 1: concurrency here is a latency
optimisation, never a correctness requirement, and nine of ten batches were lost
to `database table is locked` before the clamp existed.

## 10. Prompt changes

Both prompts, in `default_prompts.py` — so an install with seeded
`PromptTemplate` rows needs `seed_platform_config --reset-prompts` to pick them
up, or the same edits in admin.

**`profile_synthesis`** — plain text only inside JSON strings, with the reason;
one value in a scalar field, with what a list costs; never mix denominations
inside a section, and put the ISO code in `currency`; `is_estimate` on any
projected year; publisher URLs not redirects; the Founders section lists
everyone fully populated and header names are leads; and **conflicts must be
named in `observations`**.

That last one is not new policy — the prompt already asked for it, and the live
run passed `ARR: INR 12 crore` beside FY26 revenue of ₹27 crore without a word.
It is now a MUST with both values and both sources required, because a silently
resolved conflict is indistinguishable from data that never conflicted, and the
reader loses the one signal telling them to check.

**`profile_research_batch`** — publisher URLs, publication name alongside every
link so the citation survives the link, and state every figure in the currency
and unit the source used.

---

## 10a. A pool of keys, rotated per call

Raising concurrency to 5 (§9) is pointless against one key: a provider's
per-minute quota is **per key**, so five concurrent grounded calls hit the same
wall the serial version did, only sooner. `fundos/llm/keyring.py` reads the
endpoint's configured variable, falls back to its plural sibling
(`GEMINI_API_KEY` → `GEMINI_API_KEYS`) so a pool can be supplied without editing
a seeded row, and hands keys out round-robin so concurrent workers do not all
take the first.

A key that reports exhaustion goes into a 65-second cooldown — sized to the
quota window — and is skipped until it expires. When *every* key is cooling
down, `next_key` returns the one that frees up soonest rather than `None`: the
call may still fail, and letting the adapter's existing retry and breaker see a
real 429 is better than manufacturing "no key configured", which sends an
operator to check an environment that is fine.

`adapter.py` acts on that. A 429 now rotates to another key and re-sends the
same body, up to four times, before surfacing a retryable error that says every
key is limited. The tracked `sent_body` matters here: both existing 400
fallbacks rewrite the request, and re-sending the *original* body after a
fallback had already adjusted it would reintroduce the 400 the fallback fixed.

**Keys are never logged.** `describe()` returns counts and last-four tails so
diagnostics, run records and support bundles can report pool health without key
material reaching them, and the key travels in the `x-goog-api-key` header
rather than the query string so it cannot surface in an `HTTPError` message.

Cooldowns are per process and deliberately not shared. A second worker
re-learning that a key is limited costs one wasted call; a shared store would
put a database round-trip in front of every LLM call to save it.

## 10b. One model, named explicitly

Every Gemini tier — `simple`, `advanced`, `judgment`, their vendor alternates,
and the `GEMINI` endpoint's `default_model` — is pinned to `gemini-2.5-flash`,
which is also what both profile-pipeline config profiles already pinned. One
model now serves the whole system instead of two sets that have to be kept in
step.

The catalog marks that model **retiring**, and a test exists precisely to stop
the seeder drifting onto a model with a published end date. Rather than delete
the guard, the exemption is written down: `Command.ACCEPTED_RETIRING_MODELS`
maps the model to the reason it was accepted, and the test skips only models
listed there — while asserting each carries a non-empty reason. The rule still
protects every other model, the decision stays visible in review, and whoever
inherits this gets a list of what to revisit rather than a silent pin.

## 10c. Configuration from a file, and one that read itself in

Settings read `os.environ` directly, so every variable had to be exported in the
shell that launched the process — which works for one command and silently does
not for the next terminal, for Celery, or for a scheduled run. `_load_env_file`
reads a `.env` beside `manage.py`: a real environment variable always wins (the
file is a default, never an override, so production under systemd behaves
exactly as before), it has no dependency (`python-dotenv` is not in
`requirements.txt`, and importing whatever happens to be in the virtualenv is
how an install works on one machine and not another), and it never raises,
because a settings module that throws at import takes down the very commands you
would use to diagnose it. The file is gitignored.

The loader skips itself under the test runner, because several tests assert the
*unconfigured* path — that vendor alternates stay inactive, that a role with no
usable key is left unbound. Loading real keys turns those into failures that
depend on who runs the suite.

**That was not sufficient, and the reason is worth recording.** Five tests in
`test_llm_cost_optimisation` passed alone and failed in company. The cause was
not this loader: `magika`, pulled in by MarkItDown and therefore imported the
first time a test uploads a document, calls `dotenv.load_dotenv(find_dotenv())`
**at import time**. That walks up from the working directory, finds the same
`.env`, and pushes every key into `os.environ` midway through the run — after
which seeding picks a provider by which key is visible, and the answer depends
on whether an unrelated test happened to touch document extraction first.

Since the load runs at module scope, the only pre-emptive hook is the flag, so
the test branch sets `PYTHON_DOTENV_DISABLED=1` before any such import can
happen. Two tests in `ResearchModelChainTests` had been passing *because* of the
leak — they repoint a config profile at the `GEMINI` endpoint to exercise the
**model** link of the diagnostic chain, and the endpoint link sits in front of
it, so without a key the chain stopped at "inactive endpoint" and never reached
the model. They now establish that precondition themselves.

## 11. Verification

`tests/api/test_profile_data_integrity.py` — 49 new tests. Every one names the
defect it pins, and each pins a value observed on the live profile rather than
an imagined one.

`tests/api/test_profile_pipeline.py` — 81 tests, unchanged and passing. Two of
them failed against a first draft of §2 and were right to: the draft
materialised every declared field as an empty value, and the tests were
asserting that the normalizer types what came back rather than inventing a
shape. The production code changed, not the assertions.

**Full suite: 1006 tests, 0 failures, 55 skipped** — measured before the
keyring, model pin and env-file work in §10a–§10c.

Those three landed against targeted runs instead:

```
manage.py test tests.api.test_configuration_integrity \
               tests.api.test_llm_cost_optimisation \
               tests.api.test_issue28_fixes \
               tests.api.test_profile_data_integrity
→ Ran 203 tests, OK (skipped=1)
```

Four modules, chosen because they are the ones the env-file leak touched: the
two that seed a provider, the one whose document upload triggered the `magika`
import, and the new suite. **The full run has not been repeated since**, so
treat 1006 as the number for §1–§9 and the 203 above as the number for
§10a–§10c.

### One assertion was widened, and why that is not a weakening

`test_spec_contract.test_empty_list_sections_return_field_template` failed on
the competitors template. Tracing it found a live inconsistency rather than a
bad change: `_data_competitors` has emitted `description` and `website` on a
POPULATED row since CR-12, while the empty-row template omitted both — so the
test was passing on a template that did not match the rows it is a template
for, and the schema never asked for either field, which is why all six
competitors on the live profile carried `description: ""` and `website: ""`.

Both are now in the section spec (so the prompt asks for them and `website` is
URL-validated on the way back), in the template, and in the test's expected set.
The assertion now requires the two shapes to agree, which is strictly more than
it required before.

## 12. Not done

**`_sync_financials_to_ckb` still does not convert.** It now refuses to write a
figure it cannot denominate and records the currency it did write, which stops
the wrong-unit value from reaching a valuation silently. It does not convert INR
crore to USD millions, because that needs a dated FX rate against the fiscal
year the figure belongs to — `FxRate` holds dated rows and the right lookup is
per row, not per sync. That is a self-contained follow-up; the current state
fails loudly rather than quietly, which is the part that was urgent.

**Single-section regeneration is still on the old path** — unchanged from
`CHANGES_company_profile_pipeline.md` §11, and it now also bypasses the new
boundary, since it does not go through `normalize_profile`. Moving regeneration
onto the stored dossier fixes both at once.

**The `revenue_model` clearance is explained, not prevented.** §7 records who
emptied it and when. Whether an empty required section should block completeness
at all is a product question, not a code one.
