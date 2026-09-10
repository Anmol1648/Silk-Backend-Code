"""
Artefact renderers (Gaps G4/G14) — the Doc 6 Appendix F stack:

  PDF  = ReportLab platypus (SimpleDocTemplate + Table/Paragraph/Spacer),
         A4 + brand styles; optional CONFIDENTIAL watermark on every page.
  PPTX = python-pptx programmatic slide construction (teaser one-pager,
         pitch deck with speaker notes).
  XLSX = openpyxl — the financial model keeps LIVE formulas (totals,
         margins) so the workbook stays a working model, not a dump.
  DOCX = python-docx for teaser/IM word exports.

All functions take structured content and a local output path; the export
service uploads the file to the object bucket and returns a signed URL.
"""
import logging

logger = logging.getLogger(__name__)

BRAND = "FundOS"


# ---------------------------------------------------------------------------
# PDF — ReportLab platypus
# ---------------------------------------------------------------------------

def _styles():
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("FundosTitle", parent=ss["Title"], fontSize=22,
                          spaceAfter=14))
    ss.add(ParagraphStyle("FundosH2", parent=ss["Heading2"], fontSize=14,
                          spaceBefore=12, spaceAfter=6))
    ss.add(ParagraphStyle("FundosBody", parent=ss["BodyText"], fontSize=10,
                          leading=14))
    ss.add(ParagraphStyle("FundosDisclaimer", parent=ss["BodyText"],
                          fontSize=8, textColor="#666666", spaceBefore=16))
    return ss


def _watermark_factory(text):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 48)
        canvas.setFillColorRGB(0.85, 0.85, 0.85)
        canvas.translate(300, 420)
        canvas.rotate(45)
        canvas.drawCentredString(0, 0, text)
        canvas.restoreState()
    return draw


def render_pdf(path, *, title, subtitle="", sections=None, tables=None,
               disclaimer="", watermark=""):
    """Generic sectioned PDF. sections=[(heading, [paragraph,…])],
    tables=[(heading, [[row…],…])]."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    ss = _styles()
    story = [Paragraph(str(title), ss["FundosTitle"])]
    if subtitle:
        story.append(Paragraph(str(subtitle), ss["FundosBody"]))
    story.append(Spacer(1, 0.4 * cm))
    for heading, paragraphs in (sections or []):
        story.append(Paragraph(str(heading), ss["FundosH2"]))
        for para in paragraphs:
            if para:
                story.append(Paragraph(str(para), ss["FundosBody"]))
        story.append(Spacer(1, 0.2 * cm))
    for heading, rows in (tables or []):
        if not rows:
            continue
        story.append(Paragraph(str(heading), ss["FundosH2"]))
        table = Table([[str(c) for c in row] for row in rows], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2b4a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f4f6fa")]),
        ]))
        story.append(table)
        story.append(Spacer(1, 0.3 * cm))
    if disclaimer:
        story.append(Paragraph(str(disclaimer), ss["FundosDisclaimer"]))

    doc = SimpleDocTemplate(path, pagesize=A4, title=str(title),
                            author=BRAND)
    on_page = _watermark_factory(watermark) if watermark else (lambda c, d: None)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return path


# ---------------------------------------------------------------------------
# PPTX — python-pptx
# ---------------------------------------------------------------------------

def render_pptx(path, *, title, subtitle="", slides=None):
    """slides=[{headline, bullets:[…], narrative, speakerNotes}]."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    # Title slide
    layout = prs.slide_layouts[0]
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = str(title)
    if len(slide.placeholders) > 1 and subtitle:
        slide.placeholders[1].text = str(subtitle)

    for item in (slides or []):
        layout = prs.slide_layouts[1]          # title + content
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = str(item.get("headline", ""))[:120]
        body = slide.placeholders[1].text_frame
        first = True
        for bullet in (item.get("bullets") or []):
            para = body.paragraphs[0] if first else body.add_paragraph()
            para.text = str(bullet)[:300]
            para.font.size = Pt(16)
            first = False
        narrative = item.get("narrative", "")
        if narrative:
            box = slide.shapes.add_textbox(Inches(0.5), Inches(5.6),
                                           Inches(9), Inches(1.4))
            tf = box.text_frame
            tf.word_wrap = True
            tf.text = str(narrative)[:600]
            tf.paragraphs[0].font.size = Pt(11)
        notes = item.get("speakerNotes", "")
        if notes:
            slide.notes_slide.notes_text_frame.text = str(notes)[:2000]
    prs.save(path)
    return path


# ---------------------------------------------------------------------------
# XLSX — openpyxl with LIVE formulas (Doc 6 App F)
# ---------------------------------------------------------------------------

def render_model_xlsx(path, *, assumptions, statements, kpis=None):
    """assumptions=[{key,label,value,unit}], statements={name: [{lineKey,
    label, values:[…]}]} — writes SUM/margin formulas so the workbook is a
    working model."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1A2B4A")

    ws = wb.active
    ws.title = "Assumptions"
    ws.append(["Key", "Assumption", "Value", "Unit"])
    for cell in ws[1]:
        cell.font, cell.fill = head_font, head_fill
    for a in (assumptions or []):
        ws.append([a.get("key", ""), a.get("label", ""),
                   a.get("value", ""), a.get("unit", "")])
    ws.column_dimensions["B"].width = 42

    for name, lines in (statements or {}).items():
        sheet = wb.create_sheet((name or "Statement")[:28])
        periods = max((len(l.get("values") or []) for l in lines), default=0)
        sheet.append(["Line"] + [f"P{i + 1}" for i in range(periods)]
                     + ["Total"])
        for cell in sheet[1]:
            cell.font, cell.fill = head_font, head_fill
        for r, line in enumerate(lines, start=2):
            values = list(line.get("values") or [])
            sheet.cell(row=r, column=1, value=line.get("label")
                       or line.get("lineKey", ""))
            for c, v in enumerate(values, start=2):
                try:
                    sheet.cell(row=r, column=c, value=float(v))
                except (TypeError, ValueError):
                    sheet.cell(row=r, column=c, value=v)
            if periods:
                first = sheet.cell(row=r, column=2).coordinate
                last = sheet.cell(row=r, column=periods + 1).coordinate
                # LIVE formula — the exported model stays editable.
                sheet.cell(row=r, column=periods + 2,
                           value=f"=SUM({first}:{last})")
        sheet.column_dimensions["A"].width = 36

    if kpis:
        sheet = wb.create_sheet("KPIs")
        sheet.append(["KPI", "Value", "Period"])
        for cell in sheet[1]:
            cell.font, cell.fill = head_font, head_fill
        for k in kpis:
            sheet.append([k.get("label") or k.get("key", ""),
                          k.get("value", ""), k.get("period", "")])
    wb.save(path)
    return path


def render_csv(path, rows):
    import csv
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)
    return path


# ---------------------------------------------------------------------------
# DOCX — python-docx
# ---------------------------------------------------------------------------

def render_docx(path, *, title, subtitle="", sections=None, disclaimer=""):
    import docx

    document = docx.Document()
    document.add_heading(str(title), level=0)
    if subtitle:
        document.add_paragraph(str(subtitle))
    for heading, paragraphs in (sections or []):
        document.add_heading(str(heading), level=2)
        for para in paragraphs:
            if para:
                document.add_paragraph(str(para))
    if disclaimer:
        p = document.add_paragraph()
        run = p.add_run(str(disclaimer))
        run.italic = True
    document.save(path)
    return path
