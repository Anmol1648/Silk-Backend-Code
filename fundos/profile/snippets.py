"""The passage a citation came from, not the phrase the model typed.

Asked where a founder's name came from, the model cited the deck and quoted
`"Khushboo Aggarwal"` — the name, and nothing else. Technically a quote;
useless as evidence. A reader clicking a provenance chip wants to see the
place in the document, enough of it to judge whether it says what the field
claims.

So the quote is not trusted as the snippet. It is used as a SEARCH TERM
against the dossier the run was built from — which is stored — and what
comes back is the surrounding text, exactly as the document has it. Nothing
is written or paraphrased: if the passage cannot be found, the model's own
quote stands rather than being replaced by a guess.

The dossier is fetched once per request and reused for every citation on
that field, because a field with four sources would otherwise download the
same 600,000-character file four times.
"""
import logging
import re

from fundos.core.services.citation_text import clean

logger = logging.getLogger("fundos.profile")

#: How much surrounding text to return. Enough to carry the sentence a figure
#: sits in and the heading above it; short enough to read in a panel.
SNIPPET_CHARS = 700

#: Never search the dossier for less than this. A two-word quote matches in a
#: hundred places, and the first one is as likely to be wrong as right.
MIN_SEARCHABLE = 4

#: Give up on a dossier bigger than this rather than hold it in memory for a
#: side panel. Nothing breaks: the model's quote is still shown.
MAX_DOSSIER_BYTES = 8 * 1024 * 1024


def dossier_text(run):
    """The dossier this run was built from, or "" when it cannot be read.

    Never raises. A provenance panel is a convenience; failing it must not
    fail the request that asked for it.
    """
    uri = getattr(run, "dossier_uri", "") or ""
    if not uri:
        return ""
    if (getattr(run, "dossier_chars", 0) or 0) > MAX_DOSSIER_BYTES:
        return ""
    try:
        import os
        import tempfile

        from fundos.docs.storage import get_storage

        handle, path = tempfile.mkstemp(suffix=".md")
        os.close(handle)
        try:
            if not get_storage().download_file(uri, path):
                return ""
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception as exc:                # pragma: no cover - never fatal
        logger.debug("SNIPPET: dossier unavailable for run %s: %s",
                     getattr(run, "id", "?"), exc)
        return ""


