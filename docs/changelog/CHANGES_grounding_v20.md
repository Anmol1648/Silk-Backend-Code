# v20 — grounding, and errors that say what went wrong

Everything here comes from the live Deepak Nitrite run (`GEN[94bf07]`) and
the five runs alongside it in the same log. That run reported 31 steps, one
failure, `finish_reason=STOP`, `was_repaired=false`, and populated 78 of 89
fields. It was also, in the sense that matters to an investment committee,
entirely unsourced. Nothing in the trace said so.

---

## 1. The dossier was written from memory

The deep extract issued no web search (`searches=None`) and was handed **367
characters** of source material — website 0 after the TLS failure, documents
0, founders 0, and 353 characters of research that turned out to be
placeholder strings. From that it produced 78 populated fields.

Those fields came out of the model's training weights. They cannot be cited,
dated or checked, and the company name on the deal record is misspelled
("Deepak Nitrate" for Deepak Nitrite Limited) — the model recognised it
anyway and answered confidently, which is precisely the behaviour that makes
this dangerous rather than merely thin.

**New diagnosis rule, and it is the highest-priority one in the file.** A
deep extract that issues no search, receives less than 2,000 characters, and
still populates 20+ fields is now CRITICAL: *"The dossier was written FROM
THE MODEL'S MEMORY, not from retrieved sources … treat every figure in this
run as unverified."* Replayed against `94bf07`, this is now finding #1; the
website TLS failure drops to #2.

A weaker HIGH fires when there is no search and little material but the
output is sparse too — that is a thin run, not a fabricated one.

---

## 2. The website adapter threw away the page

The single biggest accuracy defect, and it was hiding behind a PASS.

`WebsiteAdapter.fetch()` requested the page and kept **the `<title>` tag**.
That is why the successful ONGC fetch in run `9ecbca` contributed
`chars=221` and the earlier ril.com fetch `chars=321`. The fetch had
genuinely succeeded; the extraction discarded the body. One of only two
sources that can ground a dossier was returning a page title.

Rewritten to extract real content:

* visible body text with `<script>`, `<style>`, `<noscript>` and `<svg>`
  removed first, entities unescaped, whitespace collapsed;
* the meta description and OpenGraph description;
* the homepage **plus up to three** of `/about`, `/about-us`, `/company`,
  `/who-we-are`, `/investors`, `/investor-relations`, `/products`,
  `/what-we-do` — a homepage is a marketing banner, the facts are one click
  in. Best-effort: a 404 on `/about` is normal and never fails the source;
* capped at 12,000 characters per page and 30,000 in total;
* a browser-like User-Agent and redirects followed;
* **www/apex fallback** — which of `example.com` and `www.example.com`
  serves the site is arbitrary, and so is whichever one someone typed into
  the company record. A certificate issued for only one of them, or an apex
  that does not resolve, used to lose the best source the profile has over a
  four-character prefix. Both are tried; the original error is re-raised if
  neither works, so the diagnosis still names the address the operator
  entered;
* `client_rendered: true` when a page is reached but yields no extractable
  text, which is the JavaScript-rendered signature — reported rather than
  silently returning a payload that reads as a thin company.

Certificate verification is **not** disabled, and should not be: a source we
cannot authenticate is a source we cannot cite. The SSL error is instead
re-raised with the actual remedy, and a new diagnosis rule separates it from
a network failure — the old one told you to check outbound internet access
for what is a trust-store problem on the application server.

---

## 3. Every Gemini failure was reported as a billing problem

Run `9ecbca` got a **400 Bad Request** on `gemini-3.6-flash` and the
diagnosis read *"The AI provider refused the request for rate-limit, quota or
billing reasons. FIX: Check the account's billing status."*

The rule tested `"rate" in err.lower()`. The Gemini URL ends in
`gene`**`rate`**`Content`. It matched on **every Gemini error of every kind**,
so a rejected payload was indistinguishable from an exhausted quota and the
operator was sent to the provider's billing console.

