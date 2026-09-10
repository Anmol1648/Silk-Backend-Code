"""Read a document by sending the FILE to a model that can read documents.

This replaces the local OCR engines. Tesseract and EasyOCR rendered a page to
a bitmap and recognised glyphs; a document-capable model is handed the file
itself and reads layout, tables and figures together. On the material this
system actually receives — pitch decks with numbers baked into images, scanned
term sheets, statements photographed rather than exported — that is a large
gain in coverage.

IT IS ALSO A DIFFERENT RISK, AND THE DIFFERENCE MATTERS
-------------------------------------------------------
Tesseract fails by garbling: at low resolution it read ``USD 24.0 million`` as
``240``, which is wrong in a way that looks wrong. A model fails by producing
a clean, plausible figure that was never on the page, which is wrong in a way
that looks right.

Three things follow, and none of them is optional:

* The prompt asks for a TRANSCRIPTION, not a summary or an interpretation.
  Nothing may be inferred, computed, completed or tidied.
* Every block this module returns is labelled as model-read in the dossier,
  exactly as OCR'd blocks were, so a reader can see which figures carry that
  risk.
* Model-read content ranks BELOW natively extracted text on source tier
  (`profile_bridge._outranks`). When the spreadsheet and the scanned deck
  disagree, the spreadsheet wins — automatically, without anyone noticing the
  disagreement.

WHAT IS NOT SENT HERE
---------------------
Spreadsheets and CSVs never come this way. Cell extraction is exact and a
model reading a financial model is strictly worse than reading its cells. The
same holds for DOCX, whose native text is already complete. Only formats whose
content can be inaccessible to a text extractor — PDF, PPTX, images — are read
by a model, and even then the native text is kept when it is there.
"""
import logging

logger = logging.getLogger("fundos.profile")

#: The role this module calls. Bound in admin like any other; must resolve to
#: a provider that accepts documents (Gemini today) or the call is refused
#: rather than silently answered from the prompt alone.
DOCUMENT_ROLE = "document_read"

#: Inline upload ceiling. The request carries the file base64-encoded, which
#: inflates it by about a third, and the provider rejects the whole request
#: over roughly 20 MB. 12 MB of source bytes leaves comfortable headroom.
#: A larger file is not truncated or silently skipped — it falls back to
#: native extraction and the dossier says why.
MAX_INLINE_BYTES = 12 * 1024 * 1024

#: What each format is called when the model is told what it is holding.
MIME_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
    ".pptx": ("application/vnd.openxmlformats-officedocument"
              ".presentationml.presentation"),
}

SYSTEM = (
    "You transcribe business documents exactly as they appear. You are not "
    "an analyst and you are not summarising."
)

PROMPT = (
    "Transcribe this document to Markdown, in reading order.\n\n"
    "RULES, in order of importance:\n"
    "1. Copy figures, dates, names and units EXACTLY as printed. Never round, "
    "convert, recompute or complete a number. If a figure is cut off or "
    "unreadable, write [unreadable] rather than guessing it.\n"
    "2. Do not summarise, interpret, comment or add anything that is not on "
    "the page. No preamble, no closing remarks.\n"
    "3. Reproduce tables as Markdown tables, keeping every row and column "
    "including blank cells.\n"
    "4. Mark each page or slide boundary with a line of the form "
    "`## Page N` (or `## Slide N`), so a reader can find the source.\n"
    "5. Transcribe text inside charts, screenshots and other images too — "
    "label that block `> [from image]` so it is distinguishable from body "
    "text.\n\n"
    "Return only the transcription."
)


def is_supported(suffix):
    """True when this format is read by a model rather than by an extractor."""
    return str(suffix or "").lower() in MIME_TYPES


def mime_for(suffix):
    return MIME_TYPES.get(str(suffix or "").lower(),
                          "application/octet-stream")


class DocumentReadError(RuntimeError):
    """The model could not read this file. The caller falls back to native."""


def read_document(path, filename, *, note=None, tenant_id=None, user=None,
                  deal_id=None):
    """Return the document's text as Markdown, or raise `DocumentReadError`.

    Never returns a partial answer silently: an empty or failed read raises,
    so the caller can fall back to native extraction and record which of the
    two produced the text that ended up in the dossier.
    """
    from pathlib import Path

    from fundos.llm.adapter import llm_generate

    source = Path(path)
    suffix = source.suffix.lower()
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise DocumentReadError(f"cannot stat {filename}: {exc}") from exc

    if size > MAX_INLINE_BYTES:
        raise DocumentReadError(
            f"{filename} is {size / 1_048_576:.1f} MB, over the "
            f"{MAX_INLINE_BYTES / 1_048_576:.0f} MB limit for a single "
            f"upload")
    if size == 0:
        raise DocumentReadError(f"{filename} is empty")

    if note:
        note(f"sending {filename} ({size / 1024:.0f} KB) to the document "
             f"model for reading")

    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise DocumentReadError(f"cannot read {filename}: {exc}") from exc

    try:
        result = llm_generate(
            role=DOCUMENT_ROLE,
            system=SYSTEM,
            prompt=PROMPT,
            response_kind="text",
            tenant_id=tenant_id,
            user=user,
            deal_id=deal_id,
            calling_context=f"document_read:{filename}",
            attachments=[{"mime_type": mime_for(suffix), "data": payload}],
        )
    except Exception as exc:                       # noqa: BLE001 - reported
        raise DocumentReadError(f"{type(exc).__name__}: {exc}") from exc

    text = (result or {}).get("text") or ""
    text = text.strip()
    if not text:
        raise DocumentReadError("the model returned no text")
    return text
