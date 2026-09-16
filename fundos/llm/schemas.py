"""
Per-role LLM response schemas (Doc 5 Annexure C contract / Doc 8 §5).

PURE module — no Django imports. Validation is shape-level: required keys,
types, and the numeric-authority prohibition (schemas forbid fields where the
LLM could assert numbers the engines own — scores, valuations, dilution
percentages arrive from [DET] engines only; the LLM explains them).
"""

# Keys the LLM is NEVER allowed to return (numeric authority stays [DET]).
FORBIDDEN_NUMERIC_AUTHORITY = {
    "overall_score", "score", "valuation", "valuation_low", "valuation_high",
    "dilution_pct", "similarity_score", "recommended_amount",
}

SCHEMAS = {
    "research_synthesis": {
        "required": {"fields": list},
        # fields: [{fieldKey, value, factOrInference, evidence, confidence}]
    },
    # `detected_type` was required, and the shipped prompt never asked for it
    # — it asked for `status`, which is not in this schema at all. So EVERY
    # call failed validation with "missing required key 'detected_type'", and
    # the whole result was discarded: the summary, the extracted metrics and
    # the recommendations along with the missing label.
    #
    # That had a consequence far from here. `MaterialAsset.summary` and
    # `.extracted_metrics` stayed empty, and those two fields are the ONLY
    # thing `profile.services._document_extracts()` passes to the assessment
    # extraction. So an uploaded deck reached the Deal Scorecard as an empty
    # row, the scorecard scored ~10 of 61 parameters, and the screen read
    # "the AI ignored my documents" — which it had, for a reason no message
    # anywhere connected back to a missing string field.
    #
    # The prompt now asks for `detected_type` (see default_prompts), and it is
    # optional here because the consumer already treats it that way:
    # `readiness/tasks.py` reads `data.get("detected_type") or ""`. A label
    # the caller tolerates the absence of must not be able to discard the
    # substance beside it.
    "material_critique": {
        "required": {"summary": str, "recommendations": list},
        "optional": {"detected_type": str, "status": str,
                     "extracted_metrics": list, "inconsistencies": list,
                     "missing_information": list},
    },
    "readiness_summary": {
        "required": {"summary": str, "dimension_explanations": dict},
    },
    "peer_insight": {
        "required": {"insights": list},
        # insights: [{text, kind: fact|inference, evidence}]
    },
    "raise_narrative": {
        "required": {"narrative": str},
        "optional": {"facts": list, "inferences": list, "assumptions": list},
    },
    "valuation_negotiation": {
        "required": {"supporting_arguments": list, "investor_objections": list,
                     "suggested_responses": list},
        "optional": {"positioning_statement": str},
    },
    "instrument_explainer": {
        "required": {"instruments": list},
        # instruments: [{instrument, pros, cons, mechanics_explainer,
        #                market_context, applicability_note}]
    },
    "strategy_blueprint": {
        "required": {"executive_summary": str, "why_this_strategy": str},
        "optional": {"risks": list, "success_factors": list,
                     "next_milestones": list, "current_position": dict},
    },
    "investment_story": {
        "required": {"components": list, "positioning_options": list,
                     "thesis_primary": str},
        "optional": {"message_blocks": list},
    },
    "teaser": {
        "required": {"headline": str, "sections": list},
        "optional": {"thesis": str, "tagline": str, "suggestions": list},
    },
    "pitch_story": {
        "required": {"narrative_outline": list},
    },
    "pitch_slides": {
        "required": {"slides": list},
    },
    "fin_model_assist": {
        "required": {"assumptions": list},
        "optional": {"explanation": str, "impact_explanation": str},
    },
    "fin_review": {
        "required": {"findings": list},
    },
    "im_section": {
        "required": {"sections": list},
    },
    "im_narrative": {
        "required": {"sections": list},
    },
    "im_consistency": {
        "required": {"flags": list},
    },
    "package_review": {
        "required": {"category_notes": dict, "recommendations": list},
        "optional": {"risk_summary": list, "outstanding_actions": list},
    },
    "objection_sim": {
        "required": {"objections": list},
        # objections: [{investor_type, question, reason, supporting_data,
        #               suggested_response, evidence, confidence, priority}]
    },
    # --- Company Profile deep extract + Q&A + field regen ---
    "company_profile_deep_extract": {
        # Large tolerant dossier object; the prompt + JSON parse guard shape.
        "optional": {"needsInput": bool, "missing": list, "conflicts": list},
    },
    "company_profile_judgment": {
        # Stage 2 of the dossier: judgement over stage 1's extraction.
        "required": {"investment_thesis": dict},
        "optional": {"conflict_resolution": list, "data_quality_notes": list,
                     "needsInput": bool, "missing": list},
    },
    "profile_qa": {
        "required": {"answer": str, "proposedChanges": list},
        "optional": {"citations": list, "answered": bool, "missing": list},
    },
    "company_profile_field": {
        "optional": {"fieldKey": str, "needsInput": bool},
    },
}


