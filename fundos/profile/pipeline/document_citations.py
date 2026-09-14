"""Credit the uploaded documents for what they actually say.

A live Zyla Health run cited web research 143 times and its own uploaded
documents twice -- although the founders' names were on slide 20 of the deck
it had been given. Web research arrives as ready-made answer sentences and is
easy to quote; the same fact inside slide text or a spreadsheet row is not,
so the model took the easy source. The uploaded documents are the company's
own account and the source a reader trusts most.

So after synthesis, each value distinctive enough to be matched safely -- a
person's or company's name, a URL, an email, a figure with enough digits to
be specific -- is looked for in the documents' own text. Where a document
states it, a citation to that document is ADDED: its file, the slide or sheet
it sits on, and the line itself as a short quote. Nothing is removed and
nothing is invented: the quote is the document's own line, and a value that
is not in a document is left exactly as the model cited it.

Generic values are never matched ("India", "Healthcare", "2024", "16.5"):
they appear in documents for unrelated reasons, and crediting a document by
coincidence would be worse than not crediting it.
"""
import logging
import re

from fundos.core.services.citation_text import clean

logger = logging.getLogger("fundos.profile")

#: A document quote is a short extract, not a passage.
QUOTE_CHARS = 200

#: Values longer than this are narrative the model wrote, not a fact that can
#: appear verbatim in a document.
MAX_VALUE_CHARS = 120

_SLIDE = re.compile(r"<!--\s*Slide number:\s*(\d+)\s*-->", re.IGNORECASE)
_PAGE = re.compile(r"<!--\s*Page\s*(\d+)\s*-->|^##\s*Page\s+(\d+)\s*$",
                   re.IGNORECASE)
_SHEET = re.compile(r"^##\s+(?!Page\s+\d)(\S.*?)\s*$")
_URL = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,}(?:/\S*)?)$",
                  re.IGNORECASE)
_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.]+$")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")

#: What names a list item. Deliberately not its role, type or status.
_IDENTIFYING_FIELDS = ("name", "title", "metric", "market", "stream", "round",
                       "investor_name", "filename")

#: Words that say what KIND of thing a value is, not WHICH one. A value made
#: only of these ("Co-Founder", "Private Limited", "Series A") appears in
#: documents for reasons unrelated to this field.
_GENERIC_WORDS = {
    "co", "founder", "cofounder", "ceo", "cto", "cfo", "coo", "cbo", "cmo",
    "chief", "officer", "director", "head", "manager", "lead", "partner",
    "president", "vice", "vp", "senior", "board", "member", "advisor",
    "private", "limited", "pvt", "ltd", "llp", "inc", "llc", "corp",
    "company", "series", "seed", "pre", "round", "angel", "the", "and", "of",
    "for", "in", "on", "a", "an", "to", "full", "time", "part", "yes", "no",
    "not", "disclosed", "publicly", "available", "unknown", "none", "na",
}


def _norm(text):
    text = str(text or "").replace("|", " ").replace("*", " ")
    return re.sub(r"\s+", " ", text).strip().casefold()


def _document_lines(dossier, document_labels):
    """(label, locator, normalised line, raw line) for every document line."""
    labels = {str(label).strip(): label for label in document_labels or []}
    if not labels or not dossier:
        return []
    start = dossier.find("# Source 2")
    if start < 0:
        return []
    end = dossier.find("\n# Source ", start + 1)
    block = dossier[start:end if end > 0 else len(dossier)]

    rows, label, locator, sheet = [], None, "", ""
    for raw in block.split("\n"):
        stripped = raw.strip()
        if stripped.startswith("### "):
            name = stripped[4:].strip()
            if name in labels:
                label = labels[name]
                locator = sheet = ""
                continue
            # A `### Chart` inside a converted deck is the deck's own
            # heading, not the start of another file.
        if label is None or not stripped:
            continue
        slide = _SLIDE.search(stripped)
        if slide:
            locator = f"Slide {slide.group(1)}"
            continue
        page = _PAGE.search(stripped)
        if page:
            locator = f"Page {page.group(1) or page.group(2)}"
            continue
        heading = _SHEET.match(stripped)
        if heading:
            sheet = heading.group(1)
            locator = sheet
            continue
        if stripped.startswith("#") or stripped.startswith("*") and \
                stripped.endswith("*"):
            continue
        rows.append((label, locator, _norm(stripped), stripped))
    return rows


