# DEPLOYMENT — FundOS backend v28

Supersedes v27.5. **Do not deploy v27.5 separately** — it contains the
runtime fixes but not the seed fixes, so re-running the seeder would
recreate two of the problems it works around.

---

## 0. Before you start

```bash
# Rotate the Gemini API key if it has ever been pasted into a chat,
# ticket or document. Google AI Studio -> API keys -> delete, regenerate.

export GEMINI_API_KEY='…'
export GOOGLE_AI_API_KEY="$GEMINI_API_KEY"
```

Back up the database. v28 changes seeded configuration rows in place (see
step 3); the changes are narrow and reversible, but a snapshot costs nothing.

```bash
pg_dump -Fc fundos > /d01/backup/fundos_pre_v28_$(date +%Y%m%d).dump
```

---

## 1. Deploy the code

```bash
cd /d01/fundos
tar xzf fundos-backend_v28_domain_agnostic.tar.gz
cd fundos-backend
pip install -r requirements.txt
```

---

## 2. Migrations

```bash
python manage.py migrate
```

Expect `0013_alter_llmconfigprofile_max_uses` to apply. **This is the
migration that was missing from v27.3** and that you had to generate on the
production host. If your host already has a locally generated `0013`, Django
will report it as applied and do nothing — the two are identical.

Then confirm nothing is outstanding:

```bash
python manage.py makemigrations --check --dry-run
```

Expected: `No changes detected`. Anything else, stop and report it — a model
change without a migration file is what caused this step to be manual last
time.

---

## 3. Re-seed (this is where the configuration fixes land)

```bash
python manage.py seed_platform_config
python manage.py seed_llm_costs
```

The seeder is idempotent. Three things will change on your install:

**a. `ignoring duplicate endpoint(s) GEMINI` will stop appearing.**
Previously the seeder created a canonical `GEMINI` row alongside your
hand-made `GEMINI LLM`, then reported its own creation as a duplicate. It now
adopts the existing row. Expect instead:

```
  llm endpoint: gemini already served by 'GEMINI LLM' — not creating 'GEMINI'
```

Your `GEMINI LLM` row survives untouched. If a stray `GEMINI` row already
exists from a previous seed, deactivate it in Admin -> LLM Endpoints; the
seeder will not delete a row it did not create.

**b. Structured output is turned OFF on searching profiles.**

```
  llm search: turned OFF structured output on N searching profile(s) —
  a responseSchema suppresses google_search on Gemini (measured 19 Aug 2026:
  0/4 searched with a schema, 4/4 without), so the two cannot both be on
```

This aligns the admin screen with what the runtime has done since v27.5. It
does not change behaviour; it stops the screen showing a tick the system
ignores.

**c. Thinking budgets become integers.** `thinking_budget="4000"` was seeded
as a string, which is why the trace showed a quoted number next to a bare one
and broke log parsing.

---

## 4. Verify configuration

```bash
python manage.py diagnose_config
```

Work through anything not `PASS`. Three checks matter most here:

* **Prompt tier overrides** — should read `no prompt overrides the shipped
  tier`. If it reports `company_profile_section: simple -> advanced`, see
  ADMIN_CONFIG_v28.md §1; that override now costs the role its schema.
* **LLM role bindings** — `assessment_inputs` must have an EMPTY output
  ceiling. See ADMIN_CONFIG_v28.md §2. Without this, v28's fix for the
  truncated assessment is inert.
* **Research sources** — will report honestly that most adapters are
  fixtures. That is expected and is not a deployment failure.

---

## 5. Restart

```bash
sudo systemctl restart fundos-api
sudo systemctl restart fundos-worker
sudo systemctl status fundos-api fundos-worker
```

---

## 6. Settle H4 before the first production run

Sixteen API calls, no deploy, and it answers the one question v27.5 left open:
whether the deep extract's system prompt suppresses search independently of
the schema.

```bash
python tools/diagnose_gemini_search.py --model gemini-3.5-flash
```

Reading the grid, and what each result means, is in TESTING_v28.md §5.
Record the answer in `fundos/llm/tier_contract.py` next to the 19 Aug grid.

---

## 7. First live generation

```
Admin -> Application Configuration -> AI mocked = OFF
```

Pick a company whose website is over ~30,000 characters, so the compression
reporting is exercised. Then:

```bash
grep -E 'run.economics|sources.compression|fanout_trim|CONTRACT_VIOLATION' \
     var/logs/silk_generation.log | tail -40
```

Pass conditions are in TESTING_v28.md §6. The single one that matters:
**`searches` greater than zero.** Not the absence of a warning.

---

## Rollback

```bash
sudo systemctl stop fundos-api fundos-worker
cd /d01/fundos && tar xzf fundos-backend_v27_5_measured_fixes.tar.gz
pg_restore -c -d fundos /d01/backup/fundos_pre_v28_YYYYMMDD.dump
sudo systemctl start fundos-api fundos-worker
```

No migration needs reversing — `0013` is an `AlterField` widening a column to
nullable, which older code tolerates. The seed changes are data and are
restored with the dump.
