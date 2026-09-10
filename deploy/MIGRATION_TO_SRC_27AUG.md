# FundOS — migration to `/d01/fundos/src` and `/d01/fundos-web/src`

**Date:** 27 August 2026
**Backend:** `befundos.gyain.com` (10.130.0.34) → `/d01/fundos/src`
**Frontend:** `fefundos` → `/d01/fundos-web/src`
**Downtime:** ~1–2 minutes (backend only). Frontend cutover is a symlink flip, no downtime.

---

> ## ⚠️ THE PRODUCTION DATABASE IS POSTGRESQL AND IS NOT CHANGING
>
> This release does **not** introduce SQLite to production, does not migrate
> data between engines, and does not alter the database configuration at all.
>
> | Settings module | `ENGINE` | Where it runs |
> |---|---|---|
> | `fundos.settings.prod` | `django.db.backends.postgresql` | **befundos — systemd** |
> | `fundos.settings.dev` | `django.db.backends.sqlite3` | developer laptops only |
>
> `settings/base.py` sets PostgreSQL as the engine for every settings module.
> `settings/prod.py` only adds `CONN_MAX_AGE` and `connect_timeout` on top —
> it never touches `ENGINE`. Only `dev.py` overrides to SQLite, and `dev.py`
> is never used on a server.
>
> §1.4 describes a SQLite connection hook added in this release. Its first
> line is `if connection.vendor != "sqlite": return`. Against the production
> PostgreSQL connection it returns immediately, without opening a cursor, so
> no `PRAGMA` is ever issued. It exists for developer laptops only.
>
> **Verify it yourself on the server after deploying — see §7.1a.**

---

## 0. What this migration is, in one paragraph

The backend currently runs from `/d01/fundos/current/FUNDOS/fundos-backend` — a
symlink into a timestamped release directory, with two redundant levels of
nesting inside it. This migration flattens that to a single working tree at
`/d01/fundos/src`, ships the code changes made between 21 and 27 August, and
loads the Sector Deal Data the Deal Scorecard needs in order to score
categories E and F at all. The database schema does **not** change: this is a
code-and-configuration deploy.

### Decisions this runbook implements

| Decision | Choice | Consequence |
|---|---|---|
| Backend layout | Flat `/d01/fundos/src` | `releases/` + `current` retired; `deploy-backend.sh` replaced |
| Frontend build | On the server (`npm ci`) | fefundos needs registry.npmjs.org |
| Cutover window | Anytime, low traffic | Services stop for ~1–2 min |

### What is NOT in this migration

* **No database migrations.** `makemigrations --check` reports "No changes
  detected". Nothing in this release alters a model.
* **No secrets.** The artifact excludes `.env`. The server keeps reading
  `/etc/fundos/fundos.env`, which this migration does not touch.
* **No customer data.** `var/` is excluded from the artifact and carried
  forward from the existing tree by the deploy script.

---

## 1. What changed in the code

### 1.1 Assessment / Deal Scorecard

| File | Change |
|---|---|
| `fundos/assessment/integrity.py` | **New.** 13 evidence-quality audit checks |
| `fundos/assessment/services.py` | Wires `integrity.checks()` into `run_integrity_checks` |
| `fundos/assessment/phase1.py` | Adds `rating_gap()`; `dealScorecardData.ratingGap` |

The integrity block answers a question the scorecard could not previously
answer: *how much of this score rests on something a reader can check?* It
reports uncited values, derived figures with no working shown, judgements
scored above Fair with nothing behind them, weight resting on low-confidence
values, an unresolved cohort, and an ask far outside what the cohort raises.

`severity: "error"` has a consequence — `run_scoring` sets
`audit_status = "failed"` — so it is reserved for what cannot be true of a
sound assessment (no score at all, a value scored with no evidence tier, a
sub-sector TAM larger than its own sector). A thin evidence pack is a
`warning`. **Expect existing assessments to gain `warning` findings after this
deploy.** That is the checks working, not a regression.

### 1.2 Logging — fixes a silent data-loss bug

| File | Change |
|---|---|
| `fundos/core/logging.py` | **New** `SharedRotatingFileHandler` |
| `fundos/settings/base.py` | Uses it; adds `delay: True` |

`RotatingFileHandler` rotates by renaming the file it is writing. More than one
process always holds that file (gunicorn runs 4 workers; celery runs 4). When
the rename failed, the stdlib handler raised inside `emit`, logging printed a
traceback per line, **and lines were lost**. Measured under a blocked rename:

