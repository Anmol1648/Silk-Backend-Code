"""The final LLM call: consolidated markdown in, structured profile JSON out.

This is the only call in the pipeline that returns JSON, and the only one that
reads the dossier. That separation is the point of the whole architecture:
retrieval and structuring have opposite requirements — retrieval needs web
search and cannot accept a response schema, structuring needs a guaranteed
shape and must not search — and bundling them into one call is what forced
every search-capable role in the previous design to give up its schema
(`docs/changelog/CHANGES_v29.md` §7).

This module owns the *after*: turning whatever JSON came back into a
guaranteed-complete, schema-shaped profile, regardless of how loosely the
response matched. By the time :func:`run` has a dict in hand, the adapter has
already parsed, repaired and validated it; this module's job is to make it
trustworthy to write.
"""
import logging

from fundos.profile import schema
from fundos.profile.pipeline import dossier as dossier_module
from fundos.profile.pipeline import settings as pipeline_settings

logger = logging.getLogger("fundos.profile")

ROLE = "profile_synthesis"

MAX_RECORDED_CORRECTIONS = 200
"""Cap on corrections stored on the run row.

A pathological response can generate one per field across every section, and
`progress` is a JSON column on a row an operator loads in admin. The count is
always exact; only the listing is truncated, because the number tells you
whether to look and the first two hundred lines tell you what at."""


MAX_SYNTHESIS_ROUNDS = 3
"""How many times to ask for the sections still missing.

WHY THIS EXISTS
---------------
One call had to emit all seventeen sections, and a response is capped by the
model's output ceiling -- 65,536 tokens on gemini-2.5-flash, whatever the
role's configured budget says. Past that the JSON is cut mid-object, the
adapter salvages a valid prefix, and the run reports success. On 08 Sep that
produced a profile with two populated sections out of seventeen: the model
spent its whole budget on sections one and two, and three to seventeen were
never emitted. Nothing downstream could tell, because a salvaged prefix is
still valid JSON.

Raising the ceiling does not fix it -- the ceiling belongs to the model, and
the configured 98,304 was already above what it can emit. Splitting the
sections across concurrent calls does not fix it either, at acceptable cost:
the dossier is ~300,000 tokens and rides in the user prompt, so every extra
call re-sends it and a four-way split nearly quadruples the bill.

Asking again only for what is missing scales the OUTPUT, which is the side
that is actually bounded, and pays for input only when something was lost. A
complete first response costs exactly what it costs today.
"""

MIN_ROUND_GAIN = 1
"""Stop when a round adds fewer sections than this.

A section can be legitimately empty -- the dossier genuinely says nothing
about the cap table of a company that has never raised. Asking a second time
is worth one call, because a truncated section and an unevidenced one look
identical from here. Asking a third time after the second added nothing is
just paying to be told the same thing.
"""


class EmptyDossier(RuntimeError):
    """Raised when there is nothing to synthesize from.

    A distinct type because the remedy is distinct: an empty dossier is a
    *sourcing* failure, and sending it to the model anyway would produce a
    plausible-looking, entirely unfounded profile — precisely what this
    pipeline exists to prevent.
    """


def _truncate(dossier, limit):
    """Cap the dossier, cutting at a section boundary where possible.

    The cut prefers the last ``---`` rule before the limit so the model is
    never handed a document ending mid-sentence, and it says in the text that
    it was truncated, so a thin profile traced back to a truncated dossier
    explains itself.
    """
    if len(dossier) <= limit:
        return dossier, False
    head = dossier[:limit]
    boundary = head.rfind("\n\n---\n\n")
    if boundary > limit // 2:
        head = head[:boundary]
    note = (f"\n\n---\n\n_This dossier was truncated at {len(head):,} of "
            f"{len(dossier):,} characters to stay inside the configured "
            f"limit (Application configuration -> max dossier chars). "
            f"Sections after this point "
            f"were not sent to the model._\n")
    return head + note, True


