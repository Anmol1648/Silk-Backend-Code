# Configuration — v27.2

## STEP A — Deploy  (2 min)

No migrations. No re-seed.

```bash
# extract over the release, then
sudo systemctl restart fundos-web fundos-worker
```

Then regenerate Lenskart or Titan **with force** and read three lines:

```
sources.website  status="OK"   chars=<a big number>      ← the 403 fix worked
sections.summary status="OK"   sections_written=14        ← profile written again
DIAGNOSIS 1 [OK] No fault detected.
```

If the website line still says `403`, that site is refusing browser headers
too — rare, and it means the company needs a document uploaded instead. The
run will now stop immediately rather than spending 19 LLM calls to produce an
invented profile.

---

## STEP B — Why the deep extract still will not search  (5 min)

The only substantive thing left. Do **not** change the config profile:
`company_profile_section` returned `searches=2` in the same runs, on the same
profile, endpoint and model. Configuration is proven correct.

The two roles that do not search differ in exactly two ways:

| Role | Directive | `context_chars` | `searches` |
|---|---|---|---|
| `company_profile_section` | weak cap | 8,971 | **2** |
| `company_profile_deep_extract` | "SEARCH FIRST" | **24,081** | unknown |
| `assessment_inputs` | "SEARCH FIRST" | **24,081** | unknown |

### Test 1 — the prompt (2 min, and the likelier of the two)

Admin → Prompt Templates → `company_profile_deep_extract`. Read the body you
authored on 13 Aug and look for:

* "use only the information supplied"
* "do not speculate" / "do not invent"
* "base your answer on the context provided"
* any instruction to return JSON and nothing else

Any of these contradicts the appended "SEARCH FIRST — THIS IS NOT OPTIONAL"
directive, and the prompt body is the larger, more specific instruction, so
the model follows it. If you find one, rewrite it to make searching part of
the job — *"Search the web for market size, funding history and headcount; use
the supplied context for everything it covers"* — and re-run.

### Test 2 — context saturation

Temporarily trim the deep extract's context (drop `research` and `founders`,
or truncate `website_extract`) and re-run. If `searches` becomes a number, the
cause is saturation, and the fix is structural: split the deep extract into a
*research* call that searches and an *extraction* call that structures.

Either way, the line to read is:

```
step="llm.call.company_profile_deep_extract" ... searches=2 search_expected=true
```

---

## STEP C — The research adapters are the real ceiling

```
sources.research.market      SKIP  fixture adapter — no live feed behind this source yet
sources.research.competitor  SKIP
sources.research.industry    SKIP
sources.research.public_funding SKIP
sources.research.news        SKIP    (…and four more)
```

**Nine of nine.** The website is the only live source in the system, which is
why a single 403 empties an entire run and why the grounding gate had anything
to refuse. Until at least one of these has a live feed, or search fires, every
profile rests on one homepage.

This is the highest-value build item on the list. It is not a config toggle.

---

## STEP D — Still outstanding from v27

**TLS trust store** — `www.godeepak.com` has failed identically on three
separate days:

```bash
sudo yum update ca-certificates && sudo update-ca-trust
source /d01/fundos/venv/bin/activate && pip install --upgrade certifi
sudo systemctl restart fundos-web fundos-worker
```

**The nine `cp_extract.*` profiles** — still missing, still ~18 warning lines
per run. Note `cp_extract.financial_summary` falls back to
`tier.simple.gemini`, which has `web_search=False`; that is correct for a
records call, but any role landing there cannot search regardless of prompt.

---

## STEP E — A decision, not configuration

`revenue_model` drops streams with no percentage. The mapping works — add a
split and the row is kept — so this is a product rule. Either require the
split in the prompt, or relax the form to store un-split streams. Tell me
which.

---

## Expected state after Step A

| Signal | 17 Aug (v27) | After v27.2 |
|---|---|---|
| Lenskart / Titan website | `403`, 0 chars | fetched |
| LLM calls on a doomed run | **21** | 0, or 1 if search is available |
| Sections written on a doomed run | **20, ungrounded** | 0, profile untouched |
| Profile after a failed run | replaced with invented content | **unchanged** |
| Status after a failed run | `generating` (spins) | resting |
| Financials | `kept=0` (NameError) | kept |
