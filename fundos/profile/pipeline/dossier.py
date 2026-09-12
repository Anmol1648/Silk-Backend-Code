"""Owns ``consolidated.md`` — the single markdown artifact every run produces.

Architectural intent
--------------------
``consolidated.md`` is the *evidence file*: everything the pipeline gathered, in
the words of the source that gathered it, before any synthesis happens. It is
both a deliverable in its own right (a founder or an analyst can read it) and
the sole input to the synthesis call, so its structure has to be stable and its
provenance unambiguous.

It is also what the profile has never had. Until now a source bundle was
compressed, serialised into a prompt and discarded, so "why does the profile
say this?" had no answer beyond re-running the job and hoping for the same
output. The dossier is that answer, kept.

Why a writer object instead of appends
--------------------------------------
The sources run **concurrently**, so nothing may append to the artifact
directly: interleaved appends would scramble the markdown, and section order
would depend on which source finished first — making the artifact
non-deterministic for the same inputs.

Instead each source hands its markdown to :meth:`DossierWriter.set_section`,
which rebuilds the whole document from the accumulated sections in a **fixed**
order, under a lock. That buys:

* **Deterministic section order** regardless of completion order.
* **No interleaving and no torn reads** — a reader always sees a complete
  document, never a half-written one.
* **Incremental durability.** Source 1 calls ``set_section`` after every
  research batch, so the persisted dossier reflects the latest completed work.
  If the worker dies at batch 7 of 10, seven finished batches are already
  saved.

Performance trade-off
---------------------
Rebuilding the whole document on every ``set_section`` is O(total size) per
call rather than O(delta). Deliberate and comfortably affordable: a dossier is
tens to low hundreds of KB, ``set_section`` is called on the order of a dozen
times per run, and the writes are dwarfed by the LLM latency they interleave
with. Correctness is worth far more here than the saved bytes.

The object-storage flush is the one place that reasoning needs qualifying: a
remote PUT per batch is a real network cost, so persistence is best-effort and
never blocks or fails a batch (see :meth:`_flush`). The authoritative write is
:meth:`finalize`.
"""
import re
import logging
import threading

from django.utils import timezone

logger = logging.getLogger("fundos.profile")

# Fixed render order, independent of which source finishes first.
#
# The keys are the contract between the sources and this module: a source
# writes under its key, and `set_section` rejects anything not listed here so a
# typo fails loudly at the call site instead of silently producing a dossier
# missing a whole source.
# DOCUMENTS RENDER FIRST, whatever their number.
#
# The dossier is capped before synthesis (`profile_max_dossier_chars`) and the
# cut takes the TAIL. With web research first, a 552,000-character financial
# model sat at the end and lost 174,000 characters of itself to a cap the web
# research never came near — the company's own numbers cut so a search result
# could be kept whole. That is backwards: an uploaded document outranks the
# web everywhere else in this system, and it has to outrank it here too,
# where the loss actually happens.
#
# The keys and the "Source N" labels are UNCHANGED. They are identifiers, not
# positions: `source_labels` keys on "# Source 2" to find the document block,
# citations name files and batches rather than these headings, and citations
# stored on existing profiles still resolve. Renumbering to match the new
# order would break all three for a cosmetic gain.
SECTION_ORDER = (
    ("source2", "Source 2: Company Documents "
                "(native extraction + document model)"),
    ("source1", "Source 1: Web Research (search-grounded)"),
)


def _render_founders(founders):
    """Render the supplied founders as *research leads*, labelled unverified.

    Founders come from the company's own records — typed by a founder during
    onboarding, usually a name and a LinkedIn URL and nothing else. Nothing in
    this pipeline verifies them. They are still worth carrying into the dossier
    because they are often the only ground truth about *who* runs the company,
    and the research reads measurably better anchored when the names are
    present.

    What changed, and why
    ---------------------
    This block used to end "do not infer anything beyond what is written
    here", which the model obeyed. Combined with the write path skipping any
    row whose name a human had already claimed, the result on a live profile
    was a Founders section where the two actual principals had a name and a
    LinkedIn URL and **empty** ``role`` and ``background``, while a part-time
    co-founder — whose name the founder had *not* typed in, so nothing
    suppressed him — carried a full researched paragraph.

    A name a founder typed into an onboarding form is a starting point, not a
    finished record. So the instruction is now the opposite: research these
    people as thoroughly as anyone else, and return them fully populated. The
    provenance warning stays, because the distinction that matters is
    unchanged — a typed-in name is not evidence of anything except that
    somebody typed it — but it now constrains *trust*, not *effort*.

    :returns: A markdown fragment, or ``""`` when there are no founders — the
        caller omits the block entirely rather than emitting an empty heading
        the model might read as "this company has no founders".
    """
    if not founders:
        return ""

    lines = [
        "## Founders named by the company (user-provided, unverified — "
        "RESEARCH LEADS)",
        "",
        "_Taken from the company's own records: someone typed these into an "
        "onboarding form. They are **not** researched or verified, and they "
        "are **not** a complete list — treat them as leads, not as findings._",
        "",
        "_What to do with them:_",
        "",
        "1. _Research each person named below as thoroughly as anyone you "
        "discover yourself, and return them in the Founders section **fully "
        "populated** — role, background, education, prior companies. A name "
        "supplied here with no detail is a gap to close, not a record to "
        "preserve as-is._",
        "2. _Correct them where the sources disagree. A supplied title or "
        "LinkedIn URL that the evidence contradicts should be reported as the "
        "evidence has it._",
        "3. _Include everyone the sources show running the company, whether or "
        "not they appear below._",
        "4. _Do not treat anything in this list as evidence on its own. It "
        "confirms only that a person of this name was named._",
        "",
    ]
    for founder in founders:
        name = (founder.get("name") or "").strip() or "_unnamed_"
        role = (founder.get("designation") or founder.get("role") or "").strip()
        url = (founder.get("linkedinUrl") or founder.get("linkedin_url")
               or "").strip()
        bits = [b for b in (role, url) if b]
        detail = f" — {' — '.join(bits)}" if bits else ""
        lines.append(f"- **{name}**{detail}")
    return "\n".join(lines) + "\n"


