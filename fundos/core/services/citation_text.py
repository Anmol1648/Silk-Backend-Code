"""The text of a citation as a reader should see it.

A citation's quote reaches the screen from two places — the profile's field
sources panel and the assessment's evidence panel — and both were showing
whatever the dossier or the model happened to contain:

    | FY24 | 56.9 |  | NaN |
    **Khushboo Aggarwal** — [LinkedIn](https://…) _unverified_
    <!-- Slide number: 20 --> # Leadership team

The evidence is in there, under a layer of markdown a reader has to decode
first. This module removes the layer and changes nothing underneath it: no
word is rewritten, no figure is rounded, nothing is summarised. A table row
becomes "FY24 — Revenue: 56.9"; a link becomes its text; markup goes.

One function, used by both panels, so the same evidence reads the same way
wherever it is shown.
"""
import re

#: A cell that holds nothing: what a spreadsheet converter writes for empty.
_EMPTY_CELL = {"", "nan", "nat", "none", "null", "-", "--", "—"}

#: `| --- | :---: |` — a table's header rule, carrying no data.
_RULE_CELL = re.compile(r"^:?-{2,}:?$")

#: A column header a converter invented: "Unnamed: 7".
_UNNAMED = re.compile(r"^unnamed:?\s*\d+$", re.IGNORECASE)

#: Alt text that describes nothing: a filename or a generic word.
_GENERIC_ALT = re.compile(
    r"^\s*(?:[\w\- ]*\.(?:png|jpe?g|gif|bmp|svg|webp|tiff?|emf|wmf)"
    r"|(?:image|picture|pic|img|graphic|shape|googleshape|photo|figure|"
    r"object|diagram|chart)\s*[\w\-]*)?\s*$",
    re.IGNORECASE)

#: Longest a cleaned citation is allowed to run before it is cut.
MAX_CHARS = 900


def _cells(row):
    body = row.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _is_rule(cells):
    return bool(cells) and all(_RULE_CELL.match(c) or not c for c in cells)


def _blank(cell):
    return cell.strip().lower() in _EMPTY_CELL


def _render_table(rows):
    """A markdown table as readable lines: "FY24 — Revenue: 56.9 · PAT: 3.1".

    A header row is recognised by the rule beneath it, and its labels are
    paired with each value so a figure is never shown without what it
    measures. Without a header the non-empty cells are listed in order.
    """
    parsed = [_cells(r) for r in rows]
    header = None
    body = parsed
    for i, cells in enumerate(parsed):
        if _is_rule(cells) and i > 0:
            header = parsed[i - 1]
            body = parsed[:i - 1] + parsed[i + 1:]
            break
    body = [c for c in body if not _is_rule(c)]

    lines = []
    for cells in body:
        values = [(i, c) for i, c in enumerate(cells) if not _blank(c)]
        if not values:
            continue
        if header:
            label_index, label = values[0]
            pairs = []
            for i, value in values[1:]:
                name = header[i].strip() if i < len(header) else ""
                if name and not _blank(name) and not _UNNAMED.match(name):
                    pairs.append(f"{name}: {value}")
                else:
                    pairs.append(value)
            lines.append(f"{label} — {' · '.join(pairs)}" if pairs else label)
        else:
            lines.append(" · ".join(v for _, v in values))
    return lines


def _tables_to_lines(text):
    """Every table block in `text` rendered as readable lines."""
    out, block = [], []
    for line in text.split("\n"):
        if line.strip().startswith("|"):
            block.append(line)
            continue
        if block:
            out.extend(_render_table(block))
            block = []
        out.append(line)
    if block:
        out.extend(_render_table(block))
    return "\n".join(out)


def _flat_table(text):
    """A table already flattened onto one line, as a stored quote often is.

    "| FY24 | 56.9 |  | NaN |" has lost its row breaks, so pairing with a
    header is not possible; the non-empty cells are kept in order.
    """
    cells = [c.strip() for c in text.split("|")]
    kept = [c for c in cells if c and not _blank(c) and not _RULE_CELL.match(c)]
    return " · ".join(kept)


def _image(match):
    alt = match.group(1).strip()
    if not alt or _GENERIC_ALT.match(alt):
        return ""
    return f"Image: {alt}"


def _join(lines):
    """Lines as one passage: a wrapped sentence rejoined with a space, a
    heading or a table row separated from what follows with a semicolon."""
    out = ""
    for line in lines:
        if not out:
            out = line
        elif out[-1] in ".!?:;,…" or line[:1].islower():
            out += " " + line
        else:
            out += "; " + line
    return out


def clean(text, *, limit=MAX_CHARS):
    """A citation's text with the markup removed and nothing else changed.

    Never raises and never returns None: a panel that fails to render
    because a quote was a number or missing is worse than an empty string.
    """
    if text is None:
        return ""
    text = str(text)
    if not text.strip():
        return ""

    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)

    # An extract's cut marks travel on their own lines; set them aside so
    # they neither break a table nor get joined in with a separator.
    rows = text.split("\n")
    lead = bool(rows) and rows[0].strip() in ("…", "...")
    tail = len(rows) > 1 and rows[-1].strip() in ("…", "...")
    text = "\n".join(rows[1 if lead else 0:len(rows) - 1 if tail else len(rows)])

    # Tables first, while the row breaks that make them tables still exist.
    if "\n" in text and "|" in text:
        text = _tables_to_lines(text)
    elif text.count("|") >= 2:
        text = _flat_table(text)

    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", _image, text)
    text = re.sub(r"\[image:\s*([^\]]+)\]", r"Image: \1", text)
    text = re.sub(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]*\)", r"\1", text)

    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or re.fullmatch(r"[-*_=]{3,}", stripped):
            continue
        stripped = re.sub(r"^#{1,6}\s+", "", stripped)
        stripped = re.sub(r"^>\s?", "", stripped)
        stripped = re.sub(r"^(?:[-*+•]|\d{1,2}[.)])\s+", "", stripped)
        lines.append(stripped)
    text = _join(lines)

    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    # Single `*` or `_` emphasis only where it wraps words — never inside a
    # filename like Project_Orah_Model.xlsx, where the underscore is content.
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w])_(?!\s)([^_]+?)(?<!\s)_(?![\w])", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"\s*;\s*;\s*", "; ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ;")

    if limit and len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        text = text[:cut if cut > limit // 2 else limit].rstrip(" ;,")
        tail = True
    if not text:
        return ""
    return ("… " if lead else "") + text + (" …" if tail else "")
