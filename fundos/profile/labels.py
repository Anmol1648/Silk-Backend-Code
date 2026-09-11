"""What a section and a field are CALLED on screen.

Lifted out of the views module so anything that has to name a field for a
reader -- a citation panel, a chat suggestion -- says it the same way. One
definition, because two would drift and a reader would meet the same field
under two names.
"""
import re

#: Tokens that are shouted rather than capitalised, so a humanised field name
#: reads as someone would say it: "USP", not "Usp".
ALWAYS_UPPER = {
    "usp", "kpi", "kpis", "arr", "mrr", "gmv", "cac", "ltv", "roi", "roe",
    "ebitda", "pat", "pbt", "capex", "opex", "tam", "sam", "som", "ipo",
    "esop", "b2b", "b2c", "d2c", "hq", "ceo", "cto", "coo", "cfo", "url",
    "id", "usd", "inr", "eur", "gbp", "fy", "mn", "bn", "cr", "yoy", "cagr",
}

#: The "8.9 " in "8.9 Financial Summary" -- an index, not part of the name.
SECTION_REF_NUMBER = re.compile(r"^\d+(?:\.\d+)*\s+")


def humanize(field):
    """`description_of_business` -> `Description of business`."""
    words = str(field or "").replace("_", " ").split()
    out = []
    for position, word in enumerate(words):
        if word.lower() in ALWAYS_UPPER:
            out.append(word.upper())
        elif position == 0:
            out.append(word[:1].upper() + word[1:])
        else:
            out.append(word)
    return " ".join(out)


def section_label(wire_key):
    """The section's name as a reader would say it: "Founders & Key People"."""
    from fundos.profile import schema as profile_schema

    spec = profile_schema.sections_by_key().get(wire_key) or {}
    ref = str(spec.get("ref") or "")
    return SECTION_REF_NUMBER.sub("", ref).strip() or wire_key