class DossierWriter:
    """Accumulates the source sections and persists ``consolidated.md``.

    Thread safety: every mutating method takes ``self._lock``, so one instance
    is safe to share across the concurrently-running sources. It is not safe to
    share across processes — one run, one worker, one writer.

    Usage::

        writer = DossierWriter(run, "Acme", "https://acme.com", founders)
        writer.set_section("source1", "# Source 1…\\n")
        writer.set_section("source2", "# Source 2…\\n")
        summary = writer.finalize()
    """

    def __init__(self, run, company_name, website, founders=None):
        """:param run: The ProfileGenerationRun this dossier belongs to; its id
            fixes the storage path, so re-running a profile never overwrites an
            earlier run's evidence.
        :param company_name: Shown in the header and used by synthesis.
        :param website: Shown in the header.
        :param founders: Records to render in the header block. Copied
            defensively so a later mutation by the caller cannot change an
            already-written header.
        """
        self.run = run
        self.company_name = company_name
        self.website = website
        self.founders = list(founders or [])
        self.remote_path = (f"profiles/{run.profile_id}/runs/{run.id}/"
                            "consolidated.md")
        self._sections = {}
        self._lock = threading.Lock()
        self._text = ""

    # -- rendering ---------------------------------------------------------
    def _render(self):
        """Build the full markdown from current section state.

        Must be called with ``self._lock`` held — it reads ``self._sections``,
        which the concurrent sources mutate.

        A section that has not reported yet renders as an explicit placeholder
        rather than being omitted, so a reader (human or model) can tell "this
        source found nothing" from "this source has not run". Those are very
        different statements about a company, and conflating them is how a
        thin dossier gets mistaken for a thin company.
        """
        header = (
            f"# Consolidated research dossier: {self.company_name}\n\n"
            f"- Company: **{self.company_name}**\n"
            f"- Website: {self.website or '_not recorded_'}\n"
            f"- Last updated: {timezone.now().isoformat()}\n"
            f"- Order: the company's own documents come first because they "
            f"outrank web research. Where the two disagree, the document is "
            f"what the company itself reported.\n"
        )
        founders_block = _render_founders(self.founders)
        if founders_block:
            header = f"{header}\n{founders_block}"

        parts = [header]
        for key, title in SECTION_ORDER:
            content = (self._sections.get(key) or "").strip()
            parts.append(content if content else
                         f"# {title}\n\n_This source has not reported yet._")
        return "\n\n---\n\n".join(parts) + "\n"

    # -- writing -----------------------------------------------------------
    def set_section(self, key, markdown):
        """Record a source's markdown and re-persist the dossier.

        Safe to call concurrently and repeatedly; the last call for a key wins,
        which is what Source 1's per-batch updates rely on — each batch re-sends
        the *whole* section rather than a delta, precisely so a retried or
        out-of-order batch cannot corrupt the accumulated text.

        :raises ValueError: If ``key`` is not a known section. Fail-fast by
            design: a silent no-op would produce a dossier quietly missing a
            source, and the resulting profile would look merely thin rather
            than broken.
        """
        if key not in dict(SECTION_ORDER):
            raise ValueError(f"Unknown dossier section: {key!r}")
        with self._lock:
            self._sections[key] = markdown
            self._text = self._render()
            self._flush(best_effort=True)

    def _flush(self, *, best_effort):
        """Persist the current text to object storage.

        Called with the lock held. Best-effort during the run: an intermediate
        snapshot failing to upload is a lost convenience, not a lost result —
        the text is still in memory and :meth:`finalize` will write it again.
        Letting a transient storage blip kill a batch that has already paid for
        its LLM call would be strictly worse.
        """
        from fundos.docs.storage import get_storage

        try:
            return get_storage().upload_bytes(self._text.encode("utf-8"),
                                              self.remote_path)
        except Exception as exc:
            if not best_effort:
                raise
            logger.debug("dossier snapshot upload failed (will retry at "
                         "finalize): %s", exc)
            return False

    def finalize(self):
        """Write the final dossier and report what it contains.

        Called once by the orchestrator after every source has finished and
        before synthesis. Not redundant with the last ``set_section``: it
        refreshes the "Last updated" stamp and guarantees the stored object
        matches the exact text whose size is reported here.

        :returns: A progress record for the run row — the storage path,
            character count (a tiny dossier means the sources found nothing,
            which is what explains a thin profile downstream), and which
            sources actually reported.
        """
        with self._lock:
            self._text = self._render()
            stored = self._flush(best_effort=True)
            present = [key for key, _ in SECTION_ORDER
                       if (self._sections.get(key) or "").strip()]
            return {
                "storage_uri": self.remote_path if stored else "",
                "characters": len(self._text),
                "sections_present": present,
                "persisted": bool(stored),
            }

    @property
    def text(self):
        """The current dossier text.

        Read from memory, not storage: synthesis must run against exactly what
        this writer assembled, even in the case where the upload failed and the
        stored copy is stale or absent. A run that cannot save its evidence
        should still produce a profile, and say so.
        """
        with self._lock:
            return self._text or self._render()