class SchemaValidationError(ValueError):
    pass


SCHEMAS["assessment_inputs"] = {
    # values: [{inputKey, value, unit, sourceUrl, sourceDoc, sourceTier,
    #           confidence, justification}]
    # bands:  [{inputKey, band, confidence, justification, sourceUrl,
    #           sourceDoc}]
    #
    # `sourceDoc` names an uploaded document and a locator within it
    # ("Financial Model.xlsx, P&L tab"). It is accepted in place of a URL by
    # `assessment_extraction._citation`, because the company's own financial
    # model — the most authoritative source in the pack — has no URL to give.
    # Nothing here enforces either field: the citation rule lives in
    # `_citation`, which sees the whole row and can accept either form.
    #
    # `values` is required and `bands` is not, because a company with no
    # qualitative evidence at all is a real and correct outcome, whereas a
    # response carrying neither key is a malformed one worth failing loudly.
    #
    # Note the absence of any score key: this role reports FACTS. Bands for
    # anchor rows are proposals re-validated against the stored definitions,
    # and FORBIDDEN_NUMERIC_AUTHORITY strips `score` if a model volunteers it.
    "required": {"values": list},
    "optional": {"bands": list, "sector": str, "subSector": str,
                 "missing": list},
}


SCHEMAS["profile_synthesis"] = {
    # The whole profile arrives under one key. Requiring it is a real check —
    # a response with no `sections` is malformed and worth failing on — but it
    # is deliberately the ONLY check here.
    #
    # Everything below `sections` is validated by
    # `fundos.profile.schema.normalize_profile`, which coerces rather than
    # rejects: a flattened wrapper, an omitted section or an array where an
    # object was asked for are all recoverable shape slips, and failing the
    # whole call over one of them would discard 16 correct sections to punish
    # the 17th. A type check cannot make that distinction; the normalizer can.
    "required": {"sections": dict},
}


def validate(role: str, data) -> dict:
    """Validate a parsed LLM response against the role's schema.

    Raises SchemaValidationError on hard failures; strips any forbidden
    numeric-authority keys silently (defence in depth).
    """
    if not isinstance(data, dict):
        raise SchemaValidationError(f"{role}: response is not a JSON object")

    schema = SCHEMAS.get(role)
    if schema:
        for key, typ in schema.get("required", {}).items():
            if key not in data:
                raise SchemaValidationError(f"{role}: missing required key '{key}'")
            if not isinstance(data[key], typ):
                raise SchemaValidationError(
                    f"{role}: key '{key}' must be {typ.__name__}")

    for forbidden in FORBIDDEN_NUMERIC_AUTHORITY:
        data.pop(forbidden, None)
    return data


# ---------------------------------------------------------------------------
# JSON Schema projection (v29)
# ---------------------------------------------------------------------------
# Roles whose `SCHEMAS` entry is the COMPLETE response shape, verified against
# the consumer that reads it — not merely the subset `validate()` enforces.
#
# Only these get a derived `responseSchema`. See `json_schema_for` for why the
# distinction is load-bearing: a prescriptive schema built from a partial
# validation contract silently truncates the response to the declared keys.
#
# Verified for v29:
#   readiness_summary  fundos/readiness/services.py:193 — the prompt asks for
#                      exactly {summary, dimension_explanations} and lines
#                      205ff read exactly those two keys. Complete.
#   peer_insight       fundos/strategy/services.py:241 — the prompt asks for
#                      {"insights": [...]} and line 248 reads only `insights`.
#                      Complete.
#
# NOT added, and why:
#   company_profile_deep_extract  244 output fields, 3 declared. Would be
#                                 catastrophic — see json_schema_for.
#   assessment_inputs             `values` carries 16-30 heterogeneous
#                                 records; the item shape is not pinned here
#                                 and a bare ARRAY of empty OBJECTs risks the
#                                 provider emitting empty objects.
#   company_profile_judgment      `investment_thesis` is a nested OBJECT whose
#                                 inner shape lives in the prompt, not here.
#   profile_synthesis             Its contract here is `{"sections": dict}` —
#                                 one key. `json_schema_for` projects a FLAT
#                                 object, so it would send Gemini a schema
#                                 saying "return an object with one untyped
#                                 property", which describes the response
#                                 without constraining any of it: all cost, no
#                                 guarantee. The real shape is three levels
#                                 deep and is single-sourced in
#                                 `fundos.profile.schema`, which renders it
#                                 into the prompt as explicit field
#                                 specifications and enforces it on the way
#                                 back. That is a better guarantee than a
#                                 responseSchema, not a weaker one, because it
#                                 coerces near-misses instead of rejecting
#                                 them.
#
# Adding a role means reading its consumer first. The cost of being wrong is
# a silently thinner response that parses cleanly and logs as healthy.
SCHEMA_IS_COMPLETE_OUTPUT = {
    "readiness_summary",
    "peer_insight",
}