def _normalise(text):
    """Collapse whitespace so a quote matches text wrapped differently."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _find(haystack, needle):
    """Where `needle` appears in `haystack`, ignoring case and wrapping."""
    if not haystack or not needle:
        return -1
    flat = _normalise(haystack).casefold()
    target = _normalise(needle).casefold()
    if len(target) < MIN_SEARCHABLE:
        return -1
    return flat.find(target)


#: Where a sentence ends: terminal punctuation followed by a space.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9₹$€£])")


def _is_row(line):
    return line.strip().startswith("|")


def _sentence_window(line, at, length, width):
    """Whole sentences of one long line around [at, at+length).

    A paragraph a converter wrote as one line would otherwise be cut at a
    character count — mid-sentence at both ends. Sentences are added outward
    from the one holding the quote while they fit; a single sentence longer
    than the budget falls back to word boundaries.
    """
    bounds, start = [], 0
    for match in _SENTENCE_END.finditer(line):
        bounds.append((start, match.start()))
        start = match.end()
    bounds.append((start, len(line)))

    first = next((i for i, (s, e) in enumerate(bounds) if e > at), 0)
    last = next((i for i, (s, e) in enumerate(bounds)
                 if e >= at + length), len(bounds) - 1)
    lo, hi = first, max(last, first)
    while True:
        grew = False
        if hi + 1 < len(bounds) and bounds[hi + 1][1] - bounds[lo][0] <= width:
            hi, grew = hi + 1, True
        if lo > 0 and bounds[hi][1] - bounds[lo - 1][0] <= width:
            lo, grew = lo - 1, True
        if not grew:
            break

    s, e = bounds[lo][0], bounds[hi][1]
    if e - s > width:
        # One sentence bigger than the panel: whole words around the quote.
        pad = max((width - length) // 2, 0)
        s2, e2 = max(at - pad, s), min(at + length + pad, e)
        if s2 > s:
            space = line.find(" ", s2)
            s2 = space + 1 if 0 <= space < at else s2
        if e2 < e:
            space = line.rfind(" ", at + length, e2)
            e2 = space if space > 0 else e2
        return line[s2:e2].strip(), s2 > 0, e2 < len(line)
    return line[s:e].strip(), s > 0, e < len(line)


def expand(quote, dossier, *, width=SNIPPET_CHARS):
    """The passage around `quote` in `dossier`, or "" when it is not there.

    Built from WHOLE LINES, so a spreadsheet row is never sliced through the
    middle and keeps the line break that makes it a row (the cleaner then
    pairs its figures with their column headers). A long prose line is cut on
    sentence boundaries. An ellipsis marks each edge where text was left out.
    """
    target = _normalise(quote)
    if len(target) < MIN_SEARCHABLE or not dossier:
        return ""
    wanted = target.casefold()

    # Normalised lines, and where each one starts in their joined text.
    lines, offsets, flat = [], [], ""
    for raw in str(dossier).split("\n"):
        norm = _normalise(raw)
        if not norm:
            continue
        if flat:
            flat += " "
        offsets.append(len(flat))
        lines.append(norm)
        flat += norm
    at = flat.casefold().find(wanted)
    if at < 0:
        return ""
    end_at = at + len(target)

    first = max(i for i, off in enumerate(offsets) if off <= at)
    last = max(i for i, off in enumerate(offsets) if off < end_at)

    if first == last and len(lines[first]) > width and not _is_row(lines[first]):
        text, cut_left, cut_right = _sentence_window(
            lines[first], at - offsets[first], len(target), width)
        return (("… " if cut_left or first > 0 else "") + text
                + (" …" if cut_right or last < len(lines) - 1 else ""))

    lo, hi = first, last

    def size(a, b):
        return sum(len(line) + 1 for line in lines[a:b + 1])

    while True:
        grew = False
        if hi + 1 < len(lines) and size(lo, hi + 1) <= width:
            hi, grew = hi + 1, True
        if lo > 0 and size(lo - 1, hi) <= width:
            lo, grew = lo - 1, True
        if not grew:
            break

    # A table row needs its header to be read: include the header and rule
    # of the table it sits in, even past the budget, when they are nearby.
    if _is_row(lines[lo]):
        top = lo
        while top > 0 and _is_row(lines[top - 1]) and lo - top < 40:
            top -= 1
        if top < lo and top + 1 < len(lines) and re.fullmatch(
                r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", lines[top + 1]):
            block = lines[top:top + 2] + lines[max(lo, top + 2):hi + 1]
            return _marked(block, top > 0, hi < len(lines) - 1)

    return _marked(lines[lo:hi + 1], lo > 0, hi < len(lines) - 1)


def _marked(block, cut_before, cut_after):
    """Lines joined, with an ellipsis on its own line at each cut edge.

    On its own line so a table's first row still begins with `|` — glued to
    the row, the ellipsis would stop the row being read as one.
    """
    return "\n".join((["…"] if cut_before else []) + list(block)
                     + (["…"] if cut_after else []))


#: `### Project Orah Teaser_vff.pptx` -- where one uploaded file begins.
_FILE_HEADING = re.compile(r"^###\s+(\S.*?\.[A-Za-z0-9]{2,5})\s*$",
                           re.MULTILINE)

#: `## Batch 2: Founders & Leadership` -- where one research batch begins.
_BATCH_HEADING = re.compile(r"^##\s+(Batch\s+\d+:\s*\S.*?)\s*$",
                            re.MULTILINE)

#: What ends a file or batch section: the next file, the next research batch,
#: the next top-level source, or the rule between the dossier's parts. NOT any
#: heading — a converted deck renders each slide title as `# Title`, and
#: stopping there would cut a file's section off at its first slide.
_SECTION_END = re.compile(
    r"^(?:###\s+\S.*?\.[A-Za-z0-9]{2,5}\s*$|##\s+Batch\s+\d+:|#\s+Source\s"
    r"|---\s*$)",
    re.MULTILINE)

#: "Slide 20", "slide 20", "Page 4", "p. 4".
_LOCATOR_NUMBER = re.compile(r"(slide|page|p\.)\s*(\d+)", re.IGNORECASE)


def _norm(text):
    return re.sub(r"[\s_\-]+", " ", str(text or "")).strip().casefold()


def _section_for(source, dossier):
    """The part of the dossier a citation's SOURCE covers, or "".

    A document citation reaches only its own file's section; a research
    citation only its own batch. "" when the source cannot be located, and
    the caller must not fall back to the whole dossier: that is how a
    founder's name was matched in the research-leads list at the TOP of the
    dossier and shown to a reader labelled "Project Orah Teaser · Slide 20".
    """
    target = _norm(source)
    if not target:
        return ""
    for pattern in (_FILE_HEADING, _BATCH_HEADING):
        for match in pattern.finditer(dossier):
            name = _norm(match.group(1))
            if name and (name == target or name in target or target in name):
                start = match.end()
                end = _SECTION_END.search(dossier, start)
                return dossier[start:end.start() if end else len(dossier)]
    return ""


def _slide_or_page(section, locator):
    """Narrow a file section to the slide or page a locator names, or ""."""
    match = _LOCATOR_NUMBER.search(str(locator or ""))
    if not match:
        return ""
    number = int(match.group(2))
    if match.group(1).lower().startswith("slide"):
        marker = re.compile(r"<!--\s*Slide number:\s*(\d+)\s*-->")
    else:
        marker = re.compile(r"(?:<!--\s*Page\s*(\d+)\s*-->|^##\s*Page\s+(\d+)\s*$)",
                            re.MULTILINE | re.IGNORECASE)
    marks = list(marker.finditer(section))
    for i, mark in enumerate(marks):
        found = next((g for g in mark.groups() if g), None)
        if found and int(found) == number:
            end = marks[i + 1].start() if i + 1 < len(marks) else len(section)
            return section[mark.start():end]
    return ""


def locate(citation, dossier):
    """(snippet, verified) for one citation.

    `verified` is True only when the snippet is the document's own passage,
    found where the citation points. False means the text is the model's
    quote, shown because nothing better was found — a panel must not present
    it as an extract from the document.
    """
    citation = citation if isinstance(citation, dict) else {}
    quote = citation.get("quote") or ""
    if not isinstance(quote, str):
        quote = str(quote)
    if not quote:
        return "", False
    if dossier:
        section = _section_for(citation.get("source"), dossier)
        if section:
            narrow = _slide_or_page(section, citation.get("locator"))
            for scope in (narrow, section):
                if scope:
                    passage = expand(quote, scope)
                    if passage:
                        return clean(passage), True
    return clean(quote), False


def for_citation(citation, dossier):
    """The best snippet available for one citation.

    SEARCHED WHERE THE CITATION POINTS, NEVER ANYWHERE ELSE. The narrowest
    scope first -- the named slide or page, then the named file or research
    batch -- and the model's own quote when neither contains the passage.
    Never the whole dossier: a name appears in many places, and the first of
    them is rarely the one being cited.

    Never empty when the citation carried a quote — a panel that shows
    nothing is worse than one showing the short version.
    """
    return locate(citation, dossier)[0]
