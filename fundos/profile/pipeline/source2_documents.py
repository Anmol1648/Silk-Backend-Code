"""Source 2: uploaded documents converted to markdown.

Two complementary readers run over each file:

* **MarkItDown** (Microsoft) handles native text and table extraction for PDF,
  DOCX, PPTX, XLSX, CSV and more. This is the primary converter, and for a
  spreadsheet it is the ONLY one: cell extraction is exact, and a model
  reading a financial model is strictly worse than reading its cells.
* **A document model** reads the file itself for the formats whose content a
  text extractor structurally cannot reach — PDF, PPTX and images. It
  replaces the Tesseract/EasyOCR stage: rather than rendering a page to a
  bitmap and recognising glyphs, the file is handed to a model that reads
  layout, tables and the numbers inside charts together.

WHY THE LABEL MATTERS MORE NOW, NOT LESS
----------------------------------------
OCR failed by garbling — at low resolution both engines read
``USD 24.0 million`` as ``240``, which is wrong in a way that looks wrong. A
model fails by producing a clean, plausible figure that was never on the page,
which is wrong in a way that looks right. So every model-read block is
labelled as such in the dossier, exactly as OCR'd blocks were, and model-read
content ranks BELOW natively extracted text on source tier: when the
spreadsheet and the scanned deck disagree, the spreadsheet wins without anyone
having to notice the disagreement.

What this replaces
------------------
The previous extractor (``readiness.tasks._extract_text``) read ``.txt``,
``.csv`` and the text layer of a PDF, and returned ``""`` for everything else.
A founder who uploaded a pitch deck and a financial model contributed exactly
nothing to their profile and had no way to know.

Every file's treatment is decided by a table consulted *before* any work
starts, recorded on the document row, and stated in the dossier — so "was my
deck actually read, and how?" is answerable without reading logs.

Converted markdown accumulates in memory and is pushed into the dossier
through the shared :class:`~fundos.profile.pipeline.dossier.DossierWriter`
after each file, so progress is durable without this module writing artifacts
of its own. One bad file records its error in its own subsection and never
fails the source.
"""

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field

from fundos.profile.pipeline import document_ai
from fundos.profile.pipeline import settings as pipeline_settings
from fundos.profile.pipeline import tracker

logger = logging.getLogger("fundos.profile")

SOURCE_KEY = "source2"
SOURCE_TITLE = ("Source 2: Company Documents "
                "(native extraction + document model)")

# Ignore a read that came back with almost nothing rather than content.
MIN_USEFUL_TEXT_CHARS = 24

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

CATEGORY_LABELS = {
    "company_presentation": "Company Presentation",
    "financial_model": "Financial Model",
    "annual_report": "Annual Report / Financial Statements",
    "other": "Other Documents",
    "teaser": "Teaser",
    "pitch_deck": "Pitch Deck",
    "information_memorandum": "Information Memorandum",
}
# Fixed render order, so the dossier's shape does not depend on which category
# a company happened to upload into first.
CATEGORY_ORDER = ("company_presentation", "financial_model", "annual_report",
                  "pitch_deck", "information_memorandum", "teaser", "other")


# --- how each file type is handled -----------------------------------------
#
# One table, consulted before any work starts, so "how will this file be read,
# and why" is answerable up front rather than inferred afterwards from whatever
# text happened to come out.
#
#   native -- MarkItDown extracts embedded text and tables
#   model  -- the file itself is read by a document-capable model

@dataclass(frozen=True)
class TypePolicy:
    handler: str
    """Short label surfaced on the document row, e.g. ``native+model``."""
    description: str
    """One sentence explaining the treatment, surfaced in status and dossier."""
    read_mode: str
    """``model`` — the file is read by a model — or ``never``."""
    unit: str = "page"
    """What this format's content is counted in — page, slide or image."""


NATIVE_ONLY = TypePolicy(
    handler="native only",
    description=("MarkItDown extracts the text and tables directly. This "
                 "format stores its content as text, and for a spreadsheet "
                 "the cells are exact — a model reading them could only be "
                 "worse."),
    read_mode="never",
)

