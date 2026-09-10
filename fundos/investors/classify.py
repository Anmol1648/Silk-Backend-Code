"""Investor classification — the "few Gemini calls" from the D-04 answer.

WHY THIS EXISTS
---------------
The source scrape names an investor. It does not say whether that investor is
a Global VC, a Domestic VC, a Family Office or an Angel; where it is
headquartered; or whether it runs a local office. Those attributes are what
the nine output buckets are built from, so without them Stage 3 can group
nothing.

TWO RULES THAT KEEP THIS HONEST
-------------------------------
  * NEW NAMES ONLY. An investor already classified is never revisited by the
    pipeline. Reclassification is an explicit admin action on a named
    investor. This keeps cost bounded and stops a firm's category drifting
    between refreshes when nothing about the firm changed.

  * BELOW THE CONFIDENCE FLOOR, NOTHING IS WRITTEN. The proposal goes to a
    review queue instead. An investor silently filed as Global VC when they
    are a Domestic VC changes which founders ever see them, and nothing on
    screen would look wrong.

The category list is passed in from the database, never hardcoded, so adding a
category is an admin row and the classifier picks it up on the next call.
"""
import json
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

CONFIDENCE_FLOOR = Decimal("0.80")

SYSTEM_PROMPT = (
    "You classify investment firms. You will be given a firm name and a list "
    "of allowed categories with their definitions.\n\n"
    "Search the web to identify the firm. Choose exactly one category from the "
    "list — never invent one. Report the headquarters city and country.\n\n"
    "Set confidence below 0.8 if you are not sure you have identified the "
    "right firm, if the name is ambiguous, or if the firm does not fit any "
    "category cleanly. A low confidence answer is useful; a confident wrong "
    "answer is not.\n\n"
    'Return JSON only, no prose and no code fences:\n'
    '{"category":"<exact name from the list>","hqCity":"","hqCountry":"",'
    '"hasLocalOffice":true|false|null,"confidence":0.0-1.0,'
    '"reasoning":"one sentence"}'
)


def classify_investor(investor, categories):
    """Classify one investor. Returns a dict or None.

    Keys: category (InvestorCategory | None), hq_city, hq_country,
    confidence (float), reasoning, model.
    """
    from fundos.llm.adapter import llm_generate

    catalogue = "\n".join(
        f"- {c.name}: {c.description or 'no description supplied'}"
        for c in categories)

    prompt = (
        f'Firm name: "{investor.name}"\n\n'
        f"Allowed categories:\n{catalogue}\n\n"
        "Classify this firm."
    )

    try:
        raw = llm_generate(role="investor_classification",
                           system=SYSTEM_PROMPT, prompt=prompt,
                           context={"investorName": investor.name})
    except Exception as e:
        logger.error("CLASSIFY: call failed for %r: %s", investor.name, e,
                     exc_info=True)
        raise

    data = _coerce(raw)
    if not data:
        logger.warning("CLASSIFY: unparseable response for %r", investor.name)
        return None

    by_name = {c.name.strip().lower(): c for c in categories}
    proposed = by_name.get(str(data.get("category", "")).strip().lower())
    if data.get("category") and not proposed:
        # The model invented a category. That is a signal the taxonomy does
        # not cover this firm, so it goes to review rather than being forced
        # into the nearest row.
        logger.info("CLASSIFY: %r returned unknown category %r — queuing.",
                    investor.name, data.get("category"))

    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not proposed:
        confidence = min(confidence, 0.0)

    return {
        "category": proposed,
        "hq_city": data.get("hqCity") or "",
        "hq_country": data.get("hqCountry") or "",
        "has_local_office": data.get("hasLocalOffice"),
        "confidence": confidence,
        "reasoning": data.get("reasoning") or "",
        "model": data.get("_model") or "",
    }


def _coerce(raw):
    """Unwrap the adapter envelope and strip code fences if present."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        if "content" in raw and isinstance(raw["content"], dict):
            inner = dict(raw["content"])
            inner.setdefault("_model", raw.get("model", ""))
            return inner
        if "content" in raw and isinstance(raw["content"], str):
            return _parse_json_text(raw["content"], raw.get("model", ""))
        return raw
    if isinstance(raw, str):
        return _parse_json_text(raw, "")
    return None


def _parse_json_text(text, model):
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        out = json.loads(s[start:end + 1])
        out["_model"] = model
        return out
    except json.JSONDecodeError:
        return None