# --- what a citation is allowed to name -------------------------------------
#
# The dossier's own headings, harvested back out of it. A citation that names
# one of these points at something a reader can turn to; a citation that names
# anything else points at nothing.
#
# Documents come FIRST and are labelled as such, because the ordering is the
# instruction: the company's own deck and financial model are what a reader
# trusts, and a search result repeating the same fact is weaker evidence for
# exactly the same claim. Told only to "name a heading", the model wrote the
# channel from memory instead -- 64 citations reading "Web Research
# (search-grounded)" for a company whose investor deck was in the dossier, one
# of them spelled "search-groundd".

#: `### <filename>` inside Source 2 -- one per uploaded file. The trailing
#: extension is required: a converted deck contributes its own `###` headings
#: to the same section, and a slide title harvested as a source would put a
#: label in front of the model that names no file anyone can open.
_DOCUMENT_HEADING = re.compile(
    r"^###\s+(?P<name>\S.*?\.[A-Za-z0-9]{2,5})\s*$")

#: `## Batch 7: Recent News & Developments` -- one per research batch.
_BATCH_HEADING = re.compile(r"^##\s+(?P<name>Batch\s+\d+:\s*\S.*?)\s*$")

#: What a citation says when the company itself supplied the value.
#:
#: The founders block is in the dossier but was never citable, so a founder
#: whose name came off the onboarding form had NO valid source string. The
#: model's three options were all bad -- cite a research batch it did not read
#: it from, cite the deck, or omit the citation -- and two of Tyreplex's five
#: founders came back permanently unattributable.
#:
#: It is a short fixed string rather than the block's own heading because the
#: model has to copy it character for character, and
#: "Founders named by the company (user-provided, unverified - RESEARCH
#: LEADS)" is not a string anything copies reliably.
FOUNDER_SOURCE_LABEL = "Provided by user"

#: The block that label stands for.
_FOUNDER_HEADING = re.compile(r"^##\s+Founders named by the company",
                              re.IGNORECASE)

#: Headings that are structure, not sources.
_NOT_A_SOURCE = ("extracted content", "model-read content", "grounding sources",
                 "consolidated research dossier")


def source_labels(dossier):
    """Every source a citation may name, uploaded documents first.

    :returns: ``(labels, document_count)`` -- the ordered list to put in front
        of the model, and how many of them are uploaded files, so a caller can
        say plainly whether documents were available to cite at all.
    """
    documents, batches = [], []
    founder_block = False
    in_documents = False

    for line in (dossier or "").split("\n"):
        if _FOUNDER_HEADING.match(line):
            founder_block = True
        if line.startswith("# Source 2"):
            in_documents = True
            continue
        if line.startswith("# Source ") or line.startswith("# Consolidated"):
            in_documents = False
            continue

        batch = _BATCH_HEADING.match(line)
        if batch:
            name = batch.group("name").strip()
            if name not in batches:
                batches.append(name)
            continue

        if not in_documents:
            continue
        heading = _DOCUMENT_HEADING.match(line)
        if not heading:
            continue
        name = heading.group("name").strip()
        if name.casefold() in _NOT_A_SOURCE or name.startswith("#"):
            continue
        # A research question is also a `###`, but never inside Source 2.
        if name not in documents:
            documents.append(name)

    # Documents stay FIRST so `labels[:document_count]` remains exactly the
    # uploaded files for every caller that slices it. The founder label goes
    # last in the list and first in the prompt, which is where precedence is
    # actually expressed.
    labels = documents + batches
    if founder_block:
        labels.append(FOUNDER_SOURCE_LABEL)
    return labels, len(documents)