def _needle(value):
    """The normalised form of a value worth matching, or "" if it is not."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        digits = re.sub(r"\D", "", str(value))
        return str(value) if len(digits) >= 5 else ""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > MAX_VALUE_CHARS:
        return ""
    if _EMAIL.match(text):
        return text.casefold()
    url = _URL.match(text)
    if url and "." in url.group(1) and " " not in text:
        return url.group(1).rstrip("/").casefold()
    if _YEAR.match(text):
        return ""
    words = re.findall(r"[A-Za-z]+", text)
    digits = re.sub(r"\D", "", text)
    distinctive = [w for w in words if w.casefold() not in _GENERIC_WORDS]
    if len(words) >= 2 and len(text) >= 10 and distinctive:
        return _norm(text)
    if len(digits) >= 5 and not words:
        return _norm(text)          # a specific figure: 1,20,000 / 45,62,300
    return ""


def _find(rows, joined, starts, needle):
    """The first document row containing `needle`, or None."""
    at = joined.find(needle)
    if at < 0:
        return None
    # Binary search for the row the match starts in.
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= at:
            lo = mid
        else:
            hi = mid - 1
    row = rows[lo]
    # A match straddling two rows is not a statement either row makes.
    if needle not in row[2]:
        return None
    return row


def _has_document(sources, field, document_labels):
    for key, citation in (sources or {}).items():
        if (key == field or key.startswith(f"{field}.")) and \
                (citation or {}).get("source") in document_labels:
            return True
    return False


def _add(sources, field, label, locator, raw_line):
    """Add a document citation for `field` without disturbing the others."""
    citation = {"source": label, "locator": locator,
                "quote": clean(raw_line, limit=QUOTE_CHARS)}
    if field not in sources:
        sources[field] = citation
        return
    n = 1
    while f"{field}.{n}" in sources:
        n += 1
    sources[f"{field}.{n}"] = citation


def _candidates(data):
    """(citation address, value) pairs to look for in the documents."""
    if isinstance(data, dict):
        for field, value in data.items():
            if isinstance(value, (list, dict)):
                continue
            yield str(field), value
    elif isinstance(data, list):
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            # An item is cited as a whole, by its position, and matched only
            # by what NAMES it. A founder's role "Co-Founder" sits on the team
            # slide beside somebody else's name; matching it credited the
            # slide for a founder the deck does not mention.
            for field in _IDENTIFYING_FIELDS:
                value = item.get(field)
                if isinstance(value, str) and value.strip():
                    yield str(index), value
                    break


def credit_documents(profile, dossier, document_labels):
    """Add document citations where the documents state a value.

    :returns: how many citations were added. Never raises -- a crediting pass
        that fails must not cost the run the profile it already has.
    """
    try:
        document_labels = [label for label in document_labels or [] if label]
        rows = _document_lines(dossier, document_labels)
        if not rows:
            return 0
        starts, pieces, offset = [], [], 0
        for row in rows:
            starts.append(offset)
            pieces.append(row[2])
            offset += len(row[2]) + 1
        joined = "\n".join(pieces)

        added = 0
        for section in (profile.get("sections") or {}).values():
            if not isinstance(section, dict) or not section.get("isComplete"):
                continue
            sources = section.get("sources")
            if not isinstance(sources, dict):
                sources = {}
            done = set()
            for field, value in _candidates(section.get("data")):
                if field in done or _has_document(sources, field,
                                                  document_labels):
                    continue
                needle = _needle(value)
                if not needle:
                    continue
                row = _find(rows, joined, starts, needle)
                if row is None:
                    continue
                _add(sources, field, row[0], row[1], row[3])
                done.add(field)
                added += 1
            if sources:
                section["sources"] = sources
        return added
    except Exception as exc:                # pragma: no cover - never fatal
        logger.warning("PIPELINE: document crediting skipped: %s", exc)
        return 0
