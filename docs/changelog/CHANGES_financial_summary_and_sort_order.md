# financial_summary structure + me/contexts sort order (Aug 2026)

Addresses issue-financial-summary-and-sort-order.docx — two issues. Code-only,
no migration required.

## Issue 1 — financial_summary must send field structure even with no data

When financial_summary has no data, the response now sends a self-describing
placeholder instead of a bare empty array:

    "data": {
      "financials": [
        { "year": null, "revenue_m": null, "ebitda_m": null, "growth_pct": null }
      ],
      "observations": []
    }

Details:
* The financials field names are now the section's documented schema —
  `year`, `revenue_m`, `ebitda_m`, `growth_pct` (previously financial_year /
  revenue / ebitda / pat). Generation and PATCH read/write the same keys, and
  legacy stored keys are still read via fallback so nothing breaks.
* observations are plain strings (e.g. ["Path to profitability ..."]).
* The null placeholder does NOT count as data: isComplete stays false, the
  completeness score is unaffected, and PATCHing back an untouched placeholder
  does not mark the section complete (the writer drops all-null rows).
* Mirrors how business_model already sends its full key set when empty.

## Issue 2 — GET /me/contexts default order + sort query params

Default order is now **newest company first**, enforced explicitly by an
ORDER BY on creation date (descending) rather than relying on membership
insertion order (which produced newest-last).

Sort is now client-controllable:

    GET /api/v1/me/contexts?sortBy=<field>&order=<direction>

* sortBy (case-insensitive): `createdAt` (default), `companyName`, `sector`.
  Aliases accepted: `name` → companyName, `date` → createdAt.
* order (case-insensitive): `desc` (default) or `asc`.
* Defaults: sortBy=createdAt, order=desc → newest first.
* Robustness: unknown sortBy/order fall back to the defaults (never errors);
  rows missing the sort value always sort to the END regardless of direction;
  internal sort-only keys are stripped so the response shape is unchanged.

### Answers to the backend questions in the doc
* Param name for sort field: `sortBy`.
* Param name for direction: `order`.
* Accepted sort fields: `createdAt`, `companyName`, `sector` (+ aliases
  `name`, `date`).
* Accepted direction values: `asc` / `desc` (case-insensitive).
* Default when no params: newest first (createdAt desc) — fixed and confirmed.

## Tests

`tests/api/test_spec_contract.py`:
  * `test_financial_summary_empty_sends_field_template`
  * `test_contexts_default_newest_first_and_sort_params`
Full suite green: 232 tests pass.