| | tracebacks | lines kept | stream |
|---|---|---|---|
| stdlib (old) | 18 | **2 of 20** | dead |
| new | 0 | **20 of 20** | alive |

On Linux the rename normally succeeds, so this is less acute than on Windows —
but with 8 processes sharing one log it is not theoretical, and the failure
mode is losing 90% of the diagnostic log exactly when it is being read.

### 1.3 `requirements.txt` — fixes financial models reading as empty

```diff
- markitdown>=0.0.1a2
+ markitdown[docx,pptx,xlsx,xls,pdf]>=0.0.1a2
```

Bare `markitdown` installs the converter classes but not the libraries they
read with. An `.xlsx` financial model raised `MissingDependencyException`,
recovered **0 characters**, and category B (Financials) scored blank on every
company — with nothing on the scorecard saying the model was never read.
This was reproduced on 26 August against a real upload.

### 1.4 SQLite tuning — DEVELOPER LAPTOPS ONLY, inert on this server

**Production is PostgreSQL. This change cannot affect it.** See the banner at
the top of this document.

`fundos/core/apps.py` adds a `connection_created` receiver that enables WAL
and a 30-second busy timeout. Its first statement is:

```python
if connection.vendor != "sqlite":
    return
```

Against PostgreSQL it returns before opening a cursor, so no `PRAGMA` is
issued. `settings/dev.py` also gains `"OPTIONS": {"timeout": 30}` — `dev.py`
is not deployed.

*Why it exists:* on a developer laptop the dev SQLite file locks while a
profile run holds a write connection for minutes. An assessment starting in
that window died in 0.14s with `database is locked`, leaving an assessment
with zero parameters and a blank Deal Scorecard. PostgreSQL has MVCC and
never exhibits this, which is precisely why the fix is scoped out of it.

### 1.5 Deployment files

| File | Change |
|---|---|
| `deploy/deploy-backend-src.sh` | **New.** Flat-layout deploy with rollback |
| `deploy/systemd/*.service` | Corrected: `WorkingDirectory=/d01/fundos/src`, celery logs to `/d01/fundos/logs` |

> The repo's systemd units previously said
> `WorkingDirectory=/d01/fundos/app/current` — **a path that does not exist on
> the server** — and pointed celery logs at `/var/log/fundos`, while the live
> units use `/d01/fundos/logs`. The repo copies were stale and misleading.
> They now match reality plus the new `src` path.

### 1.6 Tests

`tests/api/test_assessment_integrity.py` (33 tests) and
`tests/api/test_logging_rotation.py` (10 tests).
Full suite before this work: **1081 passed**. After: **1124 passed, 0 failed,
55 skipped** (the 55 skips are workbook-gated and pre-existing).

---

## 2. Artifacts

Staged on the build PC at `Downloads\FUNDOS_DEPLOY_27AUG\`:

| File | Size | SHA-256 (first 16) |
|---|---|---|
| `fundos-backend-<stamp>.tar.gz` | ~1.1 MB | see `SHA256SUMS.txt` |
| `fundos-frontend-<stamp>.tar.gz` | ~1.0 MB | see `SHA256SUMS.txt` |
| `Tyreplex_Deal_Assessment_v22.xlsx` | 0.63 MB | Sector Deal Data source |

**Excluded from both archives and verified absent:** `.env`, `.env.bak-*`,
`var/`, `*.sqlite3`, `.venv/`, `node_modules/`, `__pycache__/`, `*.pyc`,
`dist/`, `.git/`, `celerybeat-schedule`.

The backend archive is **flat** — `manage.py` is at the root, not under
`FUNDOS/fundos-backend/`. This is required by the new deploy script and is
the point of the migration.

---

## 3. PRE-FLIGHT — run these before touching anything

### GATE 1 — Has anyone edited code directly on the server?

**Stop condition: if this returns any file, do not proceed.** The artifact
would silently revert that change.

```bash
APP=/d01/fundos/current/FUNDOS/fundos-backend
find $APP -name '*.py' -newermt '2026-08-22' \
     -not -path '*/var/*' -not -path '*/__pycache__/*' -ls
