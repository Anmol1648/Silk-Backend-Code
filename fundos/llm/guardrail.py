"""
Advisory guardrail (Doc 1 §0.2 / LLM-M0-004 / Doc 8 §5).

Every representational role passes apply(); the adapter layer stamps
guardrail metadata on valuation/dilution/instrument/legal outputs:
ranges + SEBI/advisory disclaimer + "options, not advice". The UI renders
the disclaimer inseparably from the value.
"""
SEBI_DISCLAIMER = (
    "This output is AI-generated indicative information for preparation "
    "purposes only. It is not investment advice, a valuation report, or a "
    "recommendation under SEBI regulations. Valuations and dilution are "
    "presented as ranges, not commitments. For FEMA purposes, valuations of "
    "Indian entities may need to be expressed in INR by a registered valuer. "
    "Please consult your legal and financial advisers before acting."
)

OPTIONS_NOT_ADVICE = (
    "The instruments below are presented as compared options with pros and "
    "cons — FundOS never prescribes an instrument. Consult counsel before "
    "selecting a structure."
)

GUARDED_ROLES = {
    "valuation_negotiation": SEBI_DISCLAIMER,
    "instrument_explainer": OPTIONS_NOT_ADVICE,
    "raise_narrative": SEBI_DISCLAIMER,
}

# Directive phrases forbidden in guarded outputs (options, never prescriptions).
_DIRECTIVE_MARKERS = (
    "you must choose", "the best instrument is", "we recommend the safe",
    "you should accept", "guaranteed valuation",
)


def apply(role: str, data: dict, *, ai_model_version: str = "") -> dict:
    """Stamp guardrail metadata on an AI output dict."""
    data = dict(data)
    data["ai_generated"] = True
    if ai_model_version:
        data["model_version"] = ai_model_version
    data["requires_review"] = True                 # human-review-required
    data["label"] = "AI Generated Insights"

    disclaimer = GUARDED_ROLES.get(role)
    if disclaimer:
        data["disclaimer"] = disclaimer
        _strip_directive_language(data)
    return data


def _strip_directive_language(data):
    """Defensive scrub: soften any directive phrasing that slipped through."""
    def scrub(value):
        if isinstance(value, str):
            lowered = value.lower()
            for marker in _DIRECTIVE_MARKERS:
                if marker in lowered:
                    return value + " [Note: presented as an option, not advice.]"
            return value
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        return value

    for key in list(data.keys()):
        if key not in ("disclaimer", "label"):
            data[key] = scrub(data[key])