_JSON_TYPES = {
    str: "STRING",
    int: "INTEGER",
    float: "NUMBER",
    bool: "BOOLEAN",
    list: "ARRAY",
    dict: "OBJECT",
}


def json_schema_for(role):
    """`SCHEMAS[role]` projected into a provider-ready JSON Schema, or None.

    WHY THIS EXISTS
    ---------------
    `LLMConfigProfile.output_schema` is a JSONField defaulting to `{}`, and
    NOTHING in the codebase has ever written to it — not the seeder, not a
    migration, not the adapter. Only a human editing Django admin could
    populate it, and on a real install nobody had.

    The consequence ran through six releases unnoticed. `capabilities_payload`
    emits `{"structured_output": {"schema": {}}}`, and every provider path
    guards with `if so and so.get("schema")` — `{}` is falsy — so
    `responseSchema` was NEVER sent to Gemini, on any tier, for any role. The
    `structured_output` tick did nothing at all.

    That matters because v27.5 and v28 were both built on the finding that a
    responseSchema suppresses `google_search`. The finding is real — the
    diagnostic script sends a genuine schema — but it could not explain
    anything in the 19 August logs, because FundOS was not sending one. Both
    `company_profile_deep_extract` (0 searches) and `company_profile_section`
    (2-6 searches) ran with identical capabilities on the same profile. The
    schema was never the variable that separated them.

    So there were two defensible ways forward: delete the toggle, or make it
    real. Deleting it loses genuine value on the SIMPLE and JUDGMENT tiers,
    where a guaranteed response shape removes the repair call entirely. This
    makes it real.

    WHY IT IS DERIVED FROM THE ROLE, NOT STORED ON THE PROFILE
    ---------------------------------------------------------
    A schema is a property of a ROLE's contract. A config profile is a TIER
    setting shared by many roles — `tier.simple.gemini` serves eleven of
    them. There is no single schema that could correctly sit in that row's
    `output_schema`, which is very likely why the field was never populated:
    it was architecturally in the wrong place. The role already declares its
    shape here, so that is what gets sent.

    An explicit `output_schema` on the profile still wins when an
    administrator has set one; this only fills the blank.

    DELIBERATELY PERMISSIVE
    -----------------------
    Only REQUIRED keys are marked required, optional keys are advertised but
    not enforced, and no nested item shapes are asserted. An over-tight
    responseSchema turns a recoverable formatting drift into a hard provider
    error, which is a worse failure than the repair call it saves.

    AND DELIBERATELY OPT-IN — READ THIS BEFORE ADDING A ROLE
    -------------------------------------------------------
    `SCHEMAS` is a VALIDATION contract: the keys `validate()` insists on. It
    is NOT a description of the whole response. For several roles the two are
    wildly different, and a `responseSchema` is PRESCRIPTIVE — the model
    returns the declared properties and nothing else.

    The worked example, which is why this function is gated:

        SCHEMAS["company_profile_deep_extract"]
            {"optional": {"needsInput", "missing", "conflicts"}}

        the same call on 19 Aug
            populated_fields=235  total_fields=244

    Deriving a schema there would send Gemini a three-property object and
    reduce the entire dossier to three housekeeping keys — silently, with a
    valid parse and a healthy-looking log. That is a far worse outcome than
    the inert tick this function was written to fix.

    So a role gets a derived schema only by appearing in
    `SCHEMA_IS_COMPLETE_OUTPUT` below, which is an assertion by a human that
    the validation contract IS the full output shape for that role. Roles are
    added after reading their consumer, not by inspection of this file.
    """
    if role not in SCHEMA_IS_COMPLETE_OUTPUT:
        return None
    spec = SCHEMAS.get(role)
    if not spec:
        return None
    properties = {}
    for group in ("required", "optional"):
        for key, py_type in (spec.get(group) or {}).items():
            json_type = _JSON_TYPES.get(py_type)
            if not json_type:
                continue
            entry = {"type": json_type}
            if json_type == "ARRAY":
                # Gemini requires `items` on every ARRAY. The element shape is
                # deliberately unconstrained: these lists hold heterogeneous
                # records and pinning them is how a schema starts rejecting
                # correct answers.
                entry["items"] = {"type": "OBJECT", "properties": {}}
            properties[key] = entry
    if not properties:
        return None
    out = {"type": "OBJECT", "properties": properties}
    required = [k for k in (spec.get("required") or {})
                if k in properties]
    if required:
        out["required"] = required
    return out