def _ask(sections, dossier, *, company_name, website, user=None,
         source_labels=None, document_count=0):
    """One synthesis call, for whichever sections are asked for.

    Takes the section list rather than reading it from the schema, so a
    continuation round can request only what is still missing. Both the schema
    block and the key list narrow together, because they come from the same
    argument -- there is no second place to keep in step.
    """
    from fundos.llm.adapter import llm_generate

    keys = schema.promptable_keys(sections)
    return llm_generate(
        role=ROLE,
        context={
            "company": {"name": company_name, "website": website},
            "company_name": company_name,
            "website": website,
        },
        section_context={
            "company_name": company_name,
            "website": website or "",
            "section_keys": "\n".join(f"  - {key}" for key in keys),
            "schema_block": schema.schema_prompt_block(
                sections, source_labels=source_labels,
                document_count=document_count),
            "dossier": dossier,
        },
        config_profile=pipeline_settings.synthesis_config_profile(),
        calling_context="profile.synthesis",
        user=user,
    )


def _populated(profile):
    """The section keys that actually carry data."""
    return [key for key, section in profile["sections"].items()
            if section.get("isComplete")]


def _unpopulated(profile, sections):
    """The sections the model was asked for and did not fill.

    Pipeline-owned sections are excluded: the Document Center is written from
    our own records and is never the model's to populate, so counting it as
    missing would make every run look like it lost a section.
    """
    owned = getattr(schema, "PIPELINE_OWNED_SECTIONS", set())
    return [s for s in sections
            if s["key"] not in owned
            and not (profile["sections"].get(s["key"]) or {}).get("isComplete")]


def _citation_summary(profile, document_labels):
    """How many values ended up cited, and to what.

    Recorded on the run because it is the one number that answers "did the
    deck we uploaded actually get used?". A run whose citations are all web
    research for a company that supplied an investor deck is a bad run even
    when every section is populated, and nothing else in the summary says so.
    """
    documents = set(document_labels)
    cited = to_documents = 0
    for section in (profile.get("sections") or {}).values():
        for citation in (section.get("sources") or {}).values():
            if not (citation or {}).get("source"):
                continue
            cited += 1
            to_documents += int(citation["source"] in documents)
    return {"cited": cited,
            "to_documents": to_documents,
            "to_research": cited - to_documents}