```

Expected: **no output**. If files are listed, capture them
(`cp` them somewhere safe) and reconcile before deploying.

Also record what is already there, so §7 can confirm the new code arrived:

```bash
ls -l $APP/fundos/assessment/ | grep -E 'phase1|integrity'
md5sum $APP/requirements.txt
```

### GATE 2 — Disk space

The deploy keeps `src.previous`, so it needs roughly 2× the tree.

```bash
df -h /d01
du -sh /d01/fundos/releases/ /d01/fundos/current/FUNDOS/ 2>/dev/null
```

**Stop condition: less than 2 GB free on `/d01`.** Reclaim first — the
451 MB `fundos-backend.tar` and `fundos-backend_ORG/` inside the release
directory are the obvious candidates, but only remove them **after** §7 passes.

### GATE 3 — Full backup

```bash
TS=$(date +%Y%m%d-%H%M)
sudo -u fundos cp -a /d01/fundos/current/FUNDOS/fundos-backend \
     /d01/fundos/BACKUP-backend-$TS
sudo -u postgres pg_dump fundos | gzip > /d01/fundos/db-$TS.sql.gz
ls -lh /d01/fundos/db-$TS.sql.gz /d01/fundos/BACKUP-backend-$TS/manage.py
```

**Stop condition: either command fails, or the dump is 0 bytes.**

### GATE 4 — Record the gunicorn log paths

The live web unit's `ExecStart` is line-wrapped, so its `--access-logfile` /
`--error-logfile` values were not visible in earlier output. Capture them now;
§5.3 must preserve whatever they are.

```bash
systemctl cat fundos-web | sed -n '/ExecStart/,/^$/p'
ls -ld /var/log/fundos /d01/fundos/logs 2>/dev/null
```

---

## 4. BACKEND — transfer

From the build PC (PowerShell):

```powershell
cd "C:\Users\Somen Mukherjee\Downloads\FUNDOS_DEPLOY_27AUG"
scp fundos-backend-*.tar.gz Tyreplex_Deal_Assessment_v22.xlsx `
    root@befundos.gyain.com:/tmp/
```

On befundos, verify the bytes survived the trip:

```bash
cd /tmp && sha256sum fundos-backend-*.tar.gz
# compare against SHA256SUMS.txt from the build PC
tar -tzf fundos-backend-*.tar.gz | head -5      # expect ./manage.py near the top
```

**Stop condition: the hash does not match.** Re-transfer; do not proceed.

---

## 5. BACKEND — deploy

### 5.1 Install the new deploy script

It ships inside the artifact, so extract just that file first:

```bash
cd /tmp
tar -xzf fundos-backend-*.tar.gz ./deploy/deploy-backend-src.sh
sudo install -o root -g root -m 755 /tmp/deploy/deploy-backend-src.sh \
     /d01/fundos/deploy-backend-src.sh
```

Retire the old one so nobody runs it by muscle memory — its structure check
(`$REL/FUNDOS/fundos-backend/manage.py`) can no longer pass:

```bash
sudo mv /d01/fundos/deploy-backend.sh /d01/fundos/deploy-backend.sh.RETIRED
```

### 5.2 Run the deploy

```bash
sudo /d01/fundos/deploy-backend-src.sh /tmp/fundos-backend-*.tar.gz
```

The script performs, in order:

1. venv sanity (must be Python 3.11)
2. unpack → `/d01/fundos/src.new`
3. validate `manage.py` at root
4. carry `var/` and `celerybeat-schedule` forward from the live tree
5. requirements diff → decide whether to pip
6. stop `fundos-web fundos-worker fundos-beat`
7. cut over: `src → src.previous`, `src.new → src`
8. pip (requirements changed this release, so it **will** run) / migrate / collectstatic
9. start services, health-check `https://befundos.gyain.com/api/v1/me/contexts` expecting **401**

**On a failed health check it rolls back automatically**: the bad tree is
moved to `/d01/fundos/src.failed`, `src.previous` is restored, and services
restart. The script exits non-zero and says so.

### 5.3 Repoint systemd at `/d01/fundos/src`

The units still say `/d01/fundos/current/FUNDOS/fundos-backend`. Until this
step they run the OLD tree even though `src` is populated.

```bash
sudo cp /etc/systemd/system/fundos-web.service    /root/fundos-web.service.bak
sudo cp /etc/systemd/system/fundos-worker.service /root/fundos-worker.service.bak
sudo cp /etc/systemd/system/fundos-beat.service   /root/fundos-beat.service.bak

sudo sed -i 's#^WorkingDirectory=.*#WorkingDirectory=/d01/fundos/src#' \
     /etc/systemd/system/fundos-{web,worker,beat}.service

sudo systemctl daemon-reload
systemctl cat fundos-web fundos-worker fundos-beat | grep WorkingDirectory
sudo systemctl restart fundos-web fundos-worker fundos-beat
sudo systemctl status fundos-web fundos-worker fundos-beat --no-pager
```

