# FundOS backend — documentation

Four kinds of document live here. Which one you want depends on what you are
about to do.

| You are… | Read |
|---|---|
| building a client against the API | [`../API_CONTRACT.md`](../API_CONTRACT.md) — the authoritative request/response contract |
| deploying or operating a server | [`operations/`](operations/) |
| an administrator configuring behaviour without a deploy | [`configuration/`](configuration/) |
| running or extending the test suite | [`testing/`](testing/) |
| trying to understand why some code is the way it is | [`changelog/`](changelog/) |

## changelog/

One file per delivered change, newest concepts generally in the
higher-numbered files. These are **not** release notes. Each one states the
defect that motivated the change, why the obvious fix was wrong, and what is
now asserted in the test suite — so a comment in the code saying "see
CHANGES_v29 §7" resolves to a real argument rather than a version tag.

The ones worth reading first:

| File | Subject |
|---|---|
| [`CHANGES_profile_data_integrity.md`](changelog/CHANGES_profile_data_integrity.md) | the most recent pass: what a real generated profile got wrong, and the boundary that now types every model-authored field |
| [`CHANGES_production_hardening.md`](changelog/CHANGES_production_hardening.md) | signing keys, leaked file handles, honest diagnostics, repository layout |
| [`CHANGES_company_profile_pipeline.md`](changelog/CHANGES_company_profile_pipeline.md) | the current Company Profile generator: two sources, one dossier, one synthesis call |
| [`CHANGES_v29.md`](changelog/CHANGES_v29.md) | LLM tier policy, and the search-vs-schema conflict the profile pipeline resolves |
| [`CHANGES_llm_cost_optimisation.md`](changelog/CHANGES_llm_cost_optimisation.md) | where the money goes and the four levers that control it |
| [`CHANGES_grounding_v20.md`](changelog/CHANGES_grounding_v20.md) | why an ungrounded generation is refused rather than published |
| [`CHANGES_v24.md`](changelog/CHANGES_v24.md) | the assessment workbook as configuration, not as a data file |
| [`GAP_CLOSURE_REPORT.md`](changelog/GAP_CLOSURE_REPORT.md) | the G1–G15 gap analysis and how each was closed |

## operations/

| File | Subject |
|---|---|
| [`DEPLOYMENT_v29.md`](operations/DEPLOYMENT_v29.md) | current deployment procedure |
| [`DEPLOYMENT_v28.md`](operations/DEPLOYMENT_v28.md) | the preceding one, kept for the migration path |
| [`DEPLOY_AND_INGEST_v24.md`](operations/DEPLOY_AND_INGEST_v24.md) | importing the assessment workbook and sector benchmark data |
| [`DEPLOY_AND_TEST_v23.md`](operations/DEPLOY_AND_TEST_v23.md) | the full deploy-then-verify walkthrough |
| [`RUNBOOK_v27_4_verification.md`](operations/RUNBOOK_v27_4_verification.md) | verifying a live generation run end to end |
| [`DIAGNOSTIC_LOGGING.md`](operations/DIAGNOSTIC_LOGGING.md) | what is logged where, and how to read it |
| [`../deploy/MIGRATION_TO_D01_gyain.md`](../deploy/MIGRATION_TO_D01_gyain.md) | host provisioning, including the Tesseract system package |

## configuration/

Almost everything is administrator-editable at runtime through Django admin;
code ships defaults and a seeded database row wins. These describe what is
editable and what each setting does.

| File | Subject |
|---|---|
| [`ADMIN_CONFIGURATION.md`](configuration/ADMIN_CONFIGURATION.md) | the full admin surface |
| [`ADMIN_CONFIG_v29.md`](configuration/ADMIN_CONFIG_v29.md) | LLM roles, tiers, config profiles and prompts |
| [`ADMIN_CONFIG_v28.md`](configuration/ADMIN_CONFIG_v28.md) | the preceding revision |
| [`CONFIGURATION_v27.md`](configuration/CONFIGURATION_v27.md) | environment variables and settings modules |
| [`CONFIGURATION_v27.2.md`](configuration/CONFIGURATION_v27.2.md) | additions to the above |

## testing/

| File | Subject |
|---|---|
| [`COMPANY_PROFILE_E2E_WALKTHROUGH.md`](testing/COMPANY_PROFILE_E2E_WALKTHROUGH.md) | the exact call sequence for signup → generated profile, verified against a live server — start here if you're building the frontend for this flow |
| [`TESTING_v29.md`](testing/TESTING_v29.md) | what the suite covers and how to run one slice of it |
| [`TESTING_v28.md`](testing/TESTING_v28.md) | the preceding revision |
