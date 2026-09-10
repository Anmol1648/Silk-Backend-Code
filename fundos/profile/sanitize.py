"""The boundary where model-authored data becomes data this system trusts.

Everything the synthesis call returns passes through here before
:func:`fundos.profile.schema.normalize_profile` stores it. The contract this
module enforces is narrow and absolute: **what comes out is JSON-native, typed
as the field spec declares, bounded in size, and free of markup.**

Why a separate module rather than a few coercers next to the normalizer
----------------------------------------------------------------------
Three separate live defects traced back to the absence of exactly this layer,
and all three were invisible in the run record because each produced a
*plausible* value:

* **Markdown leaked into JSON string fields.** A model told to return JSON
  still writes prose the way it writes prose — ``"**Zyla Health** is India's
  *highest-rated* care management platform"``. Stored verbatim, those asterisks
  reach a PDF export, a PPTX text frame and a DOCX run, none of which render
  markdown, so the emphasis a model added for a human reader becomes literal
  punctuation in an investor-facing document.

* **A list arrived in a scalar field.** ``sub_sector`` came back as
  ``"Telemedicine, Patient Engagement, Medical AI, InsurTech, Personalized Care
  Management"``. Every lookup in
  :func:`fundos.profile.assessment_extraction.resolve_sub_sector` — exact,
  clubbed-group, name, then fuzzy at a ≥88 floor — misses a five-term
  concatenation, so the company silently got no benchmark group and category F
  of the scorecard was dropped from the denominator.

* **One field held three representations of "absent".** The competitors array
  shipped ``revenue: ""`` beside ``revenue: 28.5`` beside
  ``funding_usd_mn: null``, against a spec that says ``number|null``. Any
  client doing arithmetic gets ``NaN`` on the rows that used the empty string.

The through-line is that a *shape* mismatch and a *content* mismatch are the
same class of problem, and the only place to fix them once is the seam where
untrusted output crosses into storage.

Coercion is driven by the declared spec
---------------------------------------
:func:`coerce_to_spec` reads the field's own specification string — the same
text :func:`fundos.profile.schema.schema_prompt_block` renders into the prompt —
and coerces against it. So a field added in Django admin is type-checked on the
way back with no second declaration to keep in step, which is the property the
schema module already guarantees for the prompt and the normalizer.

Everything here is total: no function raises, and every one has a defined
answer for ``None``, for the wrong type, and for a value far larger than any
legitimate input. A sanitizer that throws is a sanitizer that turns a cosmetic
model slip into a failed run.
"""
import logging
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

logger = logging.getLogger("fundos.profile")

# --- resource bounds --------------------------------------------------------
#
# A model can emit an arbitrarily large or arbitrarily deep structure, and this
# runs inside a worker holding a database connection. These are not tuning
# knobs; they are the difference between a bad response costing one section and
# a bad response costing the process. Each is far above any legitimate value —
# the largest real field is a four-paragraph business description — so hitting
# one is itself evidence of a malformed response and is logged as such.

MAX_DEPTH = 12
"""Nesting depth before a subtree is discarded. The deepest legitimate shape in
the schema is section → data → array → object → array → object (6)."""

MAX_STRING_CHARS = 40_000
"""Per-string ceiling. `description_of_business` is the longest field the
schema asks for and runs to a few thousand characters."""

MAX_LIST_ITEMS = 1_000
"""Per-array ceiling. The longest real array observed is ~30 company metrics."""

MAX_DICT_KEYS = 500
"""Per-object ceiling, so an object used as a map cannot grow without bound."""

MAX_TOTAL_NODES = 200_000
"""Whole-tree ceiling, checked as the walk proceeds. The per-container caps
above bound each level; without this, a wide-but-legal tree could still
multiply out."""


class _Budget:
    """Mutable node counter threaded through one sanitize pass.

    A plain integer cannot carry a decrement back up the recursion, and a
    module-level counter would be shared between the concurrently-running
    generations this package explicitly supports. One instance per call keeps
    the accounting per-tree and thread-safe by construction.
    """

    __slots__ = ("remaining", "truncations")

    def __init__(self, limit=MAX_TOTAL_NODES):
        self.remaining = int(limit)
        self.truncations = 0

    def take(self):
        self.remaining -= 1
        return self.remaining > 0

    def note(self):
        self.truncations += 1