> Use `sed` on the live units rather than copying the repo's
> `deploy/systemd/*.service` over them. The repo copies do not carry whatever
> gunicorn log paths GATE 4 revealed, and overwriting would lose them.

### 5.4 Update nginx `/static/`

Only if nginx serves static files directly (GATE 4 / your nginx config):

```bash
sudo grep -rn "staticfiles" /etc/nginx/conf.d/
sudo sed -i 's#alias .*/staticfiles/;#alias /d01/fundos/src/staticfiles/;#' \
     /etc/nginx/conf.d/fundos-api.conf
sudo nginx -t && sudo systemctl reload nginx
curl -sI https://fundos.gyain.com/static/admin/css/base.css | head -1   # expect 200
```

### 5.5 Dependencies the pip step cannot install

```bash
# OCR engine — a system package, not a pip package.
sudo dnf install -y tesseract && tesseract --version

# Confirm the markitdown extras landed. This is the fix from §1.3.
sudo -u fundos /d01/fundos/app/venv/bin/python - <<'PY'
from markitdown import MarkItDown
import openpyxl, pptx, docx
print("markitdown converters OK — xlsx/pptx/docx readable")
PY
```

**If that import fails, financial models will silently read as empty.** Force
the install:

```bash
sudo -u fundos /d01/fundos/app/venv/bin/pip install \
     "markitdown[docx,pptx,xlsx,xls,pdf]>=0.0.1a2"
sudo systemctl restart fundos-worker
```

### 5.6 Configuration seeds — idempotent, safe to repeat

```bash
cd /d01/fundos/src
set -a; source /etc/fundos/fundos.env; set +a
V=/d01/fundos/app/venv/bin/python

sudo -E -u fundos $V manage.py seed_platform_config
sudo -E -u fundos $V manage.py seed_assessment_config --config-version 1 --activate
```

### 5.7 Sector Deal Data — **this is what makes the Deal Scorecard work**

Check first. If these are already non-zero, **skip this step**:

```bash
sudo -E -u fundos $V manage.py shell -c "
from fundos.assessment.models import SectorDealData, SectorMapping
print('SectorDealData:', SectorDealData.objects.count())
print('SectorMapping :', SectorMapping.objects.count())"
```

If they are **0**, categories E and F cannot score at all and every company's
sub-sector will fail to resolve. Load them:

```bash
sudo cp /tmp/Tyreplex_Deal_Assessment_v22.xlsx /d01/fundos/
sudo chown fundos:fundos /d01/fundos/Tyreplex_Deal_Assessment_v22.xlsx

sudo -E -u fundos $V manage.py import_sector_benchmarks \
     /d01/fundos/Tyreplex_Deal_Assessment_v22.xlsx --dry-run
```

Review the dry run — expect **18 sector rows, 50 sub-sector rows, 104 raw →
clubbed mappings**. Then run it for real (drop `--dry-run`) and confirm:

```bash
sudo -E -u fundos $V manage.py shell -c "
from fundos.assessment.models import SectorDealData, SectorMapping
print('SectorDealData:', SectorDealData.objects.count(), '(expect 68)')
print('SectorMapping :', SectorMapping.objects.count(), '(expect 104)')"
```

> **The workbook is customer data.** It is not in the repository and must not
> be committed. Keep it at `/d01/fundos/` with `fundos:fundos` ownership, or
> delete it after the import.

---

## 6. FRONTEND — fefundos

### 6.1 Transfer and extract

```powershell
scp fundos-frontend-*.tar.gz root@fefundos:/tmp/
```

```bash
sudo mkdir -p /d01/fundos-web/src
sudo tar -xzf /tmp/fundos-frontend-*.tar.gz -C /d01/fundos-web/src
sudo chown -R webdeploy:webdeploy /d01/fundos-web/src
ls /d01/fundos-web/src        # package.json, vite.config.js, src/, public/
```

### 6.2 GATE — the production API URL

**Vite bakes this in at build time.** A wrong value produces a build that
silently calls the wrong host, and the only symptom is a frontend that loads
but cannot log in.