* Matching is now on the parsed HTTP **status code** and on whole words. A
  bare `"400" in err` also matches a model named `gpt-400` or a byte count,
  so the code is extracted with an anchored pattern.
* A dedicated **400 branch**: *"The AI provider REJECTED THE REQUEST ITSELF
  — this is a configuration or payload fault, not a billing one"*, naming
  the usual causes, the first being the Gemini search + `responseSchema`
  conflict that v19 fixed in the seeder.

---

## 4. …and the provider's explanation was thrown away

`resp.raise_for_status()` produces `400 Client Error: Bad Request for url: …`
and **discards the response body** — which for a 4xx is the only part that
says why. A rejected schema, an unknown model, a bad capability combination
and an exhausted quota all arrived as the same sentence.

`_raise_for_status()` now parses the provider's JSON `error.message` into the
exception. Applied to Gemini, DeepInfra and custom REST; Anthropic and OpenAI
go through SDK clients that already surface their own messages.

---

## 5. Your API key was in the log file

The Gemini key was a **query parameter**, so it appeared inside requests'
`HTTPError` message ("… for url: `<full url>`"), and from there into
`silk_generation.log`, into `ProfileGenerationRun.diagnostics`, and into
every support bundle built from either.

* The key now goes in the `x-goog-api-key` **header**. Gemini accepts it on
  every endpoint; there was never a reason for it to be in the URL.
* `_redact()` strips `key=` / `api_key=` / `access_token=` parameters and
  bare `AIza…` / `sk-…` tokens from anything on its way to a log or a trace,
  as a second line of defence.

**Rotate the key that appeared in the log you sent.** It is in that file, in
the database, and in this conversation.

---

## 6. Funding rounds were being discarded over unknown days

```
PROFILE: deep-extract round 0 skipped: "2006-05" value has an invalid date format.
PROFILE: deep-extract round 1 skipped: "2010-02" value has an invalid date format.
```

Rounds are routinely announced with month precision, and the month **is** the
fact. `announced_date` went straight to a `DateField`, the whole
`create()` raised, and the amount, lead investor and investor list were
discarded along with the unknown day. Two rounds lost in that run. It was
caught into a `logger.warning` that reached no trace, so
`sections.write.funding_history` still reported OK — and since rounds are
only written when none exist, re-running never repaired it.

* `_partial_date()` coerces `2006-05` → `2006-05-01`, `2015` → `2015-01-01`,
  and returns the precision alongside.
* New `FundingRound.date_precision` (`day` / `month` / `year` / unknown) so
  the UI can render "May 2006" rather than asserting a day nobody reported.
  Migration `companyprofile.0011`.
* A round that still cannot be written now emits `records.funding_round`
  FAIL **into the trace**. Data loss belongs where the reader looks.

The same treatment for the two swallowed form seeds in that run
(`revenue_model: Invalid row`, `company_metrics: This field is required`):
they now emit `records.form_seed` FAIL with the section and row count. The
old behaviour made a validator rejection look like the model returning
nothing, sending the reader after the wrong component entirely.

---

## 7. Nine dead research adapters looked like nine working ones

`market`, `competitor`, `industry`, `public_funding`, `patents`, `news`,
`awards` and `hiring` return placeholders pending their feeds —
`MarketAdapter` literally returns *"Market context for sector 'unknown'
pending feed ratification"*. All eight reported **OK** at `ms=0` and
contributed 353 characters to the source total, which is most of what made a
completely unsourced run look healthy.

`BaseResearchAdapter.is_fixture` now declares this, the eight placeholder
adapters set it, and the pipeline reports them as **SKIP** with a reason —
and keeps their text out of the prompt entirely. `research_chars` falls to
its honest value of 0.

---

## 8. Recalculation propagation stopped silently outside a request

