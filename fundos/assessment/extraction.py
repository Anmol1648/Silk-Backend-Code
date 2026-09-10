"""Evidence extraction — documents into ParameterValue rows.

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
Facts have no opinions. Extraction writes `raw_value`, `source_type`,
`source_detail`, `source_tier` and `confidence`, and for numeric parameters it
NEVER writes `band` or `score`. The engine bands from published config. That
boundary is the whole reason the scorecard can be defended to a founder: the
fact came from page 7 of their deck, the threshold came from the published
rubric, and neither was decided by a model.

Anchor-scored (qualitative) parameters are the deliberate exception. There is
no number to threshold, so the model proposes a band and `band_parameter()`
re-validates it against the stored anchor definitions.

DOCUMENT READERS
----------------
The supplied readiness pipeline handled .txt, .csv and .pdf and returned an
empty string for .docx, .xlsx and .pptx. That gap mattered more than it looked:
the financial model is the primary evidence source for category B — 20% of the
score — and .xlsx was the format that could not be read at all.

Spreadsheets are read with coordinates preserved (Sheet!D14) so an extracted
figure can be cited back to a cell rather than to a filename.
"""
import logging
import os

logger = logging.getLogger(__name__)

CONFIDENCE_FLOOR = 0.60
MAX_CHARS_PER_DOC = 500000


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def read_document(path, mime_type=""):
    """Return (text, kind). Never raises — an unreadable document is a warning."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".txt", ".md", ".csv", ".tsv"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[:MAX_CHARS_PER_DOC], "text"
        if ext == ".pdf":
            return _read_pdf(path), "pdf"
        if ext in (".xlsx", ".xlsm", ".xls"):
            return _read_spreadsheet(path), "spreadsheet"
        if ext == ".docx":
            return _read_docx(path), "document"
        if ext == ".pptx":
            return _read_pptx(path), "deck"
    except Exception as e:
        logger.warning("EXTRACT: could not read %s: %s", path, e)
        return "", "unreadable"
    logger.info("EXTRACT: unsupported extension %s for %s", ext, path)
    return "", "unsupported"


def _read_pdf(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    out = []
    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            out.append(f"[page {i}]\n{text}")
    return "\n\n".join(out)[:MAX_CHARS_PER_DOC]


def _read_spreadsheet(path):
    """Read values AND formulas, preserving cell coordinates.

    Coordinates are the point. "ARR is 12.4" is an assertion; "ARR is 12.4,
    from Model.xlsx!P&L!D14" is a citation someone can check in ten seconds.
    Formulas are read too because a labelled formula often names the concept
    better than the computed cell does.
    """
    from openpyxl import load_workbook

    out = []
    values = load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in values.worksheets:
            rows = []
            for row in ws.iter_rows(max_row=min(ws.max_row or 1, 400)):
                cells = []
                for c in row:
                    if c.value is None:
                        continue
                    text = str(c.value).strip()
                    if text:
                        cells.append(f"{c.coordinate}={text}")
                if cells:
                    rows.append(" | ".join(cells[:40]))
            if rows:
                out.append(f"[sheet: {ws.title}]\n" + "\n".join(rows[:400]))
    finally:
        values.close()
    return "\n\n".join(out)[:MAX_CHARS_PER_DOC]


def _read_docx(path):
    from docx import Document
    doc = Document(path)
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for t_i, table in enumerate(doc.tables, 1):
        parts.append(f"[table {t_i}]")
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)[:MAX_CHARS_PER_DOC]


def _read_pptx(path):
    from pptx import Presentation
    prs = Presentation(path)
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        texts.append(" | ".join(cells))
        if texts:
            parts.append(f"[slide {i}]\n" + "\n".join(texts))
    return "\n\n".join(parts)[:MAX_CHARS_PER_DOC]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM = (
    "You extract facts from company documents for an investment scorecard. "
    "You are given document text and a list of parameters, each with its key, "
    "what it means, where to look for it, and the standard it will be judged "
    "against.\n\n"
    "YOU EXTRACT VALUES. YOU DO NOT SCORE THEM. A separate deterministic "
    "engine applies the thresholds. Do not adjust a figure toward a band you "
    "would prefer - report what the evidence says and let it land where it "
    "lands.\n\n"
    "Answer only from the documents. Never estimate, never infer a plausible "
    "figure, never fill a gap with an industry norm. If a document does not "
    "state it, return the row with a null value and the tier "
    "\"Not Evidenced\" - a blank is useful and a fabricated number is not. "
    "An unevidenced row is EXCLUDED from scoring and its weight redistributes "
    "to the rows that were evidenced, so reporting a gap honestly costs the "
    "company nothing.\n\n"
    "Each parameter below carries the cut-points or the written band "
    "definitions it will be judged against. They are shown so you know how "
    "precise the answer has to be, not so you can aim at one.\n\n"
    "TWO KINDS OF PARAMETER, TWO FIELDS:\n"
    "- A NUMERIC parameter wants `value`, a number in the unit asked for. If "
    "your source states it in another unit, convert it and say so in "
    "`justification`. Leave `band` empty - the engine bands it.\n"
    "- A BAND parameter wants `band`, exactly one of Excellent, Good, Fair or "
    "Poor, chosen by which written definition the evidence ACTUALLY "
    "satisfies. Choose the highest band whose written criteria are fully "
    "supported by the evidence.\n"
    "  * DOMAIN RELEVANCE FOR TECH & AI STARTUPS (ANC_FDR_EDU):\n"
    "    In tech-driven, SaaS, AI, or tech-enabled ventures (e.g. Healthtech, "
    "Fintech, Edtech), a degree from a Tier-1 institution (IIT/IIM/ISB/NIT/BITS "
    "or top global equivalent) in Computer Science, Software Engineering, AI, "
    "or Data Science directly powering the core platform or AI engine IS A DIRECT "
    "DOMAIN CREDENTIAL. Select `Excellent` for ANC_FDR_EDU when a founder holds a "
    "Tier-1 degree that directly builds or operates the core tech asset.\n"
    "  * JUSTIFICATION PRECISION:\n"
    "    Write a precise, fact-based `justification` stating exact institutions, "
    "degrees, and operational roles (e.g., 'CTO holds a CS degree from BITS Pilani "
    "(Tier-1) directly powering the core Athena AI platform'). Avoid vague statements "
    "like 'Given the combination, Good is appropriate'.\n"
    "Leave `value` empty unless a number genuinely applies.\n"
    "A band parameter answered with no band is the same as not answering it: "
    "those rows are the qualitative half of the scorecard and they cannot be "
    "scored from a number.\n\n"
    "EVIDENCE TIER - one of four WORDS, never a number:\n"
    "  Verified      you read this in a document and it says exactly this\n"
    "  Management    the company asserts it and nothing independent confirms "
    "it\n"
    "  Estimate      you derived or inferred it rather than reading it\n"
    "  Not Evidenced you could not establish it at all\n"
    "Verified and Management score. Estimate and Not Evidenced are excluded, "
    "which is deliberate - do not avoid a tier to make a row count. A tier "
    "claiming evidence for a value you did not establish is the one failure "
    "this task cannot tolerate.\n\n"
    "CITE EVERY VALUE. Give the file and the place inside it in `locator` - "
    "\"Financial Model.xlsx, Summary!D14\", \"Investor Deck.pptx, slide 12\" "
    "- and in `quote` the text or figures you actually read. A value with "
    "neither is an opinion and will be discarded.\n\n"
    "Where two documents disagree, the financial model beats the deck, and "
    "the deck beats everything else. Never resolve a disagreement silently: "
    "report the winning figure and record the disagreement in "
    "`contradiction`, with both numbers named. A contradiction between the "
    "deck and the model is a finding about the company, not noise.\n\n"
    "A leadership seat nobody mentions is vacant, not unknown: band it Poor "
    "and say the seat was never mentioned. Most companies have no network "
    "effects and no proprietary IP - Poor is an honest answer there and the "
    "engine handles it correctly. Do not inflate to be generous.\n\n"
    "Never report a figure without the period it describes. A forecast "
    "presented as an actual is the most expensive error available here.\n\n"
    'Return JSON only: {"values":[{"inputKey":"","value":null,"band":"",'
    '"unit":"","asOf":"","periodBasis":"Actual|Forecast|Mixed",'
    '"evidenceTier":"Verified|Management|Estimate|Not Evidenced",'
    '"locator":"file and place inside it","quote":"what you actually read",'
    '"confidence":0.0-1.0,"justification":"one sentence",'
    '"contradiction":"","ask":"what to request if unclear"}]}\n'
    "Return one entry for every key you were given. Keep `justification` to "
    "one short sentence: output length is capped, and a long one costs a "
    "parameter that would otherwise have been answered."
)

# Evidence tiers, and the stored source_tier each maps to. `services` and the
# serializer read the number; the model answers in words, because a model
# asked for a number here reliably confuses it with a citation's 1/2/3 source
# tier and writes provenance nobody can act on.
#
# Stated as an allow-list rather than a deny-list, and that is load-bearing:
# an unrecognised tier lands outside the vocabulary and the row is excluded,
# which narrows the assessment visibly. A deny-list would let a typo through
# carrying whatever provenance it failed to state.
EVIDENCE_TIERS = {
    "verified": 1,
    "management": 2,
    "estimate": 3,
    "not evidenced": None,
}

#: The stored tiers that score. Mirrors the engine: 1 Verified and 2
#: Management feed the roll-up, 3 Estimate does not.
SCORING_TIERS = (1, 2)


VALID_BANDS = ("Excellent", "Good", "Fair", "Poor")


def _band(value):
    """One of the four bands, or None.

    Exceptional is deliberately absent: it is override-only (2.4.4), and a
    model that returns it would otherwise mint a 10/10 with no override record
    and no written justification. `services.validate_automated_band` guards
    the same boundary one layer down; this stops the value being stored at
    all.
    """
    text = str(value or "").strip().title()
    return text if text in VALID_BANDS else None


def _evidence_tier(value):
    """The stored source_tier for a reported evidence tier.

    Returns None for a tier that does not score, including anything outside
    the vocabulary -- a row whose tier could not be read is excluded rather
    than admitted on provenance nobody can describe. A model that answered
    with a number has confused this with a citation's source tier, and there
    is no defensible mapping between them: "source tier 1" says the document
    was a filing, not that this parameter was verified against it.
    """
    text = str(value or "").strip().lower()
    return EVIDENCE_TIERS.get(text)


def extract_for_assessment(assessment):
    """Run the question set over this deal's documents.

    Returns a counts dict. Writes ParameterValue rows with full provenance and
    no scores — the engine bands them afterwards.
    """
    from fundos.assessment.models import ConfigParameter, ParameterValue

    deal = assessment.deal
    if deal is None:
        return {"documents": 0, "values": 0}

    documents = _deal_documents(deal)
    if not documents:
        logger.info("EXTRACT %s: no documents to read.", assessment.id)
        return {"documents": 0, "values": 0}

    corpus, sources = [], []
    for doc in documents:
        # Files live in object storage, which on a deployed stack is not the
        # local filesystem at all. Pulled to a temporary file so the readers
        # below — which take a path — work identically in both places.
        local = _fetch(doc["storage_uri"])
        if not local:
            logger.warning("EXTRACT %s: could not retrieve %s from storage.",
                           assessment.id, doc["name"])
            continue
        try:
            text, kind = read_document(local)
        finally:
            try:
                os.unlink(local)
            except OSError:
                pass
        if not text:
            logger.warning(
                "EXTRACT %s: %s was retrieved but yielded no text.",
                assessment.id, doc["name"])
            continue
        logger.info("EXTRACT %s: read %s (%s) — %d characters.",
                    assessment.id, doc["name"], kind, len(text))
        corpus.append(f"===== {doc['name']} ({kind}) =====\n{text}")
        sources.append({"name": doc["name"], "kind": kind, "id": doc["id"]})

    if not corpus:
        logger.info("EXTRACT %s: documents present but none readable.",
                    assessment.id)
        return {"documents": len(documents), "values": 0, "readable": 0}

    tenant_id = getattr(assessment, "tenant_id", None)
    questions = _question_set(tenant_id)
    if not questions:
        logger.warning("EXTRACT %s: no extraction questions configured.",
                       assessment.id)
        return {"documents": len(documents), "values": 0}

    counts = {"documents": len(documents), "readable": len(corpus),
              "values": 0, "low_confidence": 0, "unanswered": 0,
              "not_evidenced": 0, "bands": 0}

    # Domain-grouped batches matching Fundraising Strategy V2.0 architecture
    # (people_and_deal: A+D, financials_and_quality: B+C, market: E+F)
    batches = _domain_batches(questions)
    from concurrent.futures import ThreadPoolExecutor

    def _fetch_batch_answers(b_name, b_items):
        return _ask(corpus, b_items, assessment, focus=b_name)

    answers = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(_fetch_batch_answers, b_name, b_items)
                   for b_name, b_items in batches]
        for f in futures:
            try:
                answers.extend(f.result() or [])
            except Exception as e:
                logger.error("EXTRACT %s batch failed: %s", assessment.id, e, exc_info=True)

    for a in answers:
            key = a.get("inputKey")
            cfg = questions_by_key(questions).get(key)
            if not cfg:
                continue
            raw = a.get("value")
            if raw in (None, "", "null"):
                raw = a.get("rawValue")

            # A band parameter answers with a band and often no number at all.
            # The old test looked only at the value, so every one of the 24
            # anchor-scored rows was counted unanswered and dropped before it
            # reached the engine -- which then found `pv.band` empty and left
            # the row unscored for good. Those rows are the qualitative half
            # of the scorecard.
            band = _band(a.get("band"))
            if band and getattr(cfg, "scoring_type", "") != "anchor":
                # A band offered for a numeric row is not the engine's to use:
                # it bands those from the published cut-points. Dropped rather
                # than stored, so a model's opinion cannot displace a rubric.
                band = None
            if raw in (None, "", "null") and not band:
                counts["unanswered"] += 1
                continue

            try:
                confidence = float(a.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < CONFIDENCE_FLOOR:
                counts["low_confidence"] += 1

            # The tier the model reported, not a constant. Every document row
            # used to be written source_tier=1 -- "Verified" -- whether it was
            # read off a slide or derived from two other figures, so the
            # provenance shown beside a number was a claim nobody had made.
            tier = _evidence_tier(a.get("evidenceTier"))
            if tier is None:
                counts["not_evidenced"] = counts.get("not_evidenced", 0) + 1
                logger.info(
                    "EXTRACT %s: %s reported tier %r, which does not score. "
                    "The row is excluded and its weight redistributes.",
                    assessment.id, key, a.get("evidenceTier"))
                continue

            locator = (a.get("locator") or a.get("sourceDetail") or "")
            quote = (a.get("quote") or "")
            if quote:
                locator = ("%s - %s" % (locator, quote))[:2000] if locator \
                    else quote[:2000]

            # Written through the bridge's merge so precedence lives in ONE
            # place. A document beats research, but never a value the founder
            # has explicitly confirmed.
            from fundos.assessment.profile_bridge import merge_value
            wrote = merge_value(
                assessment, key,
                raw_value=str(raw) if raw not in (None, "", "null") else "",
                unit=(a.get("unit") or getattr(cfg, "unit", "") or ""),
                source_type="document",
                source_detail=locator,
                source_tier=tier,
                confidence=round(confidence, 2),
                justification=(a.get("justification") or ""),
                band=band)
            if band:
                counts["bands"] += 1
            if not wrote:
                counts["kept_existing"] = counts.get("kept_existing", 0) + 1
                continue

            # Fields the merge does not own: the config-derived identity of
            # the row, and the follow-up question to put to the founder.
            ParameterValue.objects.filter(
                assessment=assessment, input_key=key).update(
                    ref_code=getattr(cfg, "ref_code", "") or "",
                    category=getattr(cfg, "category_code", "") or "",
                    notes=(a.get("ask") or "")[:2000])
            counts["values"] += 1

    logger.info("EXTRACT %s: %s", assessment.id, counts)
    return counts


def questions_by_key(questions):
    return {q.input_key: q for q in questions}


def _question_set(tenant_id):
    """Config parameters that feed the score, with their extraction question."""
    from fundos.assessment.models import ConfigParameter
    base = {p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id__isnull=True, is_active=True, feeds_score=True)}
    base.update({p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id=tenant_id, is_active=True, feeds_score=True)})
    return list(base.values())


def _parameter_block(cfg, stage, anchors, rubrics):
    """One parameter's full specification: what it is, where to look, how it is judged.

    The old prompt sent `- KEY: question (unit: X)` and nothing else, so the
    model was asked for 20 values with no idea what any of them meant, where
    the answer lived, or how precise it had to be. Everything below is already
    in the config and was simply never shown to it: the workbook's own
    definition, its where-to-find note, the stage cut-points, and for a band
    parameter the four written definitions it is supposed to choose between.
    """
    key = cfg.input_key
    parts = ['  "%s" - %s (%s)\n' % (key, cfg.name or key,
                                     cfg.ref_code or key)]

    if not cfg.feeds_score:
        parts.append(
            "    CROSS-CHECK ROW. Does not score by itself; it is compared "
            "against its primary parameter to surface contradictions. Extract "
            "it with the same care.\n")
    if cfg.definition:
        parts.append("    Definition: %s\n" % cfg.definition)
    if cfg.where_to_find:
        parts.append("    Where to look: %s\n" % cfg.where_to_find)

    anchor = anchors.get(key)
    if cfg.scoring_type == "anchor":
        parts.append('    Report "band" as one of: Excellent, Good, Fair, '
                     'Poor.\n')
        parts.append(_anchor_block(anchor))
    else:
        unit = cfg.unit or ""
        parts.append('    Report "value" as a number%s.\n'
                     % (" in %s" % unit if unit else ""))
        if unit == "%":
            parts.append("    Percentages are whole numbers: report 42.5 for "
                         "42.5%, not 0.425.\n")
        parts.append(_rubric_block(rubrics.get(key)))
        if anchor is not None:
            # The written definitions are judgement CONTEXT for a numeric row
            # -- they say what a good answer looks like without inviting the
            # model to report a band the engine would then ignore.
            parts.append("    Judgement context for this number (do not "
                         "report a band here):\n")
            parts.append(_anchor_block(anchor))

    if cfg.if_missing:
        parts.append("    If the data is missing: %s\n" % cfg.if_missing)
    return "".join(parts)


def _anchor_block(anchor):
    """The four written band definitions, so a band is chosen against a standard."""
    if anchor is None:
        return ""
    lines = ["    Choose exactly one band by which definition the evidence "
             "actually satisfies. If the evidence does not clearly meet a "
             "band, choose the one below it.\n"]
    for label, field in (("Excellent", "excellent_def"), ("Good", "good_def"),
                         ("Fair", "fair_def"), ("Poor", "poor_def")):
        text = getattr(anchor, field, "") or ""
        if text:
            lines.append("      %s: %s\n" % (label, text))
    if getattr(anchor, "evidence_required", ""):
        lines.append("    Evidence to record: %s\n" % anchor.evidence_required)
    return "".join(lines)


def _rubric_block(rubric):
    """The cut-points this value will be banded against, at this stage.

    Shown for the reason the V2.0 prompt states: without them the model cannot
    tell which PRECISION matters. "Revenue is roughly 70-80 Cr" is a fine
    answer to "what is revenue?" and a useless one when the Excellent
    cut-point sits at 75.
    """
    if rubric is None:
        return ""
    if (rubric.direction or "") == "Range":
        return ("    Scored as a sweet spot at %s: the ideal band is %s to "
                "%s. Both too little and too much score badly.\n"
                % (rubric.stage, rubric.ideal_min, rubric.ideal_max))
    direction = ("higher is better" if (rubric.direction or "") == "Higher"
                 else "lower is better")
    return ("    Scored at %s (%s): Excellent %s, Good %s, Fair %s.\n"
            % (rubric.stage, direction, rubric.cut_excellent,
               rubric.cut_good, rubric.cut_fair))


def _domain_batches(questions):
    """Group extraction questions into domain category pairs.

    Mirrors Fundraising Strategy V2.0 architecture:
    - people_and_deal: Categories A & D (Team & Deal Dynamics)
    - financials_and_quality: Categories B & C (Financials & Business Quality)
    - market: Categories E & F (Sector & Competitive Landscape)
    """
    category_map = {
        ("A", "D"): [],
        ("B", "C"): [],
        ("E", "F"): [],
    }
    other = []
    for q in questions:
        cat = (getattr(q, "category_code", "") or "").upper()
        matched = False
        for cats, q_list in category_map.items():
            if cat in cats:
                q_list.append(q)
                matched = True
                break
        if not matched:
            other.append(q)

    out = []
    if category_map[("A", "D")]:
        out.append(("people_and_deal", category_map[("A", "D")]))
    if category_map[("B", "C")]:
        out.append(("financials_and_quality", category_map[("B", "C")]))
    if category_map[("E", "F")]:
        out.append(("market", category_map[("E", "F")]))
    if other:
        for chunk in _chunks(other, 10):
            out.append(("other", chunk))
    return out


def _config_for(batch, stage, tenant_id):
    """The anchors and stage rubrics for one batch, indexed by input key."""
    from fundos.assessment.models import ConfigAnchor, ConfigRubric

    keys = [q.input_key for q in batch]
    anchors = {a.input_key: a for a in ConfigAnchor.objects.filter(
        input_key__in=keys, is_active=True).order_by("tenant_id")}
    rubrics = {r.input_key: r for r in ConfigRubric.objects.filter(
        input_key__in=keys, stage=stage, is_active=True).order_by("tenant_id")}
    return anchors, rubrics


def _ask(corpus, batch, assessment, focus=""):
    from fundos.llm.adapter import llm_generate

    stage = (getattr(assessment, "deal_stage", "") or "Series A")
    tenant_id = getattr(assessment, "tenant_id", None)
    anchors, rubrics = _config_for(batch, stage, tenant_id)

    specs = "".join(_parameter_block(q, stage, anchors, rubrics)
                    for q in batch)
    keys = ", ".join(q.input_key for q in batch)

    focus_header = f"FOCUS AREA: {focus}\n" if focus else ""
    prompt = (
        "%sRound being raised: %s. Every threshold below is stated for this "
        "round.\n\nPARAMETERS\n%s\nReturn one entry for each of these %d "
        "keys: %s\n\nDOCUMENTS\n\n%s"
        % (focus_header, stage, specs, len(batch), keys,
           "\n\n".join(corpus)[:MAX_CHARS_PER_DOC]))

    try:
        raw = llm_generate(role="assessment_extraction",
                           system=EXTRACTION_SYSTEM, prompt=prompt,
                           context={"assessmentId": str(assessment.id)})
    except Exception as e:
        logger.error("EXTRACT %s: call failed: %s", assessment.id, e,
                     exc_info=True)
        return []

    from fundos.assessment.tasks import _coerce
    data = _coerce(raw) or {}
    return data.get("values") or []


def _fetch(storage_uri):
    """Pull one stored file to a local temp path, or return "".

    The readers take a filesystem path, and object storage is not one. Kept
    separate so a storage failure names the file it could not retrieve
    instead of disappearing into "no documents to read".
    """
    import tempfile

    try:
        from fundos.docs.storage import get_storage
        suffix = os.path.splitext(storage_uri)[1] or ".bin"
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        if get_storage().download_file(storage_uri, tmp):
            return tmp
        try:
            os.unlink(tmp)
        except OSError:
            pass
    except Exception:
        logger.warning("EXTRACT: storage read failed for %s", storage_uri,
                       exc_info=True)
    return ""


def _deal_documents(deal):
    """The uploaded files for this deal, as (name, storage_uri) pairs.

    THIS RETURNED [] FOR EVERY DEAL THAT HAS EVER EXISTED.
    ------------------------------------------------------
    Three speculative reverse accessors — `deal.documents`, `.materials`,
    `.uploads` — none of which the Deal model has, followed by
    `Document.objects.filter(deal=deal)`. `Document.deal_id` is a plain
    UUIDField and not a ForeignKey, so that filter raises

        FieldError: Cannot resolve keyword 'deal' into field

    and the bare `except Exception: return []` turned it into "this deal has
    no documents". Silently, on every run, with an INFO line reading "no
    documents to read" that looked like a fact about the deal.

    The cost was category B. Financials is deliberately excluded from step 1
    research (`assessment_extraction.NOT_RESEARCHABLE`) because reading
    revenue off a marketing page is the worst thing this system could do — so
    revenue, margins, runway and debt were supposed to come from HERE, from
    the founder's own uploaded model. They came from nowhere. A company could
    upload a 4 MB financial model, watch the pipeline report 552,541
    characters extracted from it, and still see all nine Financials rows
    blank.

    Documents are addressed by `deal_id` and their bytes live on the current
    DocumentVersion's `storage_uri`, not on the Document row, so both hops
    are made here rather than guessed at by the caller.
    """
    from fundos.docs.models import Document, DocumentVersion

    docs = list(Document.objects.filter(deal_id=deal.id, is_deleted=False))
    if not docs:
        return []

    versions = {v.document_id: v for v in DocumentVersion.objects.filter(
        id__in=[d.current_version_id for d in docs if d.current_version_id])}
    out = []
    for doc in docs:
        version = versions.get(doc.id)
        if version is None or not version.storage_uri:
            logger.warning(
                "EXTRACT: document %s (%s) has no stored version, so it "
                "cannot be read.", doc.id, doc.title)
            continue
        out.append({"id": str(doc.id),
                    "name": doc.title or str(doc.id),
                    "storage_uri": version.storage_uri})
    return out


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]