```bash
cat /d01/fundos-web/src/.env.production
```

It must point at the public API (e.g. `https://fundos.gyain.com`), **not**
`localhost:8000`. Compare against what the currently-live build used:

```bash
cat /d01/fundos-web/releases/2026-08-13-2219/.env.production 2>/dev/null
```

Fix `/d01/fundos-web/src/.env.production` before building if they differ.

### 6.3 Build

```bash
cd /d01/fundos-web/src
sudo -u webdeploy npm ci
sudo -u webdeploy npm run build
ls -l dist/                   # index.html, assets/, brand/
```

`npm ci` reads `package-lock.json` and needs registry.npmjs.org. The
`prebuild` hook runs `scripts/check-imports.mjs`; if it fails, the build
stops — that is intentional, fix the import it names.

### 6.4 Cut over — no downtime

```bash
readlink -f /d01/fundos-web/current            # record the old target first
sudo ln -sfn /d01/fundos-web/src/dist /d01/fundos-web/current
readlink -f /d01/fundos-web/current            # expect /d01/fundos-web/src/dist
sudo nginx -t && sudo systemctl reload nginx
curl -sI https://fundos.gyain.com/ | head -1   # expect 200
```

---

## 7. VERIFICATION

### 7.1 The new code is actually running

```bash
cd /d01/fundos/src
set -a; source /etc/fundos/fundos.env; set +a
V=/d01/fundos/app/venv/bin/python

sudo -E -u fundos $V - <<'PY'
import django, os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fundos.settings.prod")
django.setup()
from fundos.assessment import integrity, phase1
from fundos.core.logging import SharedRotatingFileHandler
from django.conf import settings
print("integrity checks :", len(integrity.checks.__doc__ or "") > 0)
print("rating_gap       :", callable(phase1.rating_gap))
print("log handler      :", settings.LOGGING["handlers"]["generation_file"]["class"])
print("log delay        :", settings.LOGGING["handlers"]["generation_file"].get("delay"))
PY
```

Expected: the handler class is
`fundos.core.logging.SharedRotatingFileHandler` and `delay` is `True`.

### 7.1a The live process is on PostgreSQL — confirm, don't trust

Run this against the deployed tree. It reports what the running configuration
actually resolves to:

```bash
cd /d01/fundos/src
set -a; source /etc/fundos/fundos.env; set +a
sudo -E -u fundos /d01/fundos/app/venv/bin/python manage.py shell -c "
from django.db import connection
from django.conf import settings
db = settings.DATABASES['default']
print('settings module :', settings.SETTINGS_MODULE)
print('ENGINE          :', db['ENGINE'])
print('vendor          :', connection.vendor)
connection.ensure_connection()
with connection.cursor() as c:
    c.execute('SELECT version()')
    print('server          :', c.fetchone()[0][:60])
assert connection.vendor == 'postgresql', 'NOT POSTGRES'
print()
print('OK - production is PostgreSQL. The sqlite hook is inert here.')"
```

Expected: `vendor: postgresql` and a `PostgreSQL <version>` banner. The
`assert` fails loudly if it is anything else.

Cross-check against the running server process:

```bash
ps -ef | grep '[p]ostmaster'      # -D /d01/pgsql/data
sudo -u postgres psql -c "\l" | grep fundos
```

> Note: your `PGDATA` is `/d01/pgsql/data`. The older
> `deploy/MIGRATION_TO_D01_gyain.md` refers to `/var/lib/pgsql/data` in its
> `sed` commands — that path is stale for this host. Do not run those lines.

### 7.2 Services and API

```bash
systemctl is-active fundos-web fundos-worker fundos-beat
curl -sk -o /dev/null -w "%{http_code}\n" \
     https://befundos.gyain.com/api/v1/me/contexts        # expect 401
sudo tail -n 50 /d01/fundos/logs/celery-worker.log
journalctl -u fundos-web -n 50 --no-pager | grep -i error
```

### 7.3 End-to-end through the UI

1. Log in on `https://fundos.gyain.com`.
2. Create a company and upload a deck **and an .xlsx financial model**.
3. Watch the worker: `sudo tail -f /d01/fundos/logs/celery-worker.log`
4. Confirm the financial model is read — this is the §1.3 fix:
   ```
   native extraction of <file>.xlsx recovered N characters
   ```
   **N must be greater than 0.** If it is 0, §5.5 did not take effect.