def run(run_row, *, company_name, website, dossier, documents, user=None):
    """Synthesize the structured profile from the dossier.

    Called once per run by the orchestrator, after both sources have finished
    and the dossier is final. Sequential, not concurrent with anything: it
    depends on the complete dossier as its sole input.

    :param dossier: The full consolidated markdown.
    :param documents: The run's document rows, used to overwrite section 8.17
        with the pipeline's own bookkeeping rather than the model's account of
        what documents existed.
    :returns: ``(profile, summary)`` — the normalized 17-section profile, and a
        progress record for the run row.

    This call does ONE job. Folding the sixty-one assessment parameters into
    the same response, to save a second pass over the dossier, took a live run
    from 17 populated sections to 3: the model was asked for the narrative and
    a precision extraction with opposite rules in one output, against a
    ceiling that already documents two runs lost to truncation. The parameters
    are extracted separately, by ``profile.assessment_extraction``.
    :raises EmptyDossier: If the dossier carries no content.
    """
    if not (dossier or "").strip():
        raise EmptyDossier(
            "The consolidated dossier is empty, so there is nothing to build a "
            "profile from. Every research batch failed and no document "
            "yielded text.")

    active = schema.sections()
    keys = schema.promptable_keys(active)
    text, truncated = _truncate(dossier,
                                pipeline_settings.max_dossier_chars())

    logger.info("PIPELINE: synthesizing %s from %d characters of dossier "
                "across %d sections", company_name, len(text), len(keys))

    # The exact strings a citation may name, uploaded documents first. The
    # ordering IS the instruction: the company's own deck and financial model
    # are what a reader trusts, and a search result repeating the same fact is
    # weaker evidence for the same claim.
    labels, document_count = dossier_module.source_labels(text)
    logger.info("PIPELINE: %d citable source(s) for %s, %d of them uploaded "
                "documents", len(labels), company_name, document_count)

    raw = _ask(active, text, company_name=company_name, website=website,
               user=user, source_labels=labels,
               document_count=document_count)

    # Normalize first so a partial or loosely-shaped response still yields
    # every section key, then overwrite 8.17 from our own records.
    #
    # `notes` collects every correction the boundary made — markup stripped, a
    # multi-term value narrowed for benchmark matching, an expiring redirect
    # dropped from a citation. They are carried onto the run rather than
    # logged and forgotten: each one is a place the model's output and the
    # contract disagreed, which is the earliest signal that a prompt or a field
    # spec needs changing, and it is invisible from the stored profile because
    # by then the value looks correct.
    notes = []
    profile = schema.normalize_profile(raw, active, notes=notes,
                                       allowed_sources=labels)

    # --- continuation rounds ------------------------------------------------
    #
    # A response cut off at the model's output ceiling comes back as a valid
    # JSON prefix, so the sections after the cut are indistinguishable from
    # sections the dossier had nothing to say about. Both look empty, and both
    # have the same remedy: ask again, for those sections only.
    #
    # Narrowing the request is what makes this work. The second call carries
    # the same dossier but is asked for four sections instead of seventeen, so
    # its output is a fraction of the ceiling and it finishes what the first
    # one started. It stops as soon as a round adds nothing, which is the
    # signal that what remains is genuinely unevidenced rather than truncated.
    rounds = [{"asked": len(schema.promptable_keys(active)),
               "filled": len(_populated(profile))}]
    for round_no in range(2, MAX_SYNTHESIS_ROUNDS + 1):
        missing = _unpopulated(profile, active)
        if not missing:
            break

        logger.info(
            "PIPELINE: synthesis round %d for %s — asking again for %d "
            "section(s) the first response did not fill: %s",
            round_no, company_name, len(missing),
            ", ".join(s["key"] for s in missing))

        try:
            more = _ask(missing, text, company_name=company_name,
                        website=website, user=user, source_labels=labels,
                        document_count=document_count)
        except Exception as exc:  # noqa: BLE001 - a top-up, not a dependency
            logger.warning(
                "PIPELINE: synthesis round %d failed (%s). Keeping what the "
                "earlier round(s) produced.", round_no, exc)
            rounds.append({"round": round_no, "asked": len(missing),
                           "gained": 0, "error": str(exc)[:200]})
            break

        filled = schema.normalize_profile(more, missing, notes=notes,
                                          allowed_sources=labels)
        gained = []
        for section in missing:
            key = section["key"]
            candidate = filled["sections"].get(key) or {}
            if candidate.get("isComplete"):
                profile["sections"][key] = candidate
                gained.append(key)

        rounds.append({"round": round_no, "asked": len(missing),
                       "gained": len(gained), "keys": gained})
        logger.info("PIPELINE: synthesis round %d recovered %d section(s): %s",
                    round_no, len(gained), ", ".join(gained) or "none")

        # Nothing new means the remainder is unevidenced, not truncated.
        # Asking a third time would buy the same answer at the same price.
        if len(gained) < MIN_ROUND_GAIN:
            break

    profile = schema.apply_document_center(profile, documents)

    populated = _populated(profile)
    summary = {
        "sections_total": len(profile["sections"]),
        "sections_populated": len(populated),
        "sections_empty": sorted(set(profile["sections"]) - set(populated)),
        "dossier_chars": len(text),
        "dossier_truncated": truncated,
        "corrections": notes[:MAX_RECORDED_CORRECTIONS],
        "corrections_total": len(notes),
        "rounds": rounds,
        "recovered_by_continuation": sum(r.get("gained", 0) for r in rounds[1:]),
        "citations": _citation_summary(profile, labels[:document_count]),
    }
    if notes:
        logger.info("PIPELINE: normalization corrected %d field(s) on the "
                    "synthesis response for %s", len(notes), company_name)
    recovered = sum(r.get("gained", 0) for r in rounds[1:])
    logger.info(
        "PIPELINE: synthesis complete — %d of %d sections populated in %d "
        "call(s)%s",
        len(populated), len(profile["sections"]), len(rounds),
        f", {recovered} recovered after the first" if recovered else "")
    return profile, summary
