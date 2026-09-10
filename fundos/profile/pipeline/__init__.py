"""Company Master Data Pipeline — the Company Profile generator.

Builds one company profile from two concurrent sources and emits two
deliverables:

* ``consolidated.md`` — the merged markdown dossier (the EVIDENCE)
* the 17-section structured profile (the CONCLUSION)

The split matters. Retrieval and structuring are separate calls over separate
inputs: the research pass searches the web and needs no output schema, and the
synthesis pass reads only the dossier and needs no search.
`docs/changelog/CHANGES_v29.md` §7 names that split as the correct fix for the
tier enum bundling "needs to search" with "needs a guaranteed shape" and
forcing every search role onto the losing side; this package is that fix.

Entry point: :func:`fundos.profile.pipeline.orchestrator.run_pipeline`, called
by the ``profile.generate`` Celery task.

Everything an operator would want to change — the prompts, the 100 research
questions, the section field specs, the model, the concurrency, the OCR
policy — lives in an admin table, not in this package. See
:mod:`fundos.profile.pipeline.settings`.
"""
