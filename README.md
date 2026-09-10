# FundOS backend

Django + DRF backend for the FundOS guided fundraising programme — Stage 1
(Investor Readiness) through Stage 3 (Deal Creation & Investor Materials).
Multi-tenant, Celery-backed, with an LLM layer whose model, prompts and output
shapes are administrator-editable at runtime.

API base path: `/api/v1`. The authoritative request/response contract is
[`API_CONTRACT.md`](API_CONTRACT.md).

## Run it

```bash
python -m venv .venv && .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export DJANGO_SETTINGS_MODULE=fundos.settings.dev
python manage.py migrate
python manage.py seed_initial_data              # tenants, roles, masterplan
python manage.py seed_platform_config           # profile sections, research bank, prompts
python manage.py seed_llm_costs                 # provider price list
python manage.py seed_assessment_config --activate
python manage.py runserver
```

Dev settings use SQLite, local file storage, console email and
`CELERY_TASK_ALWAYS_EAGER`, so generation jobs finish inside the 202 response
and no broker is needed. They also default `ai_mocked` on: the LLM layer
returns fixtures until you turn it off in admin and supply a provider key.

Settings modules: `fundos.settings.dev` | `.uat` | `.prod`. Deployment,
including the Tesseract system package the document pipeline uses for OCR, is
in [`docs/operations/`](docs/operations/).

## Tests

```bash
python manage.py test tests                     # the whole suite
python manage.py test tests.api.test_profile_pipeline
```

The suite runs against SQLite with no network and no provider key. Two slices
need a file that is customer data and therefore is not committed — point
`FUNDOS_ASSESSMENT_WORKBOOK` at a filled assessment workbook to run them; they
skip cleanly without it, and the offline seeder ships the same scoring model.

## Layout

```
fundos/
  core/         tenancy, auth, users, deals, jobs, notifications, alerting
  config/       AppConfiguration singleton + feature flags (admin-editable)
  platformcfg/  seeded platform tables: profile sections, research bank, prompts
  profile/      company profile — sections, records, and the generation pipeline
  assessment/   the deal scoring model, read from the workbook as configuration
  readiness/    Stage 1: materials, extraction, gap analysis, readiness score
  strategy/     Stage 2: objectives, peers, raise, valuation, instruments
  engines/      the deterministic calculation engines the above call
  exports/      PDF / PPTX / DOCX / XLSX generation
  research/     external research and enrichment
  investors/    investor database, matching, refresh pipeline
  llm/          provider adapter, roles, config profiles, cost ledger, budgets
  docs/         document storage abstraction (local | S3 | OCI | Azure)
  diagnostics/  operator-facing health and configuration checks
  settings/     base | dev | uat | prod
tests/          one module per delivered change; each test names the defect it pins
deploy/         nginx, systemd, env template, host migration guide
docs/           changelog, operations, configuration, testing — see docs/README.md
```

## Two things that explain most of the design

**Almost nothing is hardcoded that an operator might need to change.** Prompts,
LLM model bindings, the 100 research questions, profile section field specs,
scoring rubrics, feature flags and pipeline concurrency all live in database
tables edited through Django admin. Code ships a default and a seeded row wins,
so an unseeded install still works and a configuration change does not need a
deploy. [`docs/configuration/`](docs/configuration/) is the map.

**An ungrounded generation is refused, not published.** The company profile
pipeline retrieves evidence first — search-grounded research plus text
extracted from uploaded documents — into a single markdown dossier, and only
then asks a model to structure it. If nothing was retrieved, the run stops and
says so rather than emitting a plausible profile assembled from the model's
recollection. See
[`docs/changelog/CHANGES_company_profile_pipeline.md`](docs/changelog/CHANGES_company_profile_pipeline.md).