```
PROFILE: financials→CKB sync failed for financial_summary:
null value in column "tenant_id" of relation "artifact_status"
```

`recalc.mark()` read `get_current_tenant()`, which is populated by request
middleware and is therefore `None` in a management command, a Celery task or
a shell. The write died on the not-null constraint, the caller caught it and
logged it, and propagation stopped — downstream artefacts were never flagged
stale, so the deal team goes on reading a memo built from figures that have
since changed.

The tenant now comes from the **deal**, which already carries it, with the
request scope as fallback. If neither resolves, it logs at ERROR and returns
`None` rather than raising into a caller that will swallow it.

---

## Tests

`tests/api/test_grounding_and_errors.py` — 14 new tests, one per defect
above: key absent from the URL and present in the header; error body
surfaced and redacted; a Gemini 400 not routed to billing while a real 429
still is; a dense-output-from-no-sources run flagged CRITICAL while a
grounded one is not; partial dates across six shapes; body text extracted
rather than the title; script and style content kept out; fixture adapters
declaring themselves; recalc resolving the tenant from the deal; the
www/apex fallback both succeeding and failing.

Existing Gemini test doubles updated for the new `headers` kwarg
(`test_gemini_thinking`, `test_llm_phase2_gemini`).

**Full run with `GEMINI_API_KEY=test-key` set — the state of the live
server** — 552 tests, all passing:

| Modules | Tests |
|---|---|
| `test_grounding_and_errors`, `test_gemini_thinking`, `test_configuration_integrity`, `test_llm_*`, `test_generation_trace` | 229 |
| `test_profile_defect_pass`, `test_silk_review_fixes`, `test_qa_enhancements_*`, `test_issue28_fixes`, `test_spec_contract` | 88 |
| `test_flows`, `test_backfill`, `test_clarifications`, `test_deal_assessment`, `test_gap_fixes` | 93 |
| `test_change_request_pass`, `test_exports`, `test_issue*`, `test_qa_*_fixes` | 86 |
| `tests.engines`, `tests.isolation`, `tests.investors` | 56 |

Replaying `GEN[94bf07]` through the new rules produces, in order:

```
1. [CRITICAL] The dossier was written FROM THE MODEL'S MEMORY, not from
              retrieved sources …
2. [HIGH]     The company website is reachable but its TLS certificate could
              not be verified by this server. This is a trust-store problem
              HERE, not a network problem.
```

---

## What this does NOT fix

**Per-field provenance.** There is still no source URL or retrieval date
attached to an extracted figure, so the UI cannot distinguish a number read
off an annual report from one the model recalled. v20 makes an ungrounded
*run* impossible to miss; it does not make an ungrounded *field* visible
inside a grounded run. That needs a schema change and a UI decision about how
verified and unverified fields should look, which is worth discussing before
building.

**The research feeds.** Eight adapters now honestly report that they have no
feed behind them. Giving them one is a data-sourcing project, not a code
change.


---

## Deploying

```bash
python manage.py migrate                  # llm.0011, companyprofile.0011
python manage.py seed_platform_config     # v19 tier + capability repair
python manage.py seed_llm_costs
sudo systemctl restart fundos-web fundos-worker
python manage.py diagnose_config
```

Then, on the application server — the website is worth far more to the
profile now than it was:

```bash
sudo yum update ca-certificates && sudo update-ca-trust
pip install --upgrade certifi
```

**Rotate the Gemini API key.** It appeared in `silk_generation.log`, in
`ProfileGenerationRun.diagnostics`, and in any support bundle built from
either, because it used to travel as a URL query parameter.

Then regenerate and read three lines:

* `sources.website` — `chars` in the thousands, not the hundreds. A few
  hundred now means `client_rendered=true`, i.e. a JavaScript site.
* `llm.call.company_profile_deep_extract` — `searches=` non-zero.
* the DIAGNOSIS block — no "written FROM THE MODEL'S MEMORY".