# --- markdown stripping -----------------------------------------------------
#
# Ordered deliberately: fenced blocks before inline code (a fence contains
# backticks), links before emphasis (link text may be emphasised), and emphasis
# last, once no delimiter can still be part of a larger construct.

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")
_AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_SETEXT_RE = re.compile(r"^[ \t]{0,3}(=|-){3,}[ \t]*$", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]{0,4}[-*+][ \t]+", re.MULTILINE)
_ORDERED_RE = re.compile(r"^[ \t]{0,4}\d{1,3}[.)][ \t]+", re.MULTILINE)
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?/?>")

# Emphasis: the inner run may not start or end with whitespace, which is what
# keeps "EBITDA * 2" and "gross_margin_pct" intact — a lone delimiter with a
# space beside it is arithmetic or an identifier, not markup.
_STRONG_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_EMPHASIS_RE = re.compile(r"(?<![\w*_])([*_])(?=\S)([^*_\n]+?)(?<=\S)\1(?![\w*_])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)

# A table row rendered into a single-line field is unreadable either way; the
# pipes are dropped and the cells joined, which at least preserves the values.
_TABLE_DIVIDER_RE = re.compile(r"^[ \t]{0,3}\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$",
                               re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^[ \t]{0,3}\|(.+)\|[ \t]*$", re.MULTILINE)

_WS_RUN_RE = re.compile(r"[ \t]{2,}")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# C0 controls except tab/newline/carriage-return. A model echoing bytes out of
# an OCR'd PDF can emit these, and they survive JSON encoding to become
# invisible corruption in an export or a mojibake cell in an XLSX.
_CONTROL_CHARS = {c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)}
_CONTROL_CHARS[0x7F] = None


def strip_markdown(text):
    """Return ``text`` with markdown and HTML markup removed, content kept.

    Conservative by design. The failure mode that matters is not "an asterisk
    survived" — it is "a number was mangled", because a stripped figure is
    indistinguishable from a researched one and there is no second copy to
    check it against. So every rule requires a well-formed pair with non-space
    content inside it, and anything ambiguous is left exactly as written.

    A link becomes ``text (url)`` rather than bare ``text``: the URL is
    evidence, and the alternative discards the only citation the model
    provided. An image becomes its alt text — the binary is not reachable from
    here and a bare URL in a prose field reads as a broken citation.
    """
    if not isinstance(text, str) or not text:
        return "" if text is None else text

    out = text
    out = _FENCE_RE.sub(lambda m: m.group(1), out)
    out = _INLINE_CODE_RE.sub(lambda m: m.group(1), out)
    out = _IMAGE_RE.sub(lambda m: m.group(1), out)
    out = _LINK_RE.sub(_render_link, out)
    out = _AUTOLINK_RE.sub(lambda m: m.group(1), out)
    out = _HTML_TAG_RE.sub("", out)
    out = _TABLE_DIVIDER_RE.sub("", out)
    out = _TABLE_ROW_RE.sub(
        lambda m: " ".join(cell.strip() for cell in m.group(1).split("|")
                           if cell.strip()),
        out)
    out = _HEADING_RE.sub("", out)
    out = _SETEXT_RE.sub("", out)
    out = _BLOCKQUOTE_RE.sub("", out)
    out = _BULLET_RE.sub("", out)
    out = _ORDERED_RE.sub("", out)
    out = _STRONG_RE.sub(lambda m: m.group(2), out)
    out = _STRIKE_RE.sub(lambda m: m.group(1), out)
    # Emphasis twice: "*_nested_*" needs a second pass, and the pattern cannot
    # match across its own delimiters in one go.
    out = _EMPHASIS_RE.sub(lambda m: m.group(2), out)
    out = _EMPHASIS_RE.sub(lambda m: m.group(2), out)

    out = out.translate(_CONTROL_CHARS)
    out = _WS_RUN_RE.sub(" ", out)
    out = _BLANK_RUN_RE.sub("\n\n", out)
    return out.strip()


def _render_link(match):
    """``[label](url)`` → ``label (url)``, or just ``label`` when they agree."""
    label = (match.group(1) or "").strip()
    url = (match.group(2) or "").strip()
    if not url or url == label or url.startswith("#"):
        return label
    if not label:
        return url
    return f"{label} ({url})"


def plain_text(value, *, limit=MAX_STRING_CHARS, budget=None):
    """Coerce anything to a bounded, markup-free, JSON-safe string.

    Non-strings are stringified rather than rejected: a model that returns
    ``2024`` where a string was asked for has given the right answer in the
    wrong container, and refusing it loses a fact over a type.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # Before the numeric branch: bool is a subclass of int, and "True" is
        # never what a string field wanted.
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return _number_to_text(value)
    if isinstance(value, (list, tuple, set)):
        # A list where a string was asked for: join rather than drop. Losing
        # four of five values silently is worse than a slightly long string,
        # and `first_term` exists for the fields where only one is meaningful.
        parts = [plain_text(v, limit=limit) for v in list(value)[:MAX_LIST_ITEMS]]
        value = ", ".join(p for p in parts if p)
    elif isinstance(value, dict):
        # Objects reaching a string field are almost always {"value": …,
        # "source": …} wrappers. Prefer the obvious payload key over dumping
        # the whole mapping into prose.
        for key in ("value", "text", "name", "title", "description", "summary"):
            if key in value:
                return plain_text(value[key], limit=limit, budget=budget)
        value = ", ".join(f"{k}: {plain_text(v, limit=limit)}"
                          for k, v in list(value.items())[:MAX_DICT_KEYS])
    else:
        value = str(value)

    text = unicodedata.normalize("NFC", str(value))
    text = strip_markdown(text)
    if len(text) > limit:
        if budget is not None:
            budget.note()
        text = text[:limit].rstrip() + "…"
    return text


def _number_to_text(value):
    """Render a number for a string field without scientific notation.

    ``str(1e-7)`` is ``'1e-07'``, which is a correct float and a useless
    profile field. Also trims the trailing ``.0`` that makes an integer count
    read as a measurement.
    """
    try:
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return ""
            text = f"{value:.10f}".rstrip("0").rstrip(".")
            return text or "0"
        return str(value)
    except (ValueError, OverflowError):
        return ""


# --- numbers ----------------------------------------------------------------

_NUMBER_RE = re.compile(r"[-+]?\d[\d,_ ]*(?:\.\d+)?(?:[eE][-+]?\d+)?")

# Indian and international scale words. Present so a scaled figure can be
# RECOGNISED, never silently converted: "₹8.6 crore" in a field named
# `revenue_m` is a unit mismatch that a human has to adjudicate, and a
# sanitizer that quietly multiplied by 10,000,000 would replace a visible
# inconsistency with an invisible one.
SCALE_WORDS = {
    "thousand": 1e3, "k": 1e3,
    "lakh": 1e5, "lac": 1e5, "lakhs": 1e5, "lacs": 1e5,
    "million": 1e6, "mn": 1e6, "m": 1e6,
    "crore": 1e7, "cr": 1e7, "crores": 1e7,
    "billion": 1e9, "bn": 1e9, "b": 1e9,
    "trillion": 1e12, "tn": 1e12,
}
_SCALE_RE = re.compile(
    r"\b(" + "|".join(sorted(SCALE_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE)

# Currency marks that can prefix a figure. Used only for detection.
_CURRENCY_HINTS = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "": "",
    "$": "USD", "usd": "USD", "us$": "USD",
    "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
    "¥": "JPY", "jpy": "JPY", "sgd": "SGD", "aed": "AED",
}


def as_number(value, *, allow_negative=True):
    """Best-effort float, or ``None``. Never raises.

    Handles the shapes a model actually returns in a numeric field: a bare
    number, a formatted string (``"1,250"``, ``"45%"``, ``"₹8.6 crore"``,
    ``"USD 15.5M"``), an empty string, and the literal strings ``"null"`` /
    ``"n/a"`` / ``"not disclosed"``.

    Returns the number **as written**. Scale words are detected by
    :func:`describe_number` and reported, not applied — see :data:`SCALE_WORDS`.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or value in (
                float("inf"), float("-inf"))):
            # NaN and infinities are not storable in a DecimalField and are not
            # a measurement of anything.
            return None
        return None if (not allow_negative and value < 0) else float(value)
    if isinstance(value, Decimal):
        try:
            number = float(value)
        except (ValueError, OverflowError, InvalidOperation):
            return None
        return None if (not allow_negative and number < 0) else number
    if isinstance(value, (list, tuple)):
        # A single-element array around a number is a container slip; anything
        # longer is ambiguous and is refused rather than guessed at.
        return as_number(value[0], allow_negative=allow_negative) \
            if len(value) == 1 else None
    if isinstance(value, dict):
        for key in ("value", "amount", "number", "figure"):
            if key in value:
                return as_number(value[key], allow_negative=allow_negative)
        return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text or text.lower() in (
            "null", "none", "n/a", "na", "-", "--", "nil", "not disclosed",
            "not available", "not found", "undisclosed", "tbd", "unknown"):
        return None

    match = _NUMBER_RE.search(text)
    if not match:
        return None
    cleaned = match.group(0).replace(",", "").replace("_", "").replace(" ", "")
    try:
        number = float(cleaned)
    except (ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if not allow_negative and number < 0:
        return None
    # A parenthesised figure is accounting notation for a negative — "(6.32)"
    # is a loss, and reading it as +6.32 flips the sign on an EBITDA.
    if allow_negative and number > 0 and re.search(r"\(\s*[\d,.]+\s*\)", text):
        number = -number
    return number


def describe_number(value):
    """``(number, scale_word, currency)`` for a possibly-dressed figure.

    Lets a caller record *how* a figure was written without acting on it. The
    financial-summary rows that read ``revenue_m: 8.6`` while the observations
    said "₹8.6 Crore" are exactly the case this exists to make visible: the
    number is 8.6, the scale is "crore", the currency is INR, and none of that
    agreed with a field named ``_m``.
    """
    number = as_number(value)
    if not isinstance(value, str):
        return number, "", ""
    text = value.strip()
    scale_match = _SCALE_RE.search(text)
    scale = scale_match.group(1).lower() if scale_match else ""
    currency = ""
    lowered = text.lower()
    for hint, code in _CURRENCY_HINTS.items():
        if hint and hint in lowered:
            currency = code
            break
    return number, scale, currency


def as_int(value, *, minimum=None, maximum=None):
    """Best-effort integer, clamped, or ``None``. Never raises."""
    number = as_number(value)
    if number is None:
        return None
    try:
        result = int(round(number))
    except (ValueError, OverflowError):
        return None
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def as_bool(value, *, default=False):
    """Best-effort boolean. A blank or unrecognised value returns ``default``.

    Deliberately does not treat every truthy object as ``True``: the schema
    uses booleans to *qualify* a row (``is_founder``, ``is_estimate``), and a
    stray non-empty string coerced to ``True`` silently changes which table a
    person is written into.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float, Decimal)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "y", "1", "t"):
            return True
        if text in ("false", "no", "n", "0", "f", ""):
            return False
        return default
    return default


def as_str_list(value, *, limit=MAX_LIST_ITEMS, item_limit=2_000):
    """Coerce to a list of clean, de-duplicated, non-empty strings.

    A comma-separated string becomes a list, because a model asked for an array
    routinely returns one sentence — and dropping it would lose every investor
    name in a funding round.
    """
    if value is None or value == "":
        return []
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, str):
        # Only split on commas/semicolons/newlines. Splitting on " and " would
        # break "Johnson and Johnson".
        parts = re.split(r"[;\n]+|,(?![^(]*\))", value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]

    out, seen = [], set()
    for part in parts[:limit]:
        text = plain_text(part, limit=item_limit)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def first_term(value, *, separators=r"[,;/|]|\band\b|\&"):
    """The single most specific term from a value that may carry several.

    For scalar taxonomy fields — ``macro_sector``, ``sub_sector`` — where a
    downstream lookup matches the whole string. ``resolve_sub_sector`` misses a
    five-term concatenation at every stage including fuzzy, so the company gets
    no benchmark group and a fifth of the scorecard is dropped from the
    denominator with nothing on the profile saying so.

    Takes the FIRST term, which is where a model puts its primary
    classification, and the caller records the discarded remainder so the
    narrowing is visible rather than silent.
    """
    text = plain_text(value)
    if not text:
        return "", []
    parts = [p.strip(" .") for p in re.split(separators, text, flags=re.I)]
    parts = [p for p in parts if p]
    if not parts:
        return "", []
    return parts[0], parts[1:]


def match_enum(value, options, *, default=""):
    """Snap a value onto one of ``options``, or return ``default``.

    Case- and punctuation-insensitive, then a containment check both ways, so
    "VC-Funded" and "Venture Capital Funded" both land on "VC Funded". No fuzzy
    matching: these fields drive bucket selection and benchmark lookup, where a
    near-miss is worse than a blank.
    """
    text = plain_text(value)
    if not text or not options:
        return default

    def key(item):
        return re.sub(r"[^a-z0-9]+", "", str(item).lower())

    wanted = key(text)
    if not wanted:
        return default
    by_key = {key(o): o for o in options}
    if wanted in by_key:
        return by_key[wanted]
    for option_key, option in by_key.items():
        if option_key and (option_key in wanted or wanted in option_key):
            return option
    return default


# --- URLs -------------------------------------------------------------------

# Search-grounding redirectors. These are not article URLs: they are
# short-lived, provider-issued indirections that expire in roughly a month, so
# storing one as a news citation guarantees a dead link in every export that
# outlives it. Recorded here as hosts rather than a regex so a new provider is
# one line.
REDIRECT_HOSTS = (
    "vertexaisearch.cloud.google.com",
    "vertexaisearch.googleapis.com",
    "grounding-api-redirect",
    "www.google.com/url",
    "news.google.com/rss/articles",
    "r.jina.ai",
    "webcache.googleusercontent.com",
)

_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.|0\.|10\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1\]?)",
    re.IGNORECASE)


def is_redirect_url(url):
    """True when ``url`` is a provider redirector rather than a real source."""
    if not isinstance(url, str) or not url:
        return False
    lowered = url.lower()
    return any(host in lowered for host in REDIRECT_HOSTS)


def clean_url(value, *, allow_redirect=False):
    """``(url, problem)`` — a normalised absolute URL, or ``("", reason)``.

    Never raises and never guesses a scheme other than https. Rejects:

    * anything that is not http(s) — ``javascript:`` and ``data:`` in a field
      the frontend renders as an anchor is a stored-XSS vector, and ``file:``
      in an export is a local-disk reference;
    * private and loopback hosts, which in a server-side fetch are an SSRF
      target and in a client-side link are simply broken;
    * grounding redirectors, unless the caller explicitly wants them — see
      :data:`REDIRECT_HOSTS`.

    The reason is returned rather than logged and dropped, so a caller can put
    it in front of the user instead of silently emitting an empty field.
    """
    text = plain_text(value, limit=2_048)
    if not text:
        return "", ""
    # `plain_text` renders "label (url)" for a markdown link; if the field was
    # meant to be a URL, the parenthesised half is the part we want.
    embedded = re.search(r"\((https?://[^)\s]+)\)\s*$", text)
    if embedded:
        text = embedded.group(1)
    text = text.strip().strip("<>\"'")
    if " " in text:
        text = text.split()[0]
    if not text:
        return "", ""

    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return "", "not a parseable URL"
    if parsed.scheme not in ("http", "https"):
        return "", f"unsupported scheme '{parsed.scheme}'"
    host = (parsed.hostname or "").strip()
    if not host or "." not in host:
        return "", "no resolvable host"
    if _PRIVATE_HOST_RE.match(host):
        return "", "points at a private or loopback address"
    if not allow_redirect and is_redirect_url(candidate):
        return "", ("a search-grounding redirect, which expires — the "
                    "publisher's own URL is needed")
    return candidate[:1_024], ""


# --- spec-driven coercion ---------------------------------------------------

_ENUM_RE = re.compile(r"one of:\s*(.+)$", re.IGNORECASE | re.DOTALL)

# Scalar fields whose value feeds an exact-match lookup somewhere downstream,
# so a list in them is not merely untidy — it silently costs the lookup.
SINGLE_TERM_FIELDS = {"macro_sector", "sub_sector"}


def spec_kind(spec):
    """Classify a field spec string into a coercion kind.

    The spec text is the same string rendered into the prompt, so this reads
    the field's declared type from the one place it is written down. An
    unrecognised spec falls through to ``"text"``, which stores the value
    intact — the safe direction when a new admin-authored spec uses wording
    this does not know.
    """
    text = (spec or "").strip().lower()
    if not text:
        return "text"
    if text.startswith("boolean"):
        return "boolean"
    if text.startswith("number") or text.startswith("integer"):
        return "number"
    if text.startswith("array of strings") or text.startswith("list of strings"):
        return "string_list"
    if text.startswith("array of objects") or text.startswith("list of objects"):
        return "object_list"
    if text.startswith("object"):
        return "object"
    if text.startswith("array") or text.startswith("list"):
        return "list"
    return "text"


def spec_options(spec):
    """The enumerated values a spec declares after ``one of:``, else ``[]``."""
    match = _ENUM_RE.search(spec or "")
    if not match:
        return []
    tail = match.group(1)
    # The enumeration ends at the first sentence break; anything after is prose
    # about the field rather than another option.
    tail = re.split(r"\n\s*\n", tail)[0]
    return [part.strip(" .\n\t") for part in tail.split(",") if part.strip(" .\n\t")]


def coerce_to_spec(field_name, spec, value, *, budget=None, notes=None):
    """Coerce one field's value to the type its spec declares. Never raises.

    :param field_name: Used for the URL and single-term heuristics, both of
        which are about a field's downstream consumer rather than its type.
    :param spec: The field's specification string from the section schema.
    :param notes: Optional list; a human-readable line is appended whenever a
        value is changed in a way worth surfacing (a discarded term, a rejected
        link). Silence about a correction is how the sub-sector defect stayed
        invisible for a whole release.
    """
    kind = spec_kind(spec)
    lowered_name = (field_name or "").lower()
    lowered_spec = (spec or "").lower()

    if kind == "boolean":
        # An explicit null is "not stated", which is NOT the same as false, and
        # whose correct default belongs to the consumer rather than here.
        # `_replace_people` reads a missing `is_founder` as True — for a company
        # profile the founders are the population that matters — so returning
        # False for a null would file every unclassified person as a key person
        # and empty the section a reader checks for the team. Passing the
        # absence through keeps that decision where it is documented.
        return None if value is None else as_bool(value)

    if kind == "number":
        number, scale, currency = describe_number(value)
        if number is not None and scale and notes is not None:
            notes.append(
                f"{field_name}: the source wrote this as "
                f"'{plain_text(value, limit=80)}'; stored as {number} without "
                f"applying the '{scale}' multiplier, because the field's unit "
                f"is fixed by the schema and converting silently would hide "
                f"the mismatch"
                + (f" (currency read as {currency})" if currency else ""))
        return number

    if kind == "string_list":
        return as_str_list(value)

    if kind in ("object_list", "list"):
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        return [sanitize_json(item, budget=budget)
                for item in list(value)[:MAX_LIST_ITEMS]]

    if kind == "object":
        if not isinstance(value, dict):
            return {}
        return sanitize_json(value, budget=budget)

    # --- text -------------------------------------------------------------
    options = spec_options(spec)
    if options:
        matched = match_enum(value, options)
        if not matched and plain_text(value) and notes is not None:
            notes.append(
                f"{field_name}: '{plain_text(value, limit=80)}' is not one of "
                f"the {len(options)} permitted values and was cleared")
        return matched

    if "url" in lowered_spec or lowered_name in (
            "website", "link", "url", "linkedin_url", "logo_url"):
        url, problem = clean_url(value)
        if problem and notes is not None:
            notes.append(f"{field_name}: link dropped — {problem} "
                         f"({plain_text(value, limit=120)})")
        return url

    text = plain_text(value, budget=budget)
    if lowered_name in SINGLE_TERM_FIELDS and text:
        primary, rest = first_term(text)
        if rest:
            if notes is not None:
                notes.append(
                    f"{field_name}: narrowed to '{primary}' for benchmark "
                    f"matching; also reported as {', '.join(rest)}")
            return primary
    return text


def sanitize_json(value, *, budget=None, depth=0):
    """Recursively make an arbitrary decoded-JSON value safe to store.

    Type-agnostic: strips markup from every string, drops values JSON cannot
    represent, and enforces the depth, width and total-node bounds. This is the
    floor every model response gets even where no field spec applies —
    :func:`coerce_to_spec` layers declared types on top of it.

    Truncation is recorded on the budget rather than raised, because a response
    that trips a bound is still mostly good data and the useful outcome is to
    keep the part that fits and say so.
    """
    budget = budget if budget is not None else _Budget()
    if depth > MAX_DEPTH or not budget.take():
        budget.note()
        return None

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        number = as_number(value)
        return number
    if isinstance(value, str):
        return plain_text(value, budget=budget)
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if len(items) > MAX_LIST_ITEMS:
            budget.note()
            items = items[:MAX_LIST_ITEMS]
        return [sanitize_json(item, budget=budget, depth=depth + 1)
                for item in items]
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_DICT_KEYS:
                budget.note()
                break
            # A non-string key is legal in Python and not in JSON; coercing it
            # keeps the object storable in a JSONField instead of failing at
            # serialisation time, several layers away from the cause.
            clean_key = plain_text(key, limit=256)
            if not clean_key:
                continue
            out[clean_key] = sanitize_json(item, budget=budget, depth=depth + 1)
        return out

    # Anything else (a date, a model instance, a set of bytes) is stringified
    # rather than dropped: it came from a JSON decode, so this is nearly
    # unreachable, and losing the value would be worse than storing its text.
    return plain_text(value, budget=budget)