5. When the profile completes, open **Fundraising Strategy → Deal Evaluation**.
   Expect a score and rating, not a blank panel.

### 7.4 If the Deal Scorecard is blank

Check in this order — this is the exact sequence that diagnosed it locally:

```bash
sudo -E -u fundos $V manage.py shell -c "
from fundos.core.models import GenerationJob
for j in GenerationJob.objects.filter(kind='assessment').order_by('-created_at')[:5]:
    print(j.status, j.error or '-', j.created_at)"
```

1. **A `failed` job with an `error`** explains it outright.
2. **`SectorDealData` count is 0** → §5.7 did not run.
3. **Coverage below 60%** → the response carries `scoreSuppressed: true` by
   design. Check `input_coverage` in the API response; the fix is better
   source documents, not a code change.

---

## 8. ROLLBACK

### Backend

The deploy script rolls back automatically on a failed health check. To roll
back manually **after** a successful deploy:

```bash
sudo systemctl stop fundos-web fundos-worker fundos-beat
sudo rm -rf /d01/fundos/src.bad
sudo mv /d01/fundos/src /d01/fundos/src.bad
sudo mv /d01/fundos/src.previous /d01/fundos/src
sudo systemctl start fundos-web fundos-worker fundos-beat
curl -sk -o /dev/null -w "%{http_code}\n" https://befundos.gyain.com/api/v1/me/contexts
```

To also revert the systemd change:

```bash
sudo cp /root/fundos-web.service.bak    /etc/systemd/system/fundos-web.service
sudo cp /root/fundos-worker.service.bak /etc/systemd/system/fundos-worker.service
sudo cp /root/fundos-beat.service.bak   /etc/systemd/system/fundos-beat.service
sudo systemctl daemon-reload && sudo systemctl restart fundos-web fundos-worker fundos-beat
```

**No database restore is needed** — this release applies no migrations. The
GATE 3 dump is insurance only.

Two changes are *not* undone by a code rollback, and both are additive and
harmless: the Sector Deal Data import (§5.7) and `seed_platform_config`.

### Frontend

```bash
sudo ln -sfn /d01/fundos-web/releases/2026-08-13-2219/dist /d01/fundos-web/current
sudo systemctl reload nginx
```

---

## 9. AFTER a clean run (24–48 hours later)

Only once §7 has passed and the system has been used:

```bash
# The old release tree and its stray archives — ~500 MB
du -sh /d01/fundos/releases /d01/fundos/current
sudo rm -rf /d01/fundos/current/FUNDOS/fundos-backend.tar
sudo rm -rf /d01/fundos/current/FUNDOS/fundos-backend_ORG
sudo rm -rf /d01/fundos/current/FUNDOS/fundos-backend_20AUG.tar.gz

# Keep src.previous until you are confident; then:
sudo rm -rf /d01/fundos/src.previous /d01/fundos/src.failed
```

Leave `/d01/fundos/current` and `/d01/fundos/releases` in place until you are
satisfied. They cost disk, not correctness.

---

## 10. Known issues carried into this release

| Issue | Impact | Status |
|---|---|---|
| `material_critique` LLM role fails: `missing required key 'detected_type'` | Uploaded documents are not critiqued. Profile generation is unaffected. | **Open**, not addressed here |
| Coverage counts category G (Mandate Context) | G is advisor-internal, so coverage reads lower than it should | **Open**, by design pending a product decision |
| `DiligenceFindingsView` / `BandAdvancementView` lowercase-band handling | Two older endpoints, not used by Phase 1 | **Open**, deliberately untouched |
| Gemini free-tier quota | 100 research questions can exhaust it mid-run; symptom is 429s | Environmental |

---

## 11. Quick reference

| What | Where |
|---|---|
| Backend code | `/d01/fundos/src` |
| Rollback tree | `/d01/fundos/src.previous` |
| Virtualenv (3.11) | `/d01/fundos/app/venv` |
| Environment | `/etc/fundos/fundos.env` |
| Celery logs | `/d01/fundos/logs/celery-{worker,beat}.log` |
| Deploy script | `/d01/fundos/deploy-backend-src.sh` |
| Restart helper | `/d01/fundos/restart-fundos.sh` (unchanged, still works) |
| Frontend source | `/d01/fundos-web/src` |
| Frontend served | `/d01/fundos-web/current` → `/d01/fundos-web/src/dist` |
| Health check | `https://befundos.gyain.com/api/v1/me/contexts` → **401** |
