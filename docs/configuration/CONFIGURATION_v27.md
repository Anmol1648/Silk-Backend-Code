# Configuration changes required — v27

The code fixes in `CHANGES_v27.md` remove the failure modes. These are the
things **only an administrator can do**, in the order that gets you a grounded
profile fastest.

Time to complete: about 20 minutes, most of it waiting for a test run.

---

## STEP 1 — Deploy and re-seed  (required, 5 min)

```bash
cd /d01/fundos/releases/<new-release>/FUNDOS/fundos-backend
source /d01/fundos/venv/bin/activate

python manage.py migrate --check          # expect: no changes. v27 adds none.
python manage.py seed_platform_config     # safe to re-run
```

Watch for this line — it is the repair pass fixing the root cause on your
**existing** rows, which `get_or_create` would otherwise never touch:

```
llm search: filled a blank search cap on N profile(s) that had web search on
```

`N` should be at least 1 (`tier.advanced.gemini`). If `N` is 0, the caps were
already set and the cause is elsewhere — go to Step 2 and check
`web_search` itself.

Then restart both services. **The worker matters** — generation runs there, and
a stale worker will keep running the old adapter:

```bash
sudo systemctl restart fundos-web fundos-worker
```

---

## STEP 2 — Verify the search config resolves  (required, 2 min)

Admin → LLM Config Profiles → `tier.advanced.gemini`:

| Field | Required value | Why |
|---|---|---|
| `web_search` | **ON** | Without it no tool is attached at all |
| `max_uses` | **6** | Blank used to mean "send the tool, say nothing". Now defaults to 6, but set it explicitly so the intent is visible |
| `structured_output` | **ON — leave it on** | Gemini 3 supports search + structured output together. The old warning said to turn this off; that advice was wrong and is now removed from the code |
| `is_active` | **ON** | |
| `user_location` | `{"country": "IN"}` | Localises results to your market |

Also confirm the tenant's **advanced** tier actually points at this profile
(Admin → Tenant LLM Tiers). A profile nobody resolves to changes nothing.

Then confirm the search breaker is not open — a profile that repeatedly
overran its cap loses search until the cooldown expires:

```bash
python manage.py shell -c "
from django.core.cache import cache
print('breaker OPEN' if cache.get('llm:searchbreaker:tier.advanced.gemini') else 'breaker closed')"
```

### Independent check, outside the application

If search still does not fire after Step 4, run this to establish whether the
problem is your key/model or our stack:

```bash
curl -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent" \
  -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' -d '{
    "contents":[{"parts":[{"text":"Who led Deepak Nitrite'\''s most recent funding round? Search before answering."}]}],
    "tools":[{"google_search":{}}]
  }' | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['candidates'][0].get('groundingMetadata',{}).get('webSearchQueries','NO SEARCH PERFORMED'))"
```

A list of queries means the key and model are fine. `NO SEARCH PERFORMED`
means the problem is upstream of us — check the API key's enabled products.

---

## STEP 3 — Fix the TLS trust store  (required, 3 min)

Deepak Nitrite failed twice on this and it will fail for any site serving an
incomplete chain:

```
certificate verify failed: unable to get local issuer certificate
```

This is a trust-store problem **on your server**, not the site's fault:

```bash
sudo yum update ca-certificates && sudo update-ca-trust
source /d01/fundos/venv/bin/activate && pip install --upgrade certifi
sudo systemctl restart fundos-web fundos-worker

python3 -c "import requests; requests.get('https://www.godeepak.com', timeout=15); print('TLS OK')"
```

Certificate verification is never disabled anywhere in the codebase, by
design — an unauthenticated source cannot be cited. Do not add a `verify=False`
workaround; it would poison the grounding guarantee the whole pipeline rests on.

---

## STEP 4 — Test, and read three lines  (required, 5 min)

Regenerate one company **with force**, because Step 3 changed the sources and
the skip gate is now stricter:

```bash
tail -f /d01/fundos/logs/silk_generation.log &
# then trigger a regeneration with force=true from the UI or API
```

**Line 1 — did search run?**

```
step="llm.call.company_profile_deep_extract" ... searches=2 search_expected=true
```

`searches=` is now explicit. `unknown` means the provider returned no count;
a number means it counted. Anything other than `0` or `unknown` is success.

**Line 2 — is the run grounded?**

```
ASSESSMENT INPUTS ...: {'mode': 'full', ...}
```

`full` is the goal. `website_only` means search still is not running (Step 2).
`none` means nothing was retrieved at all (Step 3).

**Line 2a — did the record forms keep anything?**

```
step="records.form_seed" ... section="company_metrics" kept=5 dropped=0
```

`kept=0` used to be universal. Any non-zero `kept` means the alias fix landed.
A remaining `dropped` should now carry a reason that names the actual problem
rather than "no recognisable label".

**Line 3 — the diagnosis.**

```
DIAGNOSIS 1 [OK] No fault detected. Generation completed and wrote sections.
```

If you regenerate twice without changing anything, the second run should now
say `[NONE] Nothing to do — the sources have not changed…` rather than asking
you to contact the development team.

---

## STEP 5 — The nine missing `cp_extract.*` profiles  (recommended, 10 min)

Every run logs nine of these:

```
config profile 'cp_extract.company_overview' does not exist —
falling back to the resolved tier
```

Missing: `company_overview`, `key_people`, `revenue_model`, `company_metrics`,
`funding_history`, `recent_news`, `readiness`, `cap_table`, `market_research`.

Fallback is safe, so this is not breaking anything — but it means per-section
tuning is applied nowhere, and eighteen warning lines per run is enough noise
to hide a real one. **Choose one:**

* **Create them** (Admin → LLM Config Profiles → Add), if you want per-section
  control. `market_research` and `cap_table` want the advanced tier with
  search; the rest want the simple tier without.
* **Or remove the references**, if the tier fallback is the intended design.

Either is fine. Leaving it as it is means these warnings are permanent, and
permanent warnings stop being read.

---

## STEP 6 — Sub-sector mapping for conglomerates  (recommended)

```
sub-sector 'Energy, Petrochemicals, Retail & Digital Services' matched none of
the loaded labels. Category F carries 20% of the rating and will score blank
```

Reliance scored `coveragePct=31.2` largely because of this. Add a
`SectorMapping` row (Admin → Sector Mappings) for conglomerates, or decide
that multi-sector businesses map to their **largest revenue segment** and
document that rule — the taxonomy is behaving correctly, it just has no answer
for this shape of company.

---

## STEP 7 — Research adapters  (the ceiling on everything above)

All nine still report:

```
status="SKIP" reason="fixture adapter — no live feed behind this source yet"
```

Until live feeds sit behind `market`, `competitor`, `industry`,
`public_funding`, `news`, the best any run can reach is `mode="full"` on the
strength of web search alone. This is a build item, not a config toggle — but
it is the reason coverage stays where it is, and it belongs on the same list.

---

## Expected state once Steps 1–4 are done

| Signal | Before | After |
|---|---|---|
| `searches=` | `""` on all 21 calls | a number, or explicit `unknown` |
| `mode=` | `website_only` | `full` |
| Ungrounded run | 12 sections written from memory | 0 sections, explicit refusal |
| Failed run, then retry | silently skipped | actually retries |
| `financials→CKB sync failed` | every run | gone, or replaced by the real error |
| ERROR lines per double-click | 1 with traceback | 0 |
| `coveragePct` | 31–69% | materially higher once search runs |
| `records.form_seed` | `kept=0 dropped=5` every run | rows kept; only genuinely bad ones dropped |
| Financials | one long label discarded all years | labels normalised, bad rows dropped individually |

## Rollback

v27 adds no migrations, so rollback is repointing the release symlink and
restarting. The seeder's repair pass only fills blank caps — it does not
overwrite a value you set — so it leaves nothing to undo.