FILE_TYPE_POLICY = {
    ".pdf": TypePolicy(
        handler="native+model",
        description=("MarkItDown extracts the embedded text layer, and the "
                     "whole file is additionally read by a document model — "
                     "which reaches scanned pages, and reads tables as tables "
                     "rather than as a run of text."),
        read_mode="model", unit="page"),
    # A DECK IS NATIVE ONLY, because no provider will read the format.
    #
    # This said "native+model" and the model read 400'd every time --
    # "Unsupported MIME type: ...presentationml.presentation" -- so every run
    # promised a treatment it could not deliver, spent a failed call on it,
    # and logged a CRITICAL pointing at the model binding, which was never
    # the problem. The plan line now says what will actually happen.
    #
    # The loss is real and named rather than hidden: a number that exists
    # only inside a chart or a screenshot is not recovered from slide text.
    # Converting the deck to PDF before upload gets it read.
    ".pptx": TypePolicy(
        handler="native only",
        description=("MarkItDown extracts the slide text and speaker notes. "
                     "A document model cannot read a PowerPoint file — no "
                     "provider accepts the format — so numbers that appear "
                     "only inside a chart or a screenshot are not read. "
                     "Save the deck as PDF and they are."),
        read_mode="never", unit="slide"),
    ".ppt": NATIVE_ONLY,
    ".xlsx": NATIVE_ONLY, ".xlsm": NATIVE_ONLY, ".xls": NATIVE_ONLY,
    ".csv": NATIVE_ONLY, ".docx": NATIVE_ONLY, ".doc": NATIVE_ONLY,
    ".txt": NATIVE_ONLY, ".md": NATIVE_ONLY, ".json": NATIVE_ONLY,
    ".xml": NATIVE_ONLY, ".html": NATIVE_ONLY, ".htm": NATIVE_ONLY,
}

IMAGE_POLICY = TypePolicy(
    handler="model only",
    description=("An image has no text layer to extract, so reading it with "
                 "a model is the only route to its content."),
    read_mode="model", unit="image",
)

UNKNOWN_POLICY = TypePolicy(
    handler="native (best effort)",
    description=("Unrecognised extension: MarkItDown is given a chance to "
                 "sniff the format, but it is not sent to the document model, "
                 "which needs a declared media type to read it."),
    read_mode="never",
)


def policy_for(filename):
    """The handling rule for one file, chosen by extension."""
    suffix = os.path.splitext(filename or "")[1].lower()
    if suffix in IMAGE_SUFFIXES:
        return IMAGE_POLICY
    return FILE_TYPE_POLICY.get(suffix, UNKNOWN_POLICY)


@dataclass
class ReadOutcome:
    """What the document model did to one file — and, if nothing, why.

    The stored column names stay ``ocr_*``. They record the same fact they
    always did — whether this file needed reading beyond its text layer, and
    what came back — and migrating live rows to rename a prefix would change
    no answer any of them gives.
    """

    status: str = "pending"
    """``triggered`` | ``skipped`` | ``disabled`` | ``failed``."""
    reason: str = ""
    text: str = ""
    units_total: int = 0
    units_ocred: int = 0
    engines: list = field(default_factory=list)
    error: str = ""

    def as_document_fields(self, seconds):
        """Shape this outcome as the ``ocr_*`` columns of a MaterialAsset row."""
        return {
            "ocr_status": self.status,
            "ocr_reason": self.reason[:500],
            "ocr_engines": list(self.engines),
            "ocr_units_total": self.units_total,
            "ocr_units_ocred": self.units_ocred,
            "ocr_chars": len(self.text),
            "ocr_seconds": seconds,
            "ocr_applied": self.status == "triggered",
        }


# --- extractors (blocking) --------------------------------------------------


def _convert_with_markitdown(path):
    """Native text/table extraction via Microsoft's MarkItDown.

    Imported inside the function, not at module scope, so this module (and the
    pipeline generally) imports cleanly in an environment where ``markitdown``
    is absent — only calling this specific function requires it.
    """
    from markitdown import MarkItDown

    result = MarkItDown().convert(str(path))
    return _drop_empty_rows(_drop_slide_boilerplate(_drop_image_placeholders(
        (getattr(result, "text_content", None) or "").strip())))


