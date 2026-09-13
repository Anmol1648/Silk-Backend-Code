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


def expand(quote, dossier, *, width=SNIPPET_CHARS):
    """The passage around `quote` in `dossier`, or "" when it is not there.

    Returned on whole-word boundaries with an ellipsis where text was cut, so
    a reader can see it is an extract rather than the whole document.
    """
    flat = _normalise(dossier)
    at = _find(dossier, quote)
    if at < 0:
        return ""

    target = _normalise(quote)
    pad = max((width - len(target)) // 2, 0)
    start = max(at - pad, 0)
    end = min(at + len(target) + pad, len(flat))

    # Whole words at both edges: a snippet starting mid-word reads as damage.
    if start > 0:
        space = flat.find(" ", start)
        start = space + 1 if 0 <= space < at else start
    if end < len(flat):
        space = flat.rfind(" ", at + len(target), end)
        end = space if space > 0 else end

    passage = flat[start:end].strip()
    if not passage:
        return ""
    return (("… " if start > 0 else "") + passage
            + (" …" if end < len(flat) else ""))


def for_citation(citation, dossier):
    """The best snippet available for one citation.

    The document's own words where they can be found, the model's quote
    otherwise. Never empty when the citation carried a quote — a panel that
    shows nothing is worse than one showing the short version.
    """
    quote = (citation or {}).get("quote") or ""
    if not dossier:
        return quote
    return expand(quote, dossier) or quote
