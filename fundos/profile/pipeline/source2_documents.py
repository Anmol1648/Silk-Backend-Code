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
    ".pptx": TypePolicy(
        handler="native+model",
        description=("MarkItDown extracts the slide text and speaker notes, "
                     "and the deck is additionally read by a document model, "
                     "because charts and screenshots carry numbers that exist "
                     "only as pixels."),
        read_mode="model", unit="slide"),
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
    return (getattr(result, "text_content", None) or "").strip()


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
    if policy.read_mode == "never":
        suffix = os.path.splitext(filename)[1].lower()
        return ReadOutcome(
            status="skipped",
            reason=f"{suffix} is handled as '{policy.handler}': "
                   f"{policy.description}")
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