#: A markdown image: `![alt](target)`. In a converted deck every picture on
#: every slide becomes one of these, pointing at a file that exists only
#: inside the .pptx -- `![](GoogleShape295p51.jpg)`, `![image.png](Picture3.jpg)`.
#: The model reading the dossier cannot open any of them, so each is text
#: that costs tokens and carries nothing, sitting between the words that do.
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")

#: Alt text a converter or an authoring tool invented rather than a person
#: wrote: a filename, or a generic word and a number.
_GENERIC_ALT = re.compile(
    r"^\s*(?:[\w\- ]*\.(?:png|jpe?g|gif|bmp|svg|webp|tiff?|emf|wmf)"
    r"|(?:image|picture|pic|img|graphic|shape|googleshape|photo|figure|"
    r"object|diagram|chart)\s*[\w\-]*)?\s*$",
    re.IGNORECASE)


def _drop_image_placeholders(text):
    """Remove pictures the model cannot see; keep any description of them.

    A description somebody actually wrote -- `![Revenue by region, FY24]` --
    is the only thing an image contributes to a text reading, so it is kept
    as `[image: Revenue by region, FY24]`. A filename or "image.png" is not a
    description and goes with the reference. Slide markers are untouched:
    they are how a citation says "Slide 20".
    """
    if "![" not in text:
        return text

    removed = 0

    def replace(match):
        nonlocal removed
        removed += 1
        alt = match.group(1).strip()
        if not alt or _GENERIC_ALT.match(alt):
            return ""
        return f"[image: {alt}]"

    cleaned = _IMAGE.sub(replace, text)
    # The references usually sat on lines of their own; what is left behind
    # is a stack of blank lines. Two in a row is a paragraph break, and more
    # than that says nothing.
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    # Inline references leave a run of spaces where they sat. Only runs that
    # FOLLOW text are collapsed, so a line's own indentation survives.
    cleaned = re.sub(r"(?<=\S)[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if removed:
        logger.info("PIPELINE: removed %d image placeholder(s) from a native "
                    "extract (%d -> %d characters).", removed, len(text),
                    len(cleaned))
    return cleaned


#: The marker MarkItDown writes at the top of each slide. Kept: it is how a
#: citation says "Slide 20".
_SLIDE_MARKER = re.compile(r"<!--\s*Slide number:\s*\d+\s*-->")

#: A speaker-notes heading with nothing under it. MarkItDown writes one on
#: every slide whether or not the slide has notes.
_EMPTY_NOTES = re.compile(r"^#{1,6}\s*Notes:\s*$\n?(?=\s*(?:<!--\s*Slide|\Z))",
                          re.MULTILINE)

#: A line must recur on at least this many slides, and this share of them,
#: before it is treated as a footer rather than content.
_FOOTER_MIN_SLIDES = 3
_FOOTER_MIN_SHARE = 0.4
_FOOTER_MAX_CHARS = 80


def _drop_slide_boilerplate(text):
    """Remove what every slide repeats and no slide means.

    Two things, in a converted deck:

      * an empty `### Notes:` heading on every slide without speaker notes;
      * a footer or header line -- a logo word, "Proprietary and
        confidential" -- repeated on slide after slide.

    A repeated line is kept ONCE, on the first slide it appears, so nothing
    it says is lost. A line carrying a digit is never treated as a footer:
    a figure that happens to recur is a fact, and dropping its later copies
    would be the wrong kind of tidy. Only decks are touched; a document with
    no slide markers is returned unchanged apart from empty notes headings.
    """
    cleaned = _EMPTY_NOTES.sub("", text)

    markers = list(_SLIDE_MARKER.finditer(cleaned))
    if len(markers) < _FOOTER_MIN_SLIDES + 1:
        return cleaned

    # Split into [preamble, slide1, slide2, ...], each slide starting at its
    # marker, so line counting is per slide rather than per occurrence.
    bounds = [m.start() for m in markers] + [len(cleaned)]
    preamble = cleaned[:bounds[0]]
    slides = [cleaned[bounds[i]:bounds[i + 1]] for i in range(len(markers))]

    def candidates(slide):
        seen = set()
        for line in slide.splitlines():
            key = line.strip()
            if (key and len(key) <= _FOOTER_MAX_CHARS
                    and not _SLIDE_MARKER.fullmatch(key)
                    and not key.startswith("#")
                    and not any(ch.isdigit() for ch in key)):
                seen.add(key)
        return seen

    counts = {}
    for slide in slides:
        for key in candidates(slide):
            counts[key] = counts.get(key, 0) + 1

    threshold = max(_FOOTER_MIN_SLIDES,
                    int(len(slides) * _FOOTER_MIN_SHARE + 0.999))
    footers = {key for key, n in counts.items() if n >= threshold}
    if not footers:
        return cleaned

    kept_once, out, dropped = set(), [preamble], 0
    for slide in slides:
        lines = []
        for line in slide.splitlines():
            key = line.strip()
            if key in footers:
                if key in kept_once:
                    dropped += 1
                    continue
                kept_once.add(key)
            lines.append(line)
        out.append("\n".join(lines) + "\n")

    result = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
    logger.info("PIPELINE: removed %d repeated slide footer line(s) (%s).",
                dropped, ", ".join(sorted(footers))[:200])
    return result


#: What a spreadsheet converter writes into a cell that holds nothing.
#:
#: MarkItDown renders an empty cell as the string "NaN" and an unnamed column
#: as "Unnamed: 7" -- pandas artefacts, not content. A 3-row sheet with a few
#: trailing columns came out at 5,152 characters, almost all of it these two.
#: A live financial model reached 552,541 characters this way and became 70%
#: of a dossier, crowding out the web research and the numbers that mattered.
#:
#: The first version of this only matched a truly blank cell (`|  |  |`), so
#: it fired on decks and did nothing at all to the file it was written for.
_EMPTY_CELL_WORDS = {"", "nan", "nat", "none", "null", "-", "--", "—"}

#: A column header a converter invented because the column has no name.
_UNNAMED_COLUMN = re.compile(r"^unnamed:?\s*\d+$", re.IGNORECASE)

#: A markdown table's header separator: `| --- | --- |`. It carries no data
#: and must survive anyway, or the table stops rendering as a table.
_SEPARATOR_ROW = re.compile(r"^\s*\|[\s|:]*-{2,}[\s|:\-]*\|?\s*$")


def _cells(line):
    """The cells of a markdown table row, without the outer pipes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|")]


def _is_blank_cell(cell):
    return cell.strip().casefold() in _EMPTY_CELL_WORDS


def _is_table_row(line):
    return line.strip().startswith("|")


def _trim_table(rows):
    """One markdown table, with its empty rows and empty columns removed.

    Conservative by construction. A column goes only when its header is one
    the converter invented AND every cell beneath it is empty; a row goes
    only when every cell in it is empty. A row with one figure and thirty
    blanks is a fact about the company and survives whole.
    """
    parsed = [(line, _cells(line), bool(_SEPARATOR_ROW.match(line)))
              for line in rows]
    data = [cells for _line, cells, sep in parsed if not sep]
    if not data:
        return rows

    # NO SEPARATOR, NO HEADER. A converter always writes one; a block of
    # pipe-delimited lines without one is not a table whose first row names
    # its columns, and treating it as one threw away a lone row that carried
    # a figure. Drop the empty rows, keep everything else exactly as it is.
    if not any(sep for _line, _cells_, sep in parsed):
        return [line for line, cells, _sep in parsed
                if not all(_is_blank_cell(cell) for cell in cells)]

    width = max(len(c) for c in data)
    header = data[0] + [""] * (width - len(data[0]))
    body = [c + [""] * (width - len(c)) for c in data[1:]]

    keep = []
    for index in range(width):
        invented = _UNNAMED_COLUMN.match(header[index].strip())
        empty = all(_is_blank_cell(row[index]) for row in body)
        if invented and empty:
            continue
        keep.append(index)
    if not keep:
        return []

    out = []
    for line, cells, sep in parsed:
        if sep:
            out.append("| " + " | ".join("---" for _ in keep) + " |")
            continue
        padded = cells + [""] * (width - len(cells))
        picked = [padded[i] for i in keep]
        if all(_is_blank_cell(cell) for cell in picked):
            continue
        out.append("| " + " | ".join(
            "" if _is_blank_cell(cell) else cell for cell in picked) + " |")
    # A table reduced to nothing but its header and rule says nothing.
    if len([line for line in out if not _SEPARATOR_ROW.match(line)]) <= 1:
        return []
    return out


def _drop_empty_rows(text):
    """Remove the scaffolding a spreadsheet converter emits around the data.

    Empty rows go, invented empty columns go, and a cell holding "NaN" is
    written as the nothing it is. Everything that says anything is kept
    exactly as it was.
    """
    if "|" not in text:
        return text

    lines = text.split("\n")
    out, block = [], []
    for line in lines:
        if _is_table_row(line):
            block.append(line)
            continue
        if block:
            out.extend(_trim_table(block))
            block = []
        out.append(line)
    if block:
        out.extend(_trim_table(block))

    trimmed = "\n".join(out).strip()
    if len(trimmed) < len(text):
        logger.info("PIPELINE: spreadsheet scaffolding removed from a native "
                    "extract (%d -> %d characters).", len(text), len(trimmed))
    return trimmed


def _model_read(path, filename, policy, note, profile):
    """Hand the whole file to the document model and take back its text.

    One call per file, whatever the format. The selective page-by-page logic
    the OCR engines needed is gone with them: it existed because rendering and
    recognising a page was expensive and usually pointless on a digital PDF.
    A model reads the document as a document — layout, tables and the numbers
    inside charts together — so splitting it into pages would throw away the
    context that makes it better than OCR in the first place.
    """
    outcome = ReadOutcome(engines=[document_ai.DOCUMENT_ROLE],
                          units_total=1)
    text = document_ai.read_document(
        path, filename, note=note,
        tenant_id=getattr(profile, "tenant_id", None))
    if len(text) < MIN_USEFUL_TEXT_CHARS:
        outcome.status = "skipped"
        outcome.reason = (f"The document model returned only {len(text)} "
                          f"characters, below the {MIN_USEFUL_TEXT_CHARS}-"
                          f"character floor for useful content.")
        return outcome
    outcome.text = text
    outcome.units_ocred = 1
    outcome.status = "triggered"
    outcome.reason = (f"{policy.unit.capitalize()} content read by the "
                      f"document model from the {os.path.splitext(filename)[1]}"
                      f" file itself, including text that exists only inside "
                      f"images.")
    return outcome


# --- document discovery -----------------------------------------------------


def _storage_uri_for(document_id):
    """The latest version's storage path for a Document, or ``""``."""
    from fundos.docs.models import DocumentVersion

    version = (DocumentVersion.objects
               .filter(document_id=document_id)
               .order_by("-version_no").first())
    return getattr(version, "storage_uri", "") or ""


def _documents_for(profile):
    """Every readable document attached to this profile, in category order.

    Reads the Document Center (``ProfileDocument``) rather than the deal-scoped
    ``MaterialAsset`` table, because the Document Center is what section 8.17
    reports and what the founder believes they uploaded. The linked
    ``MaterialAsset`` is still updated with the extraction detail, so the
    readiness/materials views see the same verdict.

    Read-only migrated Stage-3 artefacts (C7) are excluded: they were generated
    *from* a profile, and feeding them back in as evidence would let the
    system cite itself.
    """
    from fundos.profile.models import ProfileDocument

    rows = (ProfileDocument.objects.filter(profile=profile, is_readonly=False)
            .order_by("category", "sort_order", "filename"))
    found = []
    for row in rows:
        uri = _storage_uri_for(row.document_id) if row.document_id else ""
        found.append({
            "id": str(row.id),
            "filename": row.filename or "(unnamed)",
            "category": row.category,
            "category_label": CATEGORY_LABELS.get(row.category, row.category),
            "material_asset_id": row.material_asset_id,
            "storage_uri": uri,
        })
    order = {key: i for i, key in enumerate(CATEGORY_ORDER)}
    found.sort(key=lambda d: (order.get(d["category"], 99), d["filename"]))
    return found


def _download(storage_uri, filename):
    """Fetch a document's bytes to a temp file. Returns a path, or None.

    The suffix is preserved because both MarkItDown and the OCR dispatch key
    off the extension — a temp file named ``tmpXXXX`` with no suffix would be
    handled as "unknown" and silently skip OCR on a scanned PDF.
    """
    from fundos.docs.storage import get_storage

    suffix = os.path.splitext(filename or "")[1].lower()
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.close()
    try:
        if get_storage().download_file(storage_uri, handle.name):
            return handle.name
    except Exception as exc:
        logger.warning("PIPELINE: could not fetch %s: %s", storage_uri, exc)
    try:
        os.unlink(handle.name)
    except OSError:
        pass
    return None


def _record_extraction(material_asset_id, **fields):
    """Persist per-file extraction detail onto the MaterialAsset row.

    Best-effort and field-tolerant: only columns the model actually has are
    written, so this degrades to a partial update rather than an exception if
    the extraction-detail migration has not been applied yet.
    """
    if not material_asset_id:
        return
    from fundos.readiness.models import MaterialAsset

    try:
        asset = MaterialAsset.objects.filter(id=material_asset_id).first()
        if asset is None:
            return
        writable = [name for name in fields if hasattr(asset, name)]
        for name in writable:
            setattr(asset, name, fields[name])
        if writable:
            asset.save(update_fields=writable)
    except Exception:
        logger.debug("could not record extraction detail for %s",
                     material_asset_id, exc_info=True)


# --- per-file orchestration -------------------------------------------------


def _convert_document(document, position, tracker_, profile=None):
    """Convert one document: markdown subsection, whether it worked, OCR detail.

    Runs native extraction and, unless policy or the OCR switch rules it out,
    OCR — in that order, both when applicable, never short-circuited. Native
    extraction failing does not skip OCR (a PDF MarkItDown cannot parse might
    still be readable by rendering its pages), and OCR failing does not discard
    whatever native text was already recovered. The two signals combine only at
    the end: the file counts as failed only if *neither* method produced text.
    """
    filename = document["filename"]
    policy = policy_for(filename)
    suffix = os.path.splitext(filename)[1].lower() or "(no extension)"
    clock = tracker.Elapsed()

    def note(message):
        tracker_.event(f"{filename}: {message}",
                       stage=tracker.PROCESSING_DOCUMENTS)

    _record_extraction(document["material_asset_id"],
                       extraction_status="pending",
                       handler=policy.handler, file_type=suffix)
    tracker_.event(
        f"reading {position} {filename} ({document['category_label']}, "
        f"{suffix}) — handled as '{policy.handler}': {policy.description}",
        stage=tracker.PROCESSING_DOCUMENTS)

    sections = [f"### {filename}\n",
                f"*{document['category_label']} — {suffix} — handled as "
                f"{policy.handler}*\n"]
    ok = True

    if not document["storage_uri"]:
        sections.append("> This document has no stored file behind it, so "
                        "nothing could be read from it.\n")
        _record_extraction(document["material_asset_id"],
                           extraction_status="failed")
        return "\n".join(sections), False, ReadOutcome(
            status="skipped", reason="no stored file"), 0

    local = _download(document["storage_uri"], filename)
    if not local:
        sections.append("> The stored file could not be fetched, so nothing "
                        "could be read from it.\n")
        _record_extraction(document["material_asset_id"],
                           extraction_status="failed")
        return "\n".join(sections), False, ReadOutcome(
            status="skipped", reason="file could not be fetched"), 0

    try:
        # --- native ------------------------------------------------------
        try:
            native = _convert_with_markitdown(local)
        except ImportError as exc:
            logger.error("PIPELINE: markitdown is not installed: %s", exc)
            sections.append("> Native extraction is unavailable on this host "
                            "(the markitdown package is not installed).\n")
            native = ""
            ok = False
        except Exception as exc:
            logger.warning("PIPELINE: markitdown failed for %s: %s",
                           filename, exc)
            sections.append(f"> Native extraction failed: `{exc}`\n")
            native = ""
            ok = False
        native_seconds = clock.seconds
        if native:
            sections.append("#### Extracted content\n\n" + native + "\n")
        tracker_.event(
            f"native extraction of {filename} recovered {len(native):,} "
            f"characters in {tracker.humanize_duration(native_seconds)}",
            stage=tracker.PROCESSING_DOCUMENTS)

        # --- document model -----------------------------------------------
        clock.reset()
        outcome = _model_stage(local, filename, policy, note,
                               sections, profile)
        ocr_seconds = clock.seconds

        if outcome.text:
            # Labelled, always. A model transcribes a figure that was never
            # on the page more plausibly than an OCR engine garbles one, so
            # a reader has to be able to see which blocks carry that risk.
            sections.append(
                "#### Model-read content\n\n"
                f"*Read from the file itself by the "
                f"{document_ai.DOCUMENT_ROLE} model rather than extracted as "
                f"text. {outcome.reason} Figures transcribed here rank below "
                f"natively extracted text.*\n\n" + outcome.text + "\n")
        elif outcome.status in {"skipped", "disabled"}:
            sections.append(
                f"> Document model not applied: {outcome.reason}\n")

        if not native and not outcome.text and ok:
            sections.append("> No text could be extracted from this file.\n")
            ok = False

        _record_extraction(
            document["material_asset_id"],
            extraction_status="extracted" if ok else "failed",
            native_chars=len(native),
            extraction_seconds=round(native_seconds + ocr_seconds, 2),
            **outcome.as_document_fields(ocr_seconds))

        tracker_.event(
            f"OCR {outcome.status} for {filename}: {outcome.reason}"
            + (f" Recovered {len(outcome.text):,} characters from "
               f"{outcome.units_ocred} unit(s) in "
               f"{tracker.humanize_duration(ocr_seconds)}."
               if outcome.status == "triggered" else ""),
            stage=tracker.PROCESSING_DOCUMENTS)

        return ("\n".join(sections), ok, outcome,
                len(native) + len(outcome.text))
    finally:
        try:
            os.unlink(local)
        except OSError:
            pass


def _model_stage(local, filename, policy, note, sections, profile):
    """Decide whether the document model reads this file, and run it.

    Never raises. A failure leaves whatever native extraction recovered, and
    says which of the two produced the text — the founder's question is "was
    my deck read, and how", and a silent fallback answers neither half.
    """
    if not pipeline_settings.document_model_enabled():
        return ReadOutcome(
            status="disabled",
            reason="Document reading is switched off for this deployment "
                   "(Application configuration -> document model enabled).")
    suffix = os.path.splitext(filename)[1].lower()
    if policy.read_mode == "never":
        return ReadOutcome(
            status="skipped",
            reason=(document_ai.why_not_readable(suffix)
                    or f"{suffix} is handled as '{policy.handler}': "
                       f"{policy.description}"))

    # ASKED FOR, BUT NO PROVIDER ACCEPTS IT. A `.pptx` was sent on every run
    # and rejected on every run -- "Unsupported MIME type" -- so each deck
    # cost a failed call, a CRITICAL log line and a diagnosis pointing at the
    # model binding, which was not the problem. Native extraction had already
    # recovered the slide text either way.
    #
    # Refused here, before the call, with the reason a founder can act on:
    # convert the deck to PDF and its charts get read.
    unreadable = document_ai.why_not_readable(suffix)
    if unreadable:
        logger.info("PIPELINE: %s is not readable by a document model — %s",
                    filename, unreadable)
        sections.append("> The document model did not read this file. "
                        + unreadable + "\n")
        return ReadOutcome(status="skipped", reason=unreadable)
    try:
        return _model_read(local, filename, policy, note, profile)
    except document_ai.DocumentReadError as exc:
        # Expected and reportable: file too large, nothing came back, no key,
        # rate limited. Whatever native extraction found still stands.
        logger.warning("PIPELINE: document model declined %s: %s",
                       filename, exc)
        sections.append(f"> Document model did not read this file: `{exc}`" + "\n")
        return ReadOutcome(
            status="failed",
            reason=f"The document model could not read this file: {exc}",
            error=str(exc))
    except Exception as exc:
        logger.warning("PIPELINE: document model failed for %s: %s",
                       filename, exc)
        sections.append(f"> Document model failed: `{exc}`" + "\n")
        return ReadOutcome(status="failed",
                           reason=f"The document model failed: {exc}",
                           error=str(exc))


def _render(by_category, total, converted, engines_note):
    """Render the whole Source 2 section from whichever files have finished."""
    parts = [f"# {SOURCE_TITLE}\n"]
    if not total:
        parts.append("_No documents were uploaded for this company._\n")
        return "\n\n".join(parts)

    parts.append(f"{converted} of {total} uploaded file(s) yielded text. "
                 f"OCR: {engines_note}. Each file below is labelled with the "
                 f"handler chosen for its type.\n")
    for category in CATEGORY_ORDER:
        subsections = by_category.get(category)
        if not subsections:
            continue
        parts.append(f"## {CATEGORY_LABELS.get(category, category)}\n")
        parts.extend(section.rstrip() for section in subsections)
    return "\n\n".join(parts)


def run(profile, writer, *, tracker_):
    """Convert every uploaded file, updating the dossier after each one.

    Called once per run by the orchestrator, concurrently with Source 1. Files
    are processed strictly one at a time: OCR is CPU-bound, so running them
    concurrently would only contend for the same cores (and for the EasyOCR
    model lock) while competing with the research batches for the GIL.

    :returns: A progress summary for the run row, plus the document rows that
        populate section 8.17.
    """
    documents = _documents_for(profile)
    engines_note = ("disabled in Application configuration"
                    if not pipeline_settings.document_model_enabled()
                    else f"read by the {document_ai.DOCUMENT_ROLE} model")

    by_category = {}
    converted = 0
    ocr_triggered = 0
    ocr_units = 0
    ocr_chars = 0
    text_chars = 0
    rows = []
    clock = tracker.Elapsed()

    if documents:
        plan = "; ".join(f"{d['filename']} -> {policy_for(d['filename']).handler}"
                         for d in documents)
        tracker_.event(f"source 2: {len(documents)} file(s) to process, one at "
                       f"a time. Plan: {plan}",
                       stage=tracker.PROCESSING_DOCUMENTS)
    else:
        tracker_.event("source 2: no documents uploaded, nothing to do",
                       stage=tracker.PROCESSING_DOCUMENTS)

    # Push an initial section so the dossier is well-formed even with no uploads.
    writer.set_section(SOURCE_KEY,
                       _render(by_category, len(documents), 0, engines_note))

    for index, document in enumerate(documents, start=1):
        file_clock = tracker.Elapsed()
        markdown, ok, outcome, chars = _convert_document(
            document, f"file {index}/{len(documents)}", tracker_,
            profile=profile)
        converted += int(ok)
        text_chars += chars
        if outcome.status == "triggered":
            ocr_triggered += 1
            ocr_units += outcome.units_ocred
        ocr_chars += len(outcome.text)
        by_category.setdefault(document["category"], []).append(markdown)
        rows.append({
            "id": document["id"],
            "filename": document["filename"],
            "category": document["category_label"],
            "status": "Processed" if ok else "Failed",
            "handler": policy_for(document["filename"]).handler,
            "ocr_status": outcome.status,
            "ocr_reason": outcome.reason,
            "chars": chars,
        })
        writer.set_section(SOURCE_KEY,
                           _render(by_category, len(documents), converted,
                                   engines_note))
        tracker_.event(
            f"finished file {index}/{len(documents)} ({document['filename']}) "
            f"in {tracker.humanize_duration(file_clock.seconds)}",
            stage=tracker.PROCESSING_DOCUMENTS)

    return {
        "files_total": len(documents),
        "files_converted": converted,
        "files_ocr_triggered": ocr_triggered,
        "ocr_units_processed": ocr_units,
        "ocr_chars": ocr_chars,
        "text_chars": text_chars,
        "ocr_engines": ([document_ai.DOCUMENT_ROLE]
                        if pipeline_settings.document_model_enabled() else []),
        "ocr_enabled": pipeline_settings.document_model_enabled(),
        "documents": rows,
        "duration_seconds": clock.seconds,
    }
