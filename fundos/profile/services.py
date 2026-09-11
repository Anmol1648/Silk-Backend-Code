"""
Company Profile services.

Design points that matter:

* Generation is ONE orchestrated job with visible sub-progress, and it
  completes even when individual sources fail (PRD §5.3 / C4). Partial
  failure is the expected case — LinkedIn may be absent, the market feed
  may not be connected — so a failed source marks its section as needing
  input rather than failing the run.

* Every source is primary (C4): website, uploaded documents, public web
  research and founder input all feed the same synthesis. There is no
  single point of failure.

* Numbers are never taken from the language model. Cash, runway and the
  fund-raise bucket are computed here or in the engines.
"""
import contextlib
import logging
import re
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Profile lifecycle
# --------------------------------------------------------------------------
def get_or_create_profile(company, user=None):
    from fundos.platformcfg.services import home_currency_for_country
    from fundos.profile.models import CompanyProfile

    profile = CompanyProfile.objects.filter(company=company).first()
    if profile:
        return profile
    return CompanyProfile.objects.create(
        tenant_id=company.tenant_id,
        company=company,
        website_url=company.domain or "",
        hq_country=(company.hq_country or "")[:2].upper(),
        home_currency=home_currency_for_country(company.hq_country),
        created_by=user,
    )


def ensure_sections(profile):
    """Materialise a row for every configured section."""
    from fundos.platformcfg.services import profile_sections
    from fundos.profile.models import ProfileSection

    existing = set(ProfileSection.objects.filter(profile=profile, is_active=True)
                   .values_list("section_key", flat=True))
    created = []
    for cfg in profile_sections():
        if cfg.section_key in existing:
            continue
        # QA issue 3: previously EVERY new section was created with
        # needs_input=True, so optional sections (and the structured ones
        # backed by their own tables) showed the orange dot forever. A
        # section needs input at creation only if it is required for
        # completeness; structured sections are then reconciled against
        # their real data by recompute_structured_sections below.
        needs = bool(getattr(cfg, "required_for_completeness", False))
        created.append(ProfileSection(
            tenant_id=profile.tenant_id, profile=profile,
            section_key=cfg.section_key, content="", needs_input=needs))
    if created:
        ProfileSection.objects.bulk_create(created)
    # Founders / Document Center reflect their backing tables, not a flag.
    recompute_structured_sections(profile)
    return created


def update_section(profile, section_key, *, content=None, structured=None,
                   user=None, change_summary="", by_ai=False,
                   ai_model_version="", source="", confidence=None,
                   needs_input=None, missing=None):
    """Update a section, snapshotting the previous version first.

    A human edit sets edited_by_user, which the regenerate path checks so
    AI output never silently discards someone's work.
    """
    from fundos.profile.models import ProfileSection, ProfileSectionVersion

    section = ProfileSection.objects.filter(
        profile=profile, section_key=section_key, is_active=True).first()
    if section is None:
        section = ProfileSection.objects.create(
            tenant_id=profile.tenant_id, profile=profile,
            section_key=section_key)

    # Snapshot current state before overwriting.
    if section.content or section.structured:
        ProfileSectionVersion.objects.create(
            tenant_id=profile.tenant_id, section=section,
            version_no=section.version_no, content=section.content,
            structured=section.structured or {},
            change_summary=change_summary or ("AI regeneration" if by_ai
                                              else "Edit"),
            changed_by=user, ai_model_version=section.ai_model_version or "")

    if content is not None:
        section.content = content
    if structured is not None:
        section.structured = structured
    if needs_input is not None:
        section.needs_input = needs_input
    if missing is not None:
        section.missing_notes = missing
    if source:
        section.source = source
    if confidence is not None:
        section.confidence = confidence
    if by_ai:
        section.ai_model_version = ai_model_version or section.ai_model_version
    else:
        section.edited_by_user = True
        section.verified = True
        section.verified_by = user
        section.verified_at = timezone.now()

    section.version_no += 1
    section.change_summary = change_summary
    section.save()
    recompute_completeness(profile)
    return section


def recompute_structured_sections(profile):
    """Clear needs_input on structured sections once their real data exists.

    QA issue 3: Founders and the Document Center are backed by their own
    tables (Founder / ProfileDocument), not by section `content`. Their
    ProfileSection rows are created with needs_input=True and nothing ever
    cleared it, so the orange "Needs your input" dot stayed on even after a
    founder was added or a document uploaded. This recomputes those flags
    from the backing data; call it after any founder or document write.

    Enhancement set (25-Jul): the same treatment now extends to every
    record-backed list section — Key People, Competitors, Funding History,
    Recent News — and to the numeric structured forms (Revenue Model,
    Company Metrics, Financial Summary), whose data lives in
    ProfileSection.structured. A record-backed section needs input only
    while it holds no records; a structured form needs input while its
    `items` list is empty. Optional sections still never block completeness
    — needs_input drives the advisory dot, not the gate.
    """
    from fundos.profile.models import (
        Competitor, Founder, FundingRound, KeyPerson, NewsItem,
        ProfileDocument, ProfileSection,
    )

    def _set(section_key, still_needs):
        ProfileSection.objects.filter(
            profile=profile, section_key=section_key, is_active=True
        ).update(needs_input=still_needs)

    # Table-backed record sections: "has any row?" is the whole test.
    record_backed = {
        "founders": Founder,
        "key_people": KeyPerson,
        "competitors": Competitor,
        "funding_history": FundingRound,
        "recent_news": NewsItem,
    }
    for section_key, Model in record_backed.items():
        has_any = Model.objects.filter(profile=profile).exists()
        _set(section_key, not has_any)

    has_document = ProfileDocument.objects.filter(profile=profile).exists()
    _set("document_center", not has_document)

    # Structured JSON forms: populated == a non-empty items list.
    from fundos.profile.records import STRUCTURED_FORMS
    for section_key in STRUCTURED_FORMS:
        section = ProfileSection.objects.filter(
            profile=profile, section_key=section_key, is_active=True).first()
        if section is None:
            continue
        structured = section.structured or {}
        if section_key == "financial_summary":
            # Stored as {financials, observations}; populated == either present.
            populated = bool(structured.get("financials")
                             or structured.get("observations")
                             or structured.get("items"))
        else:
            populated = bool(structured.get("items"))
        section.needs_input = not populated
        section.save(update_fields=["needs_input", "updated_at"])


def recompute_completeness(profile):
    """Percentage of required sections that are actually complete (C2).

    Previously this counted only sections whose narrative `content` was
    non-empty. Record-backed and structured sections (founders, competitors,
    financial_summary, revenue_model, …) keep their data in tables or in
    `structured`, never in `content`, so filling them in never moved the
    score — it stayed frozen (backend-issues combined report #4). We now
    measure completeness with the SAME per-section isComplete logic the API
    serializer uses, so the gauge reflects real populated data across every
    section type and recalculates on every write.
    """
    from fundos.platformcfg.services import completeness_section_keys
    from fundos.profile import spec_serializer

    # Keep the structured-section flags honest before measuring.
    recompute_structured_sections(profile)

    required = completeness_section_keys()
    if not required:
        profile.completeness_pct = Decimal("100")
        profile.save(update_fields=["completeness_pct", "updated_at"])
        return 100.0

    sections = spec_serializer.build_sections(profile)
    # Map internal required keys to the spec keys the serializer emits.
    filled = 0
    for internal_key in required:
        spec_key = spec_serializer.INTERNAL_TO_SPEC_KEY.get(
            internal_key, internal_key)
        wrapper = sections.get(spec_key)
        if wrapper and wrapper.get("isComplete"):
            filled += 1

    pct = round(100.0 * filled / len(required), 2)
    profile.completeness_pct = Decimal(str(pct))
    profile.save(update_fields=["completeness_pct", "updated_at"])
    return pct


def confirm_review(profile, user):
    """C2 — the owner's explicit confirmation that the profile is reviewed."""
    profile.reviewed_at = timezone.now()
    profile.reviewed_by = user
    profile.status = "reviewed"
    profile.save(update_fields=["reviewed_at", "reviewed_by", "status",
                                "updated_at"])
    return profile


# --------------------------------------------------------------------------
# Generation orchestration (PRD §5.3)
# --------------------------------------------------------------------------
def _context_deal(profile, user=None):
    """Resolve the deal that deal-scoped research machinery hangs off.

    Profile generation is COMPANY-scoped, but the research adapters and the
    materials store are deal-scoped. Previously every source here was gated on
    `Deal.objects.filter(...).first()` returning something, and at Create
    Company time no deal exists yet — so website, documents and research were
    all skipped and the model received nothing but the company name, domain
    and country. Creating the deal on demand (as the document upload path
    already did) is what makes the sources reachable at all.
    """
    from fundos.core.models import Deal

    deal = (Deal.objects.filter(company=profile.company, is_deleted=False,
                                status="active")
            .order_by("created_at").first())
    if deal:
        return deal
    return Deal.objects.create(
        tenant_id=profile.company.tenant_id, company=profile.company,
        name=f"{profile.company.name} — Fundraise", status="active",
        primary_owner=user, created_by=user)


def collect_sources(profile, user=None):
    """Gather every available source. Each failure is isolated (C4).

    Returns (payloads, failures) — failures never abort the run.
    """
    from fundos.profile.trace import (trace_event, payload_size, current_trace,
                                       OK, FAIL, SKIP)

    payloads = {}
    failures = []
    tr = current_trace()

    deal = None
    try:
        deal = _context_deal(profile, user=user)
        trace_event("sources.deal", OK, dealId=str(deal.id) if deal else None)
    except Exception as e:
        logger.warning("PROFILE: could not resolve a context deal: %s", e)
        failures.append({"source": "deal", "error": str(e)})
        trace_event("sources.deal", FAIL, error=str(e))

    # 1. Website
    website_url = profile.website_url or getattr(profile.company, "domain", "")
    if not website_url:
        trace_event("sources.website", SKIP,
                    reason="no website address recorded on the company")
    if website_url:
        try:
            from fundos.research.adapters.website import WebsiteAdapter
            if deal:
                # The adapter parameter is `ckb_snapshot`. This was called as
                # `ckb=` — a TypeError raised on EVERY run, caught below and
                # logged at WARNING, so website research silently never
                # happened even when everything else was configured.
                import time as _t
                _s = _t.monotonic()
                adapter = WebsiteAdapter(deal=deal, ckb_snapshot={})
                payloads["website"] = adapter.fetch()
                trace_event("sources.website", OK,
                            ms=int((_t.monotonic() - _s) * 1000),
                            url=website_url,
                            chars=payload_size(payloads.get("website")))
            else:
                trace_event("sources.website", SKIP,
                            reason="no deal context could be resolved")
        except Exception as e:
            logger.warning("PROFILE: website source failed: %s", e)
            failures.append({"source": "website", "error": str(e)})
            trace_event("sources.website", FAIL, url=website_url,
                        error=f"{type(e).__name__}: {e}")

    # 2. Uploaded documents
    try:
        payloads["documents"] = _document_extracts(profile)
        readiness = document_readiness(profile)
        # NOT A FAILURE — this step runs BEFORE the pipeline reads anything.
        #
        # It used to report FAIL whenever documents were uploaded and nothing
        # was analysed yet, which is the normal opening state of every first
        # generation. That put a permanent +1 on the run's failure count
        # (`complete: steps=62 failures=3`) on runs that went on to read every
        # file perfectly, and made a healthy run indistinguishable from a
        # broken one at a glance.
        #
        # SKIP says what is true: these documents have contributed nothing
        # YET. Whether the pipeline then reads them is reported by the
        # pipeline's own per-file events, which is the only place that can
        # actually know.
        if readiness["uploaded"] and not readiness["analysed"]:
            status, note = SKIP, ("not read at this point; the pipeline reads "
                                  "them inline later in this run")
        else:
            status, note = OK, ""
        trace_event(
            "sources.documents", status,
            uploaded=readiness["uploaded"], analysed=readiness["analysed"],
            pending=readiness["pending"],
            chars=payload_size(payloads.get("documents")),
            note=note)
    except Exception as e:
        logger.warning("PROFILE: document extraction failed: %s", e)
        failures.append({"source": "documents", "error": str(e)})
        trace_event("sources.documents", FAIL, error=f"{type(e).__name__}: {e}")

    # 3. Founder-supplied LinkedIn (C3 — public profile only, optional)
    try:
        payloads["founders"] = _founder_inputs(profile)
        trace_event("sources.founders", OK,
                    count=len(payloads.get("founders") or []))
    except Exception as e:
        failures.append({"source": "founders", "error": str(e)})
        trace_event("sources.founders", FAIL, error=str(e))

    # 4. Public research adapters — market/competitor/news/funding
    try:
        payloads["research"] = _research_payloads(profile, deal=deal)
    except Exception as e:
        logger.warning("PROFILE: research sources failed: %s", e)
        failures.append({"source": "research", "error": str(e)})
        trace_event("sources.research", FAIL, error=f"{type(e).__name__}: {e}")

    # A research adapter that failed stores its error in the payload. Those
    # error strings are not context — sending them to the model wastes tokens
    # and invites it to treat the failure text as evidence. Strip them out and
    # surface them as failures so they show up in run diagnostics instead.
    research = payloads.get("research") or {}
    if isinstance(research, dict):
        clean = {}
        for source, value in research.items():
            if isinstance(value, dict) and value.get("error"):
                failures.append({"source": f"research.{source}",
                                 "error": value["error"]})
                continue
            clean[source] = value
        payloads["research"] = clean

    # Cost control: compress ONCE here rather than letting the adapter blind-
    # truncate at 24k chars per call. A blind tail-slice is the worst of both
    # worlds — you pay for boilerplate and lose the end of the document.
    payloads = compress_payloads(payloads)

    trace_event("sources.summary", OK,
                website_chars=payload_size(payloads.get("website")),
                documents_chars=payload_size(payloads.get("documents")),
                research_chars=payload_size(payloads.get("research")),
                founders=len(payloads.get("founders") or []),
                total_chars=payload_size(payloads),
                failures=len(failures))
    return payloads, failures


# --------------------------------------------------------------------------
# Source compression + fingerprinting (cost control)
#
# The same payload bundle used to be serialised into every one of the ~15
# section calls. Two changes cut that bill: shrink the bundle before it is
# ever sent, and skip regeneration entirely when the sources have not moved
# since the last run.
# --------------------------------------------------------------------------

# Boilerplate that appears on nearly every marketing site and carries no
# signal for an investment dossier.
_BOILERPLATE_MARKERS = (
    "cookie", "privacy policy", "terms of service", "all rights reserved",
    "subscribe to our newsletter", "accept all", "skip to content",
)

MAX_WEBSITE_CHARS = 6000
MAX_DOC_SUMMARY_CHARS = 1200
MAX_DOCUMENTS = 12
MAX_RESEARCH_CHARS = 6000


def _clean_text(value, limit, *, _losses=None, _key=""):
    """Strip boilerplate lines and collapse whitespace, then cap.

    v27.4 — the cap now REPORTS itself.

    This is the FIRST and largest truncation in the pipeline and it was
    completely silent. v27.3 raised the adapter's ceiling from 24,000 to
    120,000 characters and added a warning there, which was correct but fixed
    the second cut, not the first: `MAX_WEBSITE_CHARS = 6000` is applied per
    page here, before the adapter ever sees the payload. MediBuddy's
    `website_chars=31001` was measured AFTER this ran, so the 22% loss
    quantified in the v27.3 changelog was what survived this function.

    De-boilerplating is signal-preserving and needs no report. The tail-slice
    is not: it removes whatever sits at the end of a page, and on a scraped
    site that is disproportionately the About / Team / Investors content that
    carries the highest diligence value. `_losses` collects the numbers so the
    caller can emit one aggregate trace line per run rather than one per key.
    """
    if not isinstance(value, str):
        return value
    lines = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(marker in lowered for marker in _BOILERPLATE_MARKERS):
            continue
        lines.append(stripped)
    cleaned = "\n".join(lines)
    if len(cleaned) <= limit:
        return cleaned
    dropped = len(cleaned) - limit
    if _losses is not None:
        _losses.append({"key": _key or "?", "chars_in": len(cleaned),
                        "chars_kept": limit, "chars_dropped": dropped})
    # The marker costs ~90 characters and buys the model the knowledge that it
    # is reasoning over a fragment. Without it a truncated page is
    # indistinguishable from a short one, and "not stated on the website" is
    # the wrong conclusion to let it draw silently.
    return cleaned[:limit] + (
        f"\n[... {dropped} characters truncated — this page is incomplete]")


def _prune_empty(obj):
    """Drop empty values — empty keys still cost tokens to serialise."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v = _prune_empty(v)
            if v not in (None, "", [], {}):
                out[k] = v
        return out
    if isinstance(obj, list):
        return [x for x in (_prune_empty(i) for i in obj)
                if x not in (None, "", [], {})]
    return obj


def compress_payloads(payloads):
    """Shrink the source bundle without losing signal.

    Website text is de-boilerplated, document summaries are capped and
    de-duplicated (the same deck summarised twice is pure repeated cost),
    and every empty key is dropped.
    """
    payloads = dict(payloads or {})
    # v27.4 — every tail-slice below records what it removed, and one trace
    # line reports the total. See _clean_text for why this was the truncation
    # that mattered most and the one nothing logged.
    losses = []
    dropped_docs = 0

    website = payloads.get("website")
    if isinstance(website, dict):
        payloads["website"] = {
            k: (_clean_text(v, MAX_WEBSITE_CHARS, _losses=losses,
                            _key=f"website.{k}") if isinstance(v, str) else v)
            for k, v in website.items()}
    elif isinstance(website, str):
        payloads["website"] = _clean_text(website, MAX_WEBSITE_CHARS,
                                          _losses=losses, _key="website")

    docs = payloads.get("documents")
    if isinstance(docs, list):
        dropped_docs = max(len(docs) - MAX_DOCUMENTS, 0)
        seen, deduped = set(), []
        for doc in docs[:MAX_DOCUMENTS]:
            if not isinstance(doc, dict):
                continue
            doc = dict(doc)
            doc["summary"] = _clean_text(
                doc.get("summary") or "", MAX_DOC_SUMMARY_CHARS,
                _losses=losses,
                _key=f"document.{doc.get('filename') or doc.get('id') or '?'}")
            # Two uploads of the same deck produce near-identical summaries.
            key = (doc.get("detected_type"), (doc.get("summary") or "")[:200])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(doc)
        payloads["documents"] = deduped

    research = payloads.get("research")
    if isinstance(research, dict):
        payloads["research"] = {
            k: (_clean_text(v, MAX_RESEARCH_CHARS, _losses=losses,
                            _key=f"research.{k}") if isinstance(v, str) else v)
            for k, v in research.items()}

    _report_compression(losses, dropped_docs)
    return _prune_empty(payloads)


def _report_compression(losses, dropped_docs=0):
    """One trace line per run for everything the compressor removed.

    Per-key lines would add up to a dozen entries to every run and get skimmed
    exactly like the eighteen `cp_extract` warnings did. One aggregate line
    carries the number that matters — how much of the corpus never reached a
    model — and names the worst offenders so the next question is answerable
    without re-running anything.

    Severity is deliberately graded. Losing a few hundred characters off a
    marketing page is noise; losing a quarter of the only live source is the
    difference between 75.0% and 43.8% assessment coverage.
    """
    if not losses and not dropped_docs:
        return
    from fundos.profile.trace import trace_event, OK, WARN
    total_in = sum(item["chars_in"] for item in losses)
    total_dropped = sum(item["chars_dropped"] for item in losses)
    pct = round(100.0 * total_dropped / total_in, 1) if total_in else 0.0
    worst = sorted(losses, key=lambda i: i["chars_dropped"], reverse=True)[:3]
    trace_event(
        "sources.compression", WARN if pct >= 10.0 else OK,
        keys_truncated=len(losses),
        documents_dropped=dropped_docs,
        chars_in=total_in,
        chars_kept=total_in - total_dropped,
        chars_dropped=total_dropped,
        pct_dropped=pct,
        worst=" | ".join(f"{i['key']}:-{i['chars_dropped']}" for i in worst),
        caps=f"website={MAX_WEBSITE_CHARS} doc_summary={MAX_DOC_SUMMARY_CHARS} "
             f"research={MAX_RESEARCH_CHARS} max_documents={MAX_DOCUMENTS}",
        note="a tail-slice removes the END of each page, which on a scraped "
             "site is typically About / Team / Investors. Raise the cap for "
             "the named keys if diligence signal is being lost.")


def _sources_are_empty(payloads):
    """True when the collection retrieved nothing worth hashing.

    Two failed source collections produce byte-identical payloads and
    therefore an identical fingerprint. Without this test the skip gate reads
    that as "sources unchanged" and declines to retry — which is exactly what
    stranded the 13 Aug Deepak Nitrite retry after a TLS failure.
    """
    p = payloads or {}
    website = p.get("website") or {}
    documents = p.get("documents") or []
    research = p.get("research") or {}
    founders = p.get("founders") or []
    return not (len(str(website)) > 2 or documents
                or len(str(research)) > 2 or founders)


def sources_fingerprint(payloads):
    """Stable hash of the source bundle.

    Used to skip regeneration when nothing has changed. A founder pressing
    "generate" twice used to pay twice for a byte-identical result.
    """
    import hashlib
    import json as _json
    try:
        blob = _json.dumps(payloads, default=str, sort_keys=True)
    except Exception:
        blob = repr(payloads)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _document_extracts(profile):
    from fundos.readiness.models import MaterialAsset
    from fundos.core.models import Deal

    deals = list(Deal.objects.filter(company=profile.company)
                 .values_list("id", flat=True))
    if not deals:
        return []
    assets = MaterialAsset.objects.filter(deal_id__in=deals)
    out = []
    for asset in assets:
        out.append({
            "category": asset.category,
            "detected_type": asset.detected_type,
            "summary": asset.summary,
            "metrics": asset.extracted_metrics,
            "extraction_status": asset.extraction_status,
        })
    return out


def document_readiness(profile):
    """Report whether uploaded documents have actually been read yet.

    TWO THINGS READ A DOCUMENT, AND ONLY ONE NEEDS A WORKER.

    * The profile pipeline (`profile.pipeline.source2_documents`) downloads
      each file and converts it INLINE during the run. No Celery, no broker.
      This is what puts a deck or a financial model into the dossier.
    * The background critique (`readiness.tasks.scan_and_analyse_material`)
      adds a summary, metrics and recommendations on top. That one is a
      Celery task, and it is an enrichment, not the read.

    This function answers the first question — "was my document read?" — so
    it counts both signals. Counting only the critique's fields meant a run
    that had just recovered 552,000 characters from a financial model
    reported `analysed=0`, which raised a CRITICAL diagnosis telling the
    operator to start infrastructure that was never in the path.

    Called before the pipeline runs, so zero-analysed on a first generation
    is the normal starting state rather than a fault.
    """
    from fundos.readiness.models import MaterialAsset
    from fundos.core.models import Deal
    from fundos.profile.models import ProfileDocument

    uploaded = ProfileDocument.objects.filter(profile=profile).count()
    deals = list(Deal.objects.filter(company=profile.company)
                 .values_list("id", flat=True))
    assets = MaterialAsset.objects.filter(deal_id__in=deals) if deals else []

    # WHAT COUNTS AS "ANALYSED" — two different things write to this row.
    #
    # `summary` / `extracted_metrics` come from the background AI critique
    # (readiness.tasks.scan_and_analyse_material). `extraction_status` and
    # `native_chars` come from the PIPELINE's own reader
    # (profile.pipeline.source2_documents), which downloads the file and
    # converts it inline — it needs no worker and no broker.
    #
    # Counting only the critique's fields meant a run that had read a
    # 552,000-character financial model reported `analysed=0`, which fired the
    # CRITICAL "start Celery and Redis" diagnosis on a run whose documents had
    # in fact been read perfectly. The advice was wrong and the state it
    # described did not exist. Text extraction is what "was my document read?"
    # means here; the critique is an enrichment on top of it.
    def _read(a):
        return bool(a.summary or a.extracted_metrics
                    or a.extraction_status == "extracted"
                    or (a.native_chars or 0) > 0)

    analysed = sum(1 for a in assets if _read(a))
    pending = sum(1 for a in assets
                  if not _read(a)
                  and a.extraction_status in ("", "pending", "queued", "running"))
    return {"uploaded": uploaded, "analysed": analysed, "pending": pending}


def _founder_inputs(profile):
    from fundos.profile.models import Founder
    return [{
        "name": f.name,
        "designation": f.designation,
        "linkedinUrl": f.linkedin_url,
        "experience": f.experience,
        "education": f.education,
        "previousCompanies": f.previous_companies,
        "biography": f.biography,
        "selfConfirmed": f.self_confirmed,
    } for f in Founder.objects.filter(profile=profile)]


def _research_payloads(profile, deal=None):
    """Run whichever research adapters are enabled and cleared.

    Adapters that are stubbed or unconfigured return thin payloads; the
    synthesis step marks the affected sections as needing input rather
    than inventing content.
    """
    from fundos.research.adapters.registry import ADAPTERS, enabled_sources
    from fundos.profile.trace import (trace_event as _trace,
                                      payload_size as _size, OK as _OK,
                                      FAIL as _FAIL, SKIP as _SKIP)

    enabled = list(enabled_sources())
    _trace("preflight.research_sources", _OK,
           enabled_count=len(enabled), enabled=",".join(enabled))

    if deal is None:
        deal = _context_deal(profile)
    if not deal:
        _trace("sources.research", _SKIP, reason="no deal context")
        return {}
    out = {}
    for source in enabled:
        if source in ("website", "linkedin"):
            continue  # handled separately
        adapter_cls = ADAPTERS.get(source)
        if not adapter_cls:
            continue
        try:
            import time as _t
            _s = _t.monotonic()
            # `ckb_snapshot`, not `ckb` — the wrong keyword raised a
            # TypeError inside this try on every adapter, so all four market
            # research sources returned an error payload instead of data.
            out[source] = adapter_cls(deal=deal, ckb_snapshot={}).fetch()
            fixture = bool(getattr(adapter_cls, "is_fixture", False))
            if fixture:
                # SKIP, not OK. These return a placeholder string pending
                # their feed; reporting success made nine dead adapters look
                # like nine working ones and put ~350 characters of "pending
                # feed ratification" into the source total, which is what
                # allowed a run with no real research to read as healthy.
                out.pop(source, None)
                _trace(f"sources.research.{source}", _SKIP,
                       reason="fixture adapter — no live feed behind this "
                              "source yet; contributes no facts")
                continue
            _trace(f"sources.research.{source}", _OK,
                   ms=int((_t.monotonic() - _s) * 1000),
                   chars=_size(out[source]))
        except Exception as e:
            logger.info("PROFILE: research source %s unavailable: %s", source, e)
            out[source] = {"facts": {}, "error": str(e)}
            _trace(f"sources.research.{source}", _FAIL,
                   error=f"{type(e).__name__}: {e}")
    return out


def generate_profile(profile, user=None, job=None, *, force=False,
                     consolidated=None):
    """Run the full profile generation. Never raises on partial failure.

    Delegates to the Company Master Data Pipeline
    (:mod:`fundos.profile.pipeline.orchestrator`), which researches the company
    across the configured question bank, reads its uploaded documents, merges
    both into one dossier, and derives the structured profile from that
    dossier in a single call.

    What stays here rather than moving into the pipeline is the part that is
    about *whether to run at all*:

    * SKIP-IF-UNCHANGED — the source bundle is fingerprinted. If nothing has
      moved since the last successful run, generation is a no-op. Pass
      force=True (or ?force=true on the API) to override.
    * THE RUN LOCK — two generations against one profile interleave and
      corrupt each other; see :func:`_profile_generation_lock`.

    ``consolidated`` is accepted and ignored. It selected between the old
    deep-extract and per-section paths, neither of which exists now; the
    parameter is kept so an older caller or a queued task argument does not
    raise TypeError mid-upgrade.
    """
    from fundos.profile.trace import GenerationTrace, current_trace

    started_at = timezone.now()

    # A trace is opened here (unless one is already active) so that every step
    # below records what it did. Without it, a run that produces an empty
    # profile leaves no evidence of WHICH step came back empty.
    outer = current_trace()
    tr = outer or GenerationTrace(
        profile=profile, company=profile.company, user=user,
        mode="pipeline", trigger="forced" if force else "normal")
    _own_trace = outer is None
    if _own_trace:
        tr.__enter__()

    try:
        preflight_report(profile)
        with _profile_generation_lock(profile, trace=tr):
            return _run_generation(profile, user=user, force=force,
                                   started_at=started_at, trace=tr)
    finally:
        if _own_trace:
            # __exit__ writes the diagnosis; a nested trace leaves that to
            # whoever opened it, so the verdict is logged exactly once.
            tr.__exit__(None, None, None)


def _run_generation(profile, *, user, force, started_at, trace):
    """Skip gate, run record, pipeline. Returns the API result dict."""
    from fundos.llm.adapter import llm_call_count
    from fundos.profile.models import ProfileGenerationRun
    from fundos.profile.pipeline.orchestrator import run_pipeline
    from fundos.profile.trace import trace_event, SKIP

    # Opened before anything is collected, so the figure covers every call the
    # run makes — including the assessment-inputs call the caller fires after
    # the pipeline returns. v27.4 moved this window twice for exactly that
    # reason: the only defensible definition of "calls this run made" is
    # "calls dispatched between the run starting and the run finishing".
    ledger_mark = llm_call_count()
    ensure_sections(profile)

    # The fingerprint still covers the OLD source bundle (website, documents,
    # research adapters, founders). That remains the right signal for "has
    # anything about this company changed since we last looked?" even though
    # the pipeline no longer feeds those payloads to a model directly: the
    # question the gate answers is whether a re-run could produce a different
    # answer, and the inputs to that question are unchanged.
    payloads, failures = collect_sources(profile, user=user)
    fingerprint = sources_fingerprint(payloads)
    profile._last_payloads = payloads

    if (not force and profile.sources_hash == fingerprint
            and profile.status == "generated" and profile.last_generated_at
            # Belt and braces: never let a fingerprint stamped over an empty
            # collection suppress a retry.
            and not _sources_are_empty(payloads)
            and not failures):
        logger.info("PROFILE: sources unchanged for %s — skipping generation "
                    "(saved a full run).", profile.company_id)
        # The single most misread behaviour in the pipeline: an admin fixes a
        # setting, regenerates, sees no change, and concludes the fix failed.
        # Say plainly what happened and what to do instead.
        trace_event("generation.skipped", SKIP,
                    reason="sources unchanged since the last successful run",
                    remedy="regenerate with force=true to override this")
        return {
            "generated": [], "skipped": [], "sourceFailures": failures,
            "completenessPct": float(profile.completeness_pct or 0),
            "skippedReason": "sources_unchanged",
            "lastGeneratedAt": profile.last_generated_at,
            "mode": "skipped",
        }

    run = ProfileGenerationRun.objects.create(
        profile=profile, tenant_id=profile.tenant_id, triggered_by=user,
        mode="pipeline", forced=force, sources_hash=fingerprint,
        source_stats=_source_stats(payloads), source_failures=failures,
        status="running", stage="queued", started_at=started_at)

    profile.status = "generating"
    profile.save(update_fields=["status", "updated_at"])

    result = run_pipeline(profile, run, user=user, trace=trace)

    # Stamp the fingerprint only on a run that actually published something.
    # A refused or failed run must stay re-runnable without force — stamping
    # it would make the skip gate suppress the retry that fixes it.
    if result.get("mode") == "pipeline":
        _stamp_sources_hash(profile, fingerprint, failures=failures,
                            extra_failed=bool(result.get("skipped")))
        profile.save(update_fields=["sources_hash", "updated_at"])
        _stamp_section_hashes(profile, result.get("generated") or [],
                              fingerprint)

    # AFTER assessment, so both the trace line and the stored figure include
    # it. v27.4 moved this window twice for exactly that reason: assessment
    # inputs is one of the roles that is supposed to search, and a call count
    # that omits it cannot be used to check whether it ran.
    #
    # `payloads` was collected BEFORE the pipeline ran, and its `documents`
    # entry is whatever the background critique had managed to write by then
    # — routinely nothing, since uploads and generation are seconds apart.
    # The pipeline has since read those same files properly. Handing the
    # assessment the stale bundle meant the scorecard was scored without the
    # documents while the profile beside it was scored with them.
    #
    # Rebuilt rather than mutated in place, so nothing that already consumed
    # `payloads` above sees it change underneath.
    # Popped, not read: `result` is serialised into the job payload and the
    # API response, and a 300 KB dossier does not belong in either. It is
    # carried out of the pipeline for this one consumer.
    dossier_text = result.pop("dossierText", "")
    assessment_payloads = dict(payloads)
    if result.get("mode") == "pipeline":
        if dossier_text:
            assessment_payloads["dossier"] = dossier_text
        try:
            # Re-read the material summaries too: the critique may have
            # finished while the pipeline was running.
            assessment_payloads["documents"] = _document_extracts(profile)
        except Exception:
            logger.debug("could not refresh document extracts for the "
                         "assessment", exc_info=True)
    result["assessmentInputs"] = _run_assessment_inputs(
        profile, assessment_payloads, user=user)
    result["llmCalls"] = _emit_run_economics(ledger_mark, mode="pipeline")
    _finish_run_record(run, started_at, ledger_mark)
    return result


def _finish_run_record(run, started_at, ledger_mark):
    """Close out the run row: duration and the economics roll-up.

    Written after the pipeline returns, whichever way it ended. A run that
    failed at synthesis still spent everything the research stage cost, and
    that is exactly the number worth knowing about a failure.

    Read from the adapter's ledger rather than by querying LLMCallLog on a
    timestamp: the ledger is keyed to THIS run (the research threads adopt the
    parent's book), whereas a time-window query over a shared table would sweep
    up every other tenant's concurrent calls and attribute them here.
    """
    from fundos.llm.adapter import llm_ledger_since
    from fundos.llm.ledger import summarise

    # `_isolated`, not a bare try/except. This is a best-effort bookkeeping
    # write at the very end of a run, and a bare swallow around a failed
    # statement leaves the transaction needing rollback — so the NEXT query
    # anywhere dies with "you can't execute queries until the end of the
    # 'atomic' block" and gets the blame. The savepoint keeps the failure
    # local, which is the whole point of the helper.
    with _isolated("run economics"):
        totals = summarise(llm_ledger_since(ledger_mark))
        run.duration_ms = int(
            (timezone.now() - started_at).total_seconds() * 1000)
        run.llm_calls = totals["calls"]
        run.total_cost_inr = totals["cost_inr"]
        run.total_search_count = totals["searches"]
        run.save(update_fields=["duration_ms", "llm_calls", "total_cost_inr",
                                "total_search_count"])



@contextlib.contextmanager
def _profile_generation_lock(profile, trace=None):
    """Serialise generation per profile, and confirm the profile still exists.

    Nothing previously stopped two generations running against the same
    profile at once, and on a live install two started ten seconds apart.
    They interleave: the "only seed when the section is empty" checks each
    see an empty section, both write, and records are duplicated. Worse was
    observed — five section writes failed with

        IntegrityError: Key (profile_id)=(…) is not present in company_profile

    because the profile row was replaced underneath a run already holding a
    stale object. That is silent corruption of a founder-facing document.

    A PostgreSQL advisory lock is used rather than a row lock: it costs
    nothing, needs no schema, is released automatically if the process dies,
    and does not block reads of the profile while a run is in progress. On
    backends without advisory locks (sqlite in tests) this degrades to a
    no-op, which is correct — those are single-writer anyway.
    """
    from django.db import connection

    from fundos.core.exceptions import GenerationAlreadyRunning
    from fundos.profile.models import CompanyProfile
    from fundos.profile.trace import FAIL, WARN, trace_event

    key = int(str(profile.pk).replace("-", "")[:15], 16) % (2 ** 31)
    vendor = connection.vendor
    acquired = True
    if vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", [key])
            acquired = bool(cur.fetchone()[0])
        if not acquired:
            # Waiting would queue a second full run behind the first and
            # produce a duplicate of work already being done.
            trace_event("generation.lock", WARN, profile=str(profile.pk),
                        note="another generation is already running for this "
                             "profile — this run was skipped rather than "
                             "interleaved with it")
            raise GenerationAlreadyRunning(
                "A generation is already in progress for this company. "
                "Wait for it to finish, then retry.")
    try:
        # Re-read under the lock. The object handed in may have been loaded
        # before another process deleted or replaced the row.
        if not CompanyProfile.objects.filter(pk=profile.pk).exists():
            trace_event("generation.lock", FAIL, profile=str(profile.pk),
                        error="the profile row no longer exists — it was "
                              "deleted or replaced after this run started")
            raise GenerationAlreadyRunning(
                "This company's profile was changed while generation was "
                "starting. Retry.")
        yield
    finally:
        if vendor == "postgresql" and acquired:
            try:
                with connection.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", [key])
            except Exception:  # pragma: no cover — connection already gone
                logger.warning("PROFILE: advisory unlock failed for %s",
                               profile.pk)


def preflight_report(profile):
    """Record the configuration state BEFORE generation runs.

    Most "empty profile" reports are a setting, not a fault. Capturing the
    settings at the top of every run means the answer is already in the log
    rather than needing a separate investigation on a system you cannot see.
    """
    from fundos.profile.trace import trace_event, OK, WARN

    fields = {}
    try:
        from fundos.config.feature_flags import ai_mocked
        fields["ai_mocked"] = bool(ai_mocked())
    except Exception as e:
        fields["ai_mocked"] = f"unknown ({e})"
    try:
        from fundos.llm.models import LLMConfigProfile, LLMEndpoint, \
            LLMRoleBinding
        from fundos.profile.pipeline import settings as pipeline_settings
        fields["llm_bindings"] = LLMRoleBinding.objects.count()
        fields["llm_endpoints_active"] = LLMEndpoint.objects.filter(
            is_active=True).count()
        # The two roles a run actually dispatches. Reporting anything else
        # here answers a question about a call that will not be made — which
        # is worse than reporting nothing, because it looks like an answer.
        #
        # The model is the part where that is easiest to get wrong. Both roles
        # pin an LLMConfigProfile by code, and a pinned profile's
        # `model_string` is what the adapter dispatches on — the endpoint's
        # `default_model` is only the fallback for a call that pins nothing.
        # Reporting the endpoint default therefore named a model no call in
        # the run would use, which is precisely the failure this block's own
        # comment warns against: a live run researched on gemini-2.5-flash
        # while every preflight line in the log said gemini-3.6-flash.
        pins = {"research": pipeline_settings.research_config_profile(),
                "synthesis": pipeline_settings.synthesis_config_profile()}
        for role, label in (("profile_research_batch", "research"),
                            ("profile_synthesis", "synthesis")):
            binding = LLMRoleBinding.objects.filter(
                role=role).select_related("primary_endpoint").first()
            if binding and binding.primary_endpoint:
                fields[f"{label}_endpoint"] = binding.primary_endpoint.code
                fields[f"{label}_model"] = (
                    binding.primary_endpoint.default_model or "")
                fields[f"{label}_mocked"] = bool(binding.is_mocked)
            else:
                fields[f"{label}_endpoint"] = ""
            pinned = LLMConfigProfile.objects.filter(
                code=pins[label], is_active=True).first()
            if pinned:
                if pinned.model_string:
                    fields[f"{label}_model"] = pinned.model_string
                if pinned.endpoint_id:
                    fields[f"{label}_endpoint"] = pinned.endpoint_id
                fields[f"{label}_profile"] = pinned.code
            else:
                # Not cosmetic. An absent or deactivated pin does not raise —
                # the adapter falls back to tier resolution — so the run
                # silently dispatches on the tier's model instead. This is the
                # only place that discrepancy is visible before the fact.
                fields[f"{label}_profile"] = f"{pins[label]} (UNRESOLVED)"
    except Exception as e:
        fields["llm_error"] = str(e)
    try:
        from fundos.platformcfg.services import profile_sections
        fields["registered_sections"] = len(profile_sections())
    except Exception as e:
        fields["registered_sections"] = f"unknown ({e})"
    try:
        from django.conf import settings as _s
        fields["celery_eager"] = bool(getattr(
            _s, "CELERY_TASK_ALWAYS_EAGER", False))
    except Exception:
        pass

    # IS THE ASSESSMENT WORKBOOK SEEDED?
    #
    # `extract_assessment_inputs` returns error="no_config" when no parameters
    # are configured, and it does so AFTER the profile pipeline has run — ten
    # search-grounded research calls and one large synthesis call already paid
    # for. That was the single most frequent failure signature in the
    # generation log (44 occurrences), and every one was the same unseeded
    # environment.
    #
    # `question_set` is called rather than its filter re-implemented here: a
    # second copy of that query is exactly how a preflight starts predicting
    # something the real gate no longer does.
    #
    # This never blocks the run. The profile half is independently useful, and
    # refusing to generate it would be a worse outcome than an unscored
    # scorecard.
    try:
        from fundos.profile.assessment_extraction import question_set
        numeric, anchors = question_set(getattr(profile, "tenant_id", None))
        fields["assessment_parameters"] = len(numeric) + len(anchors)
        if not fields["assessment_parameters"]:
            fields["assessment_config"] = (
                "NOT SEEDED — the scorecard will be skipped with "
                "error='no_config'. Run: manage.py seed_assessment_config")
    except Exception as e:
        fields["assessment_parameters"] = f"unknown ({e})"

    # NEW: Check PromptTemplate freshness (Fix 7 effectiveness)
    #
    # The PromptTemplate row contains Fix 7 (sourceDoc citation support).
    # If the row is stale (pre-dates the deployment), the updated prompt in
    # default_prompts.py is never used, and Fix 7 has no effect.
    #
    # This check warns ops to run seed_platform_config --reset-prompts if
    # the prompt is known to be old.
    try:
        from fundos.platformcfg.models import PromptTemplate
        from datetime import timedelta
        from django.utils import timezone
        
        critical_roles = [
            "assessment_inputs",
            "profile_research_batch",
            "profile_synthesis",
        ]
        stale_prompts = []
        missing_prompts = []
        
        for role in critical_roles:
            try:
                row = PromptTemplate.objects.filter(
                    role=role
                ).latest('version_no')
                age_days = (timezone.now() - row.created_at).days
                
                # Warn if older than 30 days (likely stale after deployment)
                if age_days > 30:
                    stale_prompts.append({
                        "role": role,
                        "version": row.version_no,
                        "age_days": age_days,
                    })
            except PromptTemplate.DoesNotExist:
                missing_prompts.append(role)
        
        if stale_prompts:
            import logging as _logging
            _log = _logging.getLogger("fundos.generation")
            _log.warning(
                "PREFLIGHT: %d prompt(s) may be stale (>30 days old). "
                "Fix 7 (sourceDoc citation) may not work. "
                "Run: seed_platform_config --reset-prompts. "
                "Stale: %s",
                len(stale_prompts),
                ", ".join([f"{p['role']} (v{p['version']}, {p['age_days']}d old)" 
                          for p in stale_prompts])
            )
            fields["prompt_warnings"] = f"{len(stale_prompts)} stale prompt(s)"
        
        if missing_prompts:
            import logging as _logging
            _log = _logging.getLogger("fundos.generation")
            _log.critical(
                "PREFLIGHT FATAL: %d required prompt(s) not seeded: %s. "
                "Run: seed_platform_config",
                len(missing_prompts), ", ".join(missing_prompts)
            )
            fields["prompt_errors"] = f"{len(missing_prompts)} missing prompt(s)"
            
    except Exception as e:
        import logging as _logging
        _log = _logging.getLogger("fundos.generation")
        _log.debug("Could not check PromptTemplate freshness: %s", e)

    problem = (fields.get("ai_mocked") is True
               or fields.get("llm_bindings") == 0
               or fields.get("llm_endpoints_active") == 0
               or fields.get("assessment_parameters") == 0
               or "prompt_errors" in fields)
    trace_event("preflight.config", WARN if problem else OK, **fields)
    return fields





# --------------------------------------------------------------------------
# Run-level diagnostics
#
# Per-call logs answer "what did that cost?". These answer "why was the
# output like that?" — which is the question you need to improve a prompt,
# a model choice, or a source adapter.
# --------------------------------------------------------------------------

def _emit_run_economics(mark, *, mode=""):
    """Emit `run.economics` (+ a per-role breakdown) and return the call count.

    v27.4 — WHY THIS EXISTS.

    Costing the 17 Aug runs meant parsing eleven `llm.call.*` lines out of a
    135-line file and adding up six columns by hand, and the total still came
    out 39% low because thinking tokens were missing from the only column that
    reached the database. The per-role concentration that turned out to be the
    whole story — 14 calls burning 51,092 input tokens to return 3,217 output
    tokens — was invisible until someone built that table manually.

    A run should state its own economics. Two lines: the totals, and the
    per-role split ordered by spend, so the next question ("which role is
    expensive, and is it earning it?") is answerable from the log alone.

    Never raises. Accounting that can break a generation is worse than no
    accounting, and this runs after the user's output is already safe.
    """
    from fundos.profile.trace import trace_event, OK, WARN
    try:
        from fundos.llm import ledger
        from fundos.llm.adapter import llm_ledger_since
        entries = llm_ledger_since(mark)
        totals = ledger.summarise(entries)

        # A run that searched nothing on a role that must search is the single
        # most important fact this line can carry, and it is the one that six
        # releases of healthy-looking logs managed not to state plainly.
        searched = totals["searches"]
        status = OK
        concerns = []
        if totals["failed_calls"]:
            concerns.append(f"{totals['failed_calls']} call(s) failed")
        if totals["mocked_calls"]:
            concerns.append(f"{totals['mocked_calls']} call(s) were MOCKED")
        if entries and not searched:
            concerns.append("no web search was performed by any call")
        if totals["redundant_input_pct"] >= 30.0:
            concerns.append(
                f"{totals['redundant_input_pct']}% of input was re-sent context")
        if concerns:
            status = WARN

        trace_event(
            "run.economics", status,
            mode=mode or "unknown",
            llm_calls=totals["calls"],
            failed_calls=totals["failed_calls"],
            mocked_calls=totals["mocked_calls"],
            prompt_tokens=totals["prompt_tokens"],
            completion_tokens=totals["completion_tokens"],
            thinking_tokens=totals["thinking_tokens"],
            # completion + thinking. Every provider bills reasoning at the
            # output rate and none include it in the completion count.
            billed_output_tokens=totals["billed_output_tokens"],
            cache_read_tokens=totals["cache_read_tokens"],
            cache_write_tokens=totals["cache_write_tokens"],
            total_tokens=totals["total_tokens"],
            cost_inr=totals["cost_inr"],
            llm_latency_ms=totals["latency_ms"],
            searches=searched,
            searches_unknown=totals["searches_unknown"],
            redundant_input_pct=totals["redundant_input_pct"],
            concerns="; ".join(concerns) or "none",
            note="cost_inr uses the seeded price book; billed_output includes "
                 "thinking. searches_unknown counts calls whose provider "
                 "returned no grounding metadata — that is NOT the same as "
                 "zero searches and needs a different fix.")

        for row in ledger.by_role(entries):
            trace_event(
                "run.economics.role", OK,
                role=row["role"], calls=row["calls"],
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                thinking_tokens=row["thinking_tokens"],
                cost_inr=row["cost_inr"], latency_ms=row["latency_ms"],
                searches=row["searches"],
                searches_unknown=row["searches_unknown"],
                tokens_in_per_token_out=_ratio(
                    row["prompt_tokens"],
                    row["completion_tokens"] + row["thinking_tokens"]))
        return totals["calls"]
    except Exception as e:
        logger.warning("PROFILE: run economics could not be computed (%s) — "
                       "the generation itself is unaffected.", e)
        try:
            from fundos.llm.adapter import llm_calls_since
            return llm_calls_since(mark)
        except Exception:
            return 0


def _ratio(numerator, denominator):
    """Input tokens spent per output token. The fan-out's fingerprint.

    A role at 3:1 is doing work. A role at 40:1 is re-reading a corpus to
    produce a paragraph, which is the C-03 signature and the number that
    should fall when the extract schema is widened.
    """
    if not denominator:
        return "n/a"
    return round(float(numerator) / float(denominator), 1)


def _source_stats(payloads):
    """Measure what the model actually had to work with.

    Output quality is mostly a function of input quality. Without this, a
    thin website and a bad prompt produce identical-looking failures.
    """
    payloads = payloads or {}
    stats = {}

    website = payloads.get("website")
    if isinstance(website, dict):
        stats["website_chars"] = sum(
            len(v) for v in website.values() if isinstance(v, str))
        stats["website_keys"] = sorted(website.keys())[:10]
    elif isinstance(website, str):
        stats["website_chars"] = len(website)

    docs = payloads.get("documents")
    if isinstance(docs, list):
        stats["document_count"] = len(docs)
        stats["document_chars"] = sum(
            len(d.get("summary") or "") for d in docs if isinstance(d, dict))
        stats["document_types"] = sorted(
            {d.get("detected_type") for d in docs
             if isinstance(d, dict) and d.get("detected_type")})[:10]

    research = payloads.get("research")
    if isinstance(research, dict):
        stats["research_keys"] = sorted(research.keys())[:10]
        stats["research_chars"] = sum(
            len(v) for v in research.values() if isinstance(v, str))

    founders = payloads.get("founders")
    if isinstance(founders, list):
        stats["founder_count"] = len(founders)

    stats["total_chars"] = (stats.get("website_chars", 0)
                            + stats.get("document_chars", 0)
                            + stats.get("research_chars", 0))
    return stats


def _run_llm_stats(profile, since):
    """Aggregate the calls this run actually made."""
    from decimal import Decimal
    from django.db.models import Count, Sum
    try:
        from fundos.llm.models import LLMCallLog
        agg = LLMCallLog.objects.filter(
            created_at__gte=since,
            calling_context__startswith="profile."
        ).aggregate(calls=Count("id"), cost=Sum("cost_inr"),
                    searches=Sum("search_count"))
        return (agg.get("calls") or 0,
                agg.get("cost") or Decimal("0"),
                max(agg.get("searches") or 0, 0))
    except Exception as e:
        logger.debug("PROFILE: run LLM stats unavailable: %s", e)
        return 0, Decimal("0"), 0


def _record_generation_run(profile, *, started_at, mode, forced, fingerprint,
                           payloads, failures, generated, skipped,
                           extraction=None, user=None, error=""):
    """Write one diagnostic row per generation. Never raises.

    Deliberately written once and never updated, so a series of rows is a
    time series you can regress against prompt and model changes.
    """
    from django.utils import timezone as _tz
    from fundos.profile.models import ProfileGenerationRun, ProfileSection
    # v27.5 — savepointed. This is the LAST write of a run, so a
    # failure here used to poison the transaction for the response
    # serialisation that follows it.
    with _isolated_write("generation run record"):
        needing = list(ProfileSection.objects.filter(
            profile=profile, is_active=True, needs_input=True
        ).values_list("section_key", flat=True))

        calls, cost, searches = _run_llm_stats(profile, started_at)
        extraction = extraction or {}
        conflicts = extraction.get("conflicts") or []
        resolutions = extraction.get("conflict_resolution") or []

        ProfileGenerationRun.objects.create(
            profile=profile,
            tenant_id=getattr(profile.company, "tenant_id", None),
            triggered_by=user if getattr(user, "pk", None) else None,
            mode=mode, forced=bool(forced), sources_hash=fingerprint or "",
            source_stats=_source_stats(payloads),
            source_failures=failures or [],
            sections_generated=list(generated or []),
            sections_skipped=[s.get("section") if isinstance(s, dict) else s
                              for s in (skipped or [])],
            sections_needing_input=needing,
            completeness_pct=profile.completeness_pct or 0,
            judgment_ran=bool(profile.judgment),
            conflicts_found=len(conflicts),
            conflicts_resolved=len(resolutions),
            llm_calls=calls, total_cost_inr=cost,
            total_search_count=searches,
            duration_ms=int((_tz.now() - started_at).total_seconds() * 1000),
            error=(error or "")[:2000])




def _assessment_inputs_enabled():
    """Feature flag for the Phase 3 bridge extraction (default ON).

    Off is a real operating state, not just a kill switch: a tenant that never
    runs step 2 pays for this call for nothing, and the profile is complete
    without it. Defaults ON because the far more common failure was the
    opposite — a fully researched company scoring blank in step 2 because
    nothing carried the research across.
    """
    try:
        from fundos.config.feature_flags import get_flag
        return bool(get_flag("PROFILE_ASSESSMENT_INPUTS", True))
    except Exception:
        return True


def _run_assessment_inputs(profile, payloads, user=None):
    """Emit the assessment parameter values for this company (Phase 3).

    Isolated exactly like a source adapter: a failure here marks itself and
    leaves the profile intact. The profile is a founder-facing document that
    stands on its own, and losing it because a downstream scorecard extraction
    failed would be a bad trade for the person waiting on it.
    """
    if not _assessment_inputs_enabled():
        return {"skipped": "disabled"}
    from fundos.profile.trace import trace_event, OK, FAIL, SKIP
    try:
        from fundos.profile.assessment_extraction import (
            extract_assessment_inputs)
        counts = extract_assessment_inputs(profile, payloads=payloads,
                                           user=user)
        if counts.get("error"):
            trace_event("assessment_inputs", FAIL, **counts)
        elif not counts.get("written"):
            trace_event(
                "assessment_inputs", SKIP,
                reason="the extraction returned no usable parameter values",
                remedy="check the website and research payloads are populated",
                **counts)
        else:
            trace_event("assessment_inputs", OK, **counts)
        return counts
    except Exception as e:
        logger.warning("PROFILE: assessment-input extraction failed: %s", e)
        trace_event("assessment_inputs", FAIL, error=str(e))
        return {"error": str(e)}


def _section_is_current(profile, section_key, fingerprint):
    """True when this section was generated from exactly these sources."""
    from fundos.profile.models import ProfileSection
    row = ProfileSection.objects.filter(
        profile=profile, section_key=section_key, is_active=True
    ).only("source_hash", "edited_by_user", "needs_input").first()
    if row is None:
        return False
    # A section still flagged needs-input is worth retrying — the sources may
    # be the same but a prompt or model change could now resolve it.
    if row.needs_input:
        return False
    return bool(row.source_hash) and row.source_hash == fingerprint


def _stamp_section_hashes(profile, section_keys, fingerprint):
    """Record which source bundle produced each section we just generated.

    Keys arrive as WIRE names and rows are keyed by STORAGE names, which
    differ for five sections. Translating here rather than at the call site
    keeps the caller free of that distinction — and without it the stamp
    silently missed those five, which would look like nothing at all until a
    later skip decision quietly used a stale hash.
    """
    from fundos.profile.models import ProfileSection
    from fundos.profile.schema import storage_key_for

    section_keys = [storage_key_for(k) for k in (section_keys or [])]
    if not section_keys:
        return
    with _isolated_write("section hash stamping"):        # v27.5
        ProfileSection.objects.filter(
            profile=profile, section_key__in=list(section_keys), is_active=True
        ).update(source_hash=fingerprint)




# Sections whose AI generation produces STRUCTURED records / form items
# rather than prose. Record-backed list sections + the numeric forms.
STRUCTURED_GEN_SECTIONS = {
    "founders", "key_people", "competitors", "funding_history", "recent_news",
    "revenue_model", "company_metrics", "financial_summary",
}

# Sections that carry structured §7 data but read as "narrative" — the four
# the Gap2 doc flagged as populated in content but empty in sections.data.
# Generated via the company_profile_structured role and stored in
# ProfileSection.structured so the clean sections.data is populated.
STRUCTURED_NARRATIVE_SECTIONS = {
    "products_services", "customers_markets", "competitive_advantages",
    "business_model",
}


# Company Profile extraction attaches a per-section LLM config profile by a
# stable code convention: `cp_extract.<section_key>` (Phase 2). If a profile
# with that code exists and is active in admin, its model + capabilities
# (e.g. Gemini Search grounding / URL context / responseSchema) drive that
# section's generation. If none is configured, generation runs exactly as
# before — so this is fully backward-compatible and opt-in per section.
def _section_config_profile(section_key):
    return f"cp_extract.{section_key}"


def _generate_structured_narrative(profile, cfg, payloads, user=None):
    """Populate a narrative-but-structured section's §7 data.

    Calls the `company_profile_structured` role and stores the returned
    shape into ProfileSection.structured (so sections.data is populated),
    keeping the prose `content` as a fallback. Never overwrites a value a
    human has edited.
    """
    from fundos.llm import guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.profile.models import ProfileSection

    existing = ProfileSection.objects.filter(
        profile=profile, section_key=cfg.section_key, is_active=True).first()
    if existing and existing.edited_by_user:
        return  # respect founder edits

    context = {
        "company": {"name": profile.company.name,
                    "website": profile.website_url,
                    "hqCountry": profile.hq_country,
                    "homeCurrency": profile.home_currency},
        "section_key": cfg.section_key,
        "section_label": cfg.label,
        "website_extract": payloads.get("website", {}),
        "document_extracts": payloads.get("documents", []),
        "research": payloads.get("research", {}),
        "founders": payloads.get("founders", []),
        # Conclusions from the Judgement tier, when one has run. Costs a few
        # hundred tokens and stops a cheap Simple call contradicting the
        # adjudication the expensive call already paid for.
        "judgment": judgment_context(profile),
    }
    result = llm_generate(
        role="company_profile_structured",
        context=context, deal_id=None, user=user,
        calling_context=f"profile.generate.{cfg.section_key}",
        section_context={"section_key": cfg.section_key,
                         "section_label": cfg.label},
        config_profile=_section_config_profile(cfg.section_key))
    result = guardrail.apply("company_profile_structured", result)

    # Normalise into the structured blob the serializer reads.
    if cfg.section_key == "business_model":
        obj = result.get("object") or {}
        structured = {
            "customer_type": obj.get("customer_type", ""),
            "value_proposition": obj.get("value_proposition", ""),
            "delivery_model": obj.get("delivery_model", ""),
            "pricing_model": obj.get("pricing_model", ""),
            "sales_model": obj.get("sales_model", ""),
            "distribution_channels": obj.get("distribution_channels", []) or [],
        }
        has_data = any(v for v in structured.values())
    else:
        items = result.get("items") or []
        structured = {"items": items}
        has_data = bool(items)

    content = result.get("content") or ""
    update_section(
        profile, cfg.section_key,
        content=content, structured=structured,
        user=user, by_ai=True, source="ai_research",
        needs_input=bool(result.get("needsInput")) or not has_data,
        missing=result.get("missing", []),
        change_summary="Generated structured data from company sources")

# Which model backs each record-list section (mirrors records.RECORD_TYPES).
_RECORD_SECTION_MODELS = {
    "founders": "Founder",
    "key_people": "KeyPerson",
    "competitors": "Competitor",
    "funding_history": "FundingRound",
    "recent_news": "NewsItem",
}


def _generate_structured_section(profile, cfg, payloads, user=None):
    """Auto-populate a structured section from AI (tester Issue #5).

    Calls the `company_profile_records` role, then materialises the result:
      * list sections  → create typed rows (KeyPerson/Competitor/…), but
        only when the section is still empty, so we never clobber or
        duplicate records the founder has already entered/edited.
      * numeric forms  → validate + store items via update_structured_form
        (which also syncs financials to the CKB — see Issue #1).
    Fields the model could not source are omitted upstream and simply stay
    empty, leaving the section flagged needs-input for the founder. The AI
    never overwrites a human-verified value.
    """
    from fundos.llm import guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.profile.records import RECORD_TYPES, _apply_fields, _model

    context = {
        "company": {"name": profile.company.name,
                    "website": profile.website_url,
                    "hqCountry": profile.hq_country,
                    "homeCurrency": profile.home_currency},
        "section_key": cfg.section_key,
        "section_label": cfg.label,
        "website_extract": payloads.get("website", {}),
        "document_extracts": payloads.get("documents", []),
        "research": payloads.get("research", {}),
        "founders": payloads.get("founders", []),
        # Conclusions from the Judgement tier, when one has run. Costs a few
        # hundred tokens and stops a cheap Simple call contradicting the
        # adjudication the expensive call already paid for.
        "judgment": judgment_context(profile),
    }
    result = llm_generate(
        role="company_profile_records",
        context=context, deal_id=None, user=user,
        calling_context=f"profile.generate.{cfg.section_key}",
        section_context={"section_key": cfg.section_key,
                         "section_label": cfg.label},
        config_profile=_section_config_profile(cfg.section_key))
    result = guardrail.apply("company_profile_records", result)

    if cfg.section_key in _RECORD_SECTION_MODELS:
        spec = RECORD_TYPES[cfg.section_key]
        Model = _model(spec["model"])
        # Don't overwrite founder data: only seed when the section is empty.
        if not Model.objects.filter(profile=profile).exists():
            rows = result.get("records") or []
            for i, row in enumerate(rows):
                # v27.5 — savepointed. One bad row used to abort the whole
                # transaction and every later write blamed itself.
                with _isolated_write(
                        f"record seed {cfg.section_key} row {i}"):
                    inst = Model(tenant_id=profile.company.tenant_id,
                                 profile=profile, source="ai_research",
                                 sort_order=i + 1, created_by=user)
                    _apply_fields(inst, spec, row, partial=True)
                    inst.save()
    else:
        # Numeric form section.
        structured = result.get("structured") or {}
        items = structured.get("items") or []
        # Only seed when the founder hasn't already filled the form.
        from fundos.profile.models import ProfileSection
        existing = ProfileSection.objects.filter(
            profile=profile, section_key=cfg.section_key, is_active=True).first()

        if cfg.section_key == "financial_summary":
            # financial_summary is an OBJECT section ({financials, observations}),
            # not a percent-summing numeric form. Store it in the spec shape the
            # serializer reads, preserving observations (backend-issues #1).
            already = bool((existing.structured or {}).get("financials")
                           or (existing.structured or {}).get("items")) \
                if existing else False
            if not already and (items or structured.get("observations")):
                financials = [_fin_row_to_spec(r) for r in items]
                observations = list(structured.get("observations") or [])
                has = bool(financials or observations)
                update_section(
                    profile, cfg.section_key,
                    structured={"financials": financials,
                                "observations": observations},
                    user=user, by_ai=True, needs_input=not has,
                    change_summary="Generated financial summary")
            recompute_structured_sections(profile)
            return

        already = bool((existing.structured or {}).get("items")) if existing else False
        if items and not already:
            # Map stored field names back to the API field names the
            # validator expects, per section.
            api_items, dropped = _records_items_to_api(cfg.section_key, items)
            if dropped:
                from fundos.profile.trace import WARN, trace_event
                trace_event("records.form_seed", WARN,
                            section=cfg.section_key,
                            kept=len(api_items), dropped=len(dropped),
                            reason="; ".join(dropped[:3]))
            if not api_items:
                return
            try:
                update_structured_form(profile, cfg.section_key,
                                       {"items": api_items}, user=user)
                # An unweighted draft is saved but INCOMPLETE. Marking it
                # keeps completeness honest and puts the section in front of
                # the user, who is the only one who knows the split.
                if cfg.section_key == "revenue_model" and not any(
                        r.get("pct") is not None for r in api_items):
                    _mark_needs_input(profile, cfg.section_key)
            except Exception as e:
                from fundos.profile.trace import FAIL, trace_event
                # The AI produced rows and the section stayed empty. Without
                # a trace step this reads as "the model returned nothing",
                # sending the reader after the model when the fault is a
                # validator rejecting the shape it was handed.
                logger.warning("PROFILE: form seed %s skipped: %s",
                               cfg.section_key, e)
                trace_event("records.form_seed", FAIL,
                            section=cfg.section_key,
                            rows=len(api_items or []),
                            sample=str(api_items[0])[:160] if api_items else "",
                            error=f"{type(e).__name__}: {e}")

    recompute_structured_sections(profile)


def _fin_row_to_spec(r):
    """Normalise an AI financial row to the section's field shape.

    Every key the schema asks for on a financial row is carried through.
    Previously four were kept — year, revenue, EBITDA and a growth figure — and
    ``pat`` and ``currency`` were dropped, which is why eleven rows of a live
    profile arrived with neither while the section's own observations discussed
    PAT figures. The model had them; this function threw them away.

    The growth field no longer falls back to ``pat``. That fallback wrote a
    profit-after-tax straight into a percentage column, so a loss of -6.32
    (crore) was stored and rendered as -6.32% year-on-year growth: a plausible
    number, in the right shape, describing something else entirely. Falling
    back across units is never a recovery — an absent growth rate is correct
    and a fabricated one is not.
    """
    if not isinstance(r, dict):
        return {}

    def pick(*keys):
        for k in keys:
            if k in r and r[k] not in (None, ""):
                return r[k]
        return None

    return {
        "year": pick("year", "financial_year", "fiscalYear", "fy", "period"),
        "revenue_m": pick("revenue_m", "revenue", "totalRevenue", "sales"),
        "ebitda_m": pick("ebitda_m", "ebitda", "operatingProfit"),
        "pat_m": pick("pat_m", "pat", "profitAfterTax", "netProfit",
                      "net_income"),
        "growth_pct": pick("growth_pct", "yoy_revenue_growth_pct",
                           "yoyRevenueGrowthPct", "revenueGrowthPct"),
        "gross_margin_pct": pick("gross_margin_pct", "grossMarginPct",
                                 "grossMargin"),
        "ev_revenue_multiple": pick("ev_revenue_multiple",
                                    "evRevenueMultiple"),
        # An unmarked row is an actual. Stated explicitly rather than left to
        # `.get()` returning None somewhere downstream, because the estimate
        # flag now decides whether a figure may reach the valuation.
        "is_estimate": bool(pick("is_estimate", "isEstimate", "estimate")
                            or False),
        "currency": pick("currency", "ccy", "currencyCode"),
    }


# What the model actually calls each field, per form. The validator is
# strict by design — it backs a founder-facing form where a wrong number is
# worse than a missing one — but the AI is not filling in that form, it is
# proposing rows. Handing its output to the validator unmapped discarded
# every row of revenue_model and company_metrics on every live run.
_FORM_FIELD_ALIASES = {
    "revenue_model": {
        "label": ("label", "name", "stream", "streamName", "source",
                  "revenue_stream", "revenueStream", "segment", "category",
                  "title", "modelType", "type", "description"),
        "pct": ("pct", "percent", "percentage", "share", "sharePct",
                "revenue_pct", "revenueShare", "contribution", "weight",
                "value"),
    },
    # Only distinct WORDS belong here now — casing and underscores are
    # handled by _key_norm, so `metric_name` matches `metricName` without an
    # entry of its own.
    "company_metrics": {
        "label": ("label", "name", "metric", "metricName", "kpi", "title",
                  "indicator", "measureName"),
        "value": ("value", "amount", "number", "figure", "val", "count"),
        "unit": ("unit", "units", "uom", "measure", "currency"),
    },
    "financial_summary": {
        "fiscalYear": ("fiscalYear", "year", "financial_year", "fy",
                       "period"),
        "revenue": ("revenue", "revenue_m", "totalRevenue", "sales"),
        "grossMarginPct": ("grossMarginPct", "gross_margin_pct",
                           "grossMargin", "margin_pct"),
        # Growth is its own field now. It used to have nowhere to land, so a
        # row's growth figure was dropped by the validator and separately
        # back-filled from `pat` by `_fin_row_to_spec` — two halves of one
        # defect, each invisible from the other's side.
        "growthPct": ("growthPct", "growth_pct", "yoyRevenueGrowthPct",
                      "yoy_revenue_growth_pct", "revenueGrowthPct"),
        "ebitda": ("ebitda", "ebitda_m", "operatingProfit"),
        "pat": ("pat", "pat_m", "profitAfterTax", "netProfit", "netIncome"),
        "isEstimate": ("isEstimate", "is_estimate", "estimate", "projected"),
        "ccy": ("ccy", "currency", "currencyCode"),
    },
}

# Numbers arrive dressed for reading: "45%", "1,250", "Rs 7,761 crore",
# "12.5 %". The validator's coercers take a bare number.
_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _loose_number(value):
    """Pull a number out of a human-formatted string, or return it as-is.

    THE BUG THIS FIXES
    ------------------
    Magnitude suffixes were matched as bare SUBSTRINGS anywhere in the text,
    with "m" (a million) last in the list. So any string containing the
    letter m multiplied its number by a million, and any string containing
    "cr" multiplied it by ten million:

        "45% of sales from enterprise"   -> 45,000,000   ("m" in "from")
        "3 acres of warehousing"         ->  30,000,000  ("cr" in "acres")
        "Customer segments (B2B ... )"   ->  2,000,000   ("2" from B2B, m from "segments")

    The last of those reached the log on 17 Aug as
    `pct: 2000000.0 — Enter a percentage 0-100`. The percentage validator
    caught it because it has a range; `company_metrics` has none, so the same
    corruption there would have been stored silently.

    A magnitude word now has to be a WORD, and has to follow the number it
    scales — "250 mn" scales, "segments" does not. A percentage never scales:
    nothing is measured in millions of percent.
    """
    if value is None or isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    normalised = text.replace("\u2212", "-")
    match = _NUM_RE.search(normalised)
    if not match:
        return None
    try:
        number = float(match.group(0).replace(",", ""))
    except ValueError:
        return None

    # A percentage is never in millions.
    tail = normalised[match.end():]
    if tail.lstrip().startswith("%") or "percent" in tail.lower()[:12]:
        return number

    # The magnitude must be a whole word, and must FOLLOW the number within a
    # short window — long enough for "7,761 crore" and "250 mn", short enough
    # that prose further along the sentence cannot reach back and scale it.
    window = tail[:14].lower()
    for token, factor in (("crore", 1e7), ("cr", 1e7), ("lakh", 1e5),
                          ("billion", 1e9), ("bn", 1e9),
                          ("million", 1e6), ("mn", 1e6), ("m", 1e6)):
        if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", window):
            return number * factor
    return number
def _key_norm(name):
    """Case- and separator-insensitive key identity.

    THE BUG THIS FIXES
    ------------------
    The alias table listed `metricName` but not `metric_name`, and the model
    emits both across runs — so `company_metrics` logged `kept=0 dropped=5,
    no recognisable label in ['metric_name', 'source', 'value']` on live runs
    while an alias that differs only in an underscore sat in the table.

    Listing every spelling is chasing a moving target one key at a time. The
    model's casing convention is not information: `metric_name`, `metricName`
    and `MetricName` mean the same thing, so they are compared as the same
    thing. Only genuinely different WORDS need an alias entry now.
    """
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _match_alias(row, candidates):
    """First candidate present in `row`, matched on normalised key identity."""
    # Exact hits first — cheapest, and preserves the table's ordering intent.
    for candidate in candidates:
        if candidate in row and row[candidate] not in (None, ""):
            return row[candidate]
    wanted = {_key_norm(c) for c in candidates}
    for key, value in row.items():
        if _key_norm(key) in wanted and value not in (None, ""):
            return value
    return None


def _fiscal_year_label(value):
    """Squeeze a model's fiscal-year phrasing into the form's 9-char field.

    THE BUG THIS FIXES
    ------------------
    `financial_summary.fiscalYear` is `max_len=9`, and the deep extract passed
    the model's label through untouched. The model writes "FY2024-25
    (Consolidated)", "Financial Year 2024-25", "March 2025" — all longer than
    nine characters. `validate_structured_form` raises on the FIRST bad row,
    and the caller wrapped the whole batch in one try/except, so one verbose
    label discarded EVERY year of financials with the log line
    "deep-extract financials skipped: Keep this under 9 characters."

    Nothing in that message says which field, which row, or which value.

    The label is presentational — the year is the information — so it is
    normalised rather than rejected. Returns "" when no year can be found,
    which the caller treats as a droppable row rather than a fatal one.
    """
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if len(text) <= 9:
        return text

    # "FY2024-25", "FY 2024-25", "Financial Year 2024-25" -> "FY2024-25"
    span = re.search(r"(\d{4})\s*[-/–]\s*(\d{2,4})", text)
    if span:
        start, end = span.group(1), span.group(2)
        return f"FY{start}-{end[-2:]}"

    # "FY2024", "March 2025", "2024 (Audited)" -> "FY2024"
    single = re.search(r"(19|20)\d{2}", text)
    if single:
        year = single.group(0)
        return f"FY{year}" if "fy" in text.lower() or len(text) > 4 else year

    # A two-digit fiscal shorthand: "FY26", "FY 25".
    short = re.search(r"fy\s*'?(\d{2})\b", text, re.I)
    if short:
        return f"FY{short.group(1)}"

    # No year anywhere in it. Truncating to nine characters would coin a
    # label like "unknown p" and store it as a fiscal year — a row with no
    # year is not a financial year row, so it is dropped by the caller.
    return ""


def _records_items_to_api(section_key, items):
    """Normalise AI-returned form items to the API field names.

    Maps the model's key names onto the form's, pulls numbers out of
    formatted strings, and DROPS rows it cannot rescue rather than letting
    one bad row discard the batch. Returns (rows, dropped_reasons).
    """
    aliases = _FORM_FIELD_ALIASES.get(section_key)
    if not aliases:
        return list(items or []), []

    numeric = {"pct", "value", "revenue", "ebitda", "grossMarginPct"}
    out, dropped = [], []
    for index, row in enumerate(items or []):
        if not isinstance(row, dict):
            # A bare string ("Retail 45%") is the commonest shape the model
            # falls back to. Salvage it rather than discarding the row.
            if isinstance(row, str) and "label" in aliases:
                number = _loose_number(row)
                text = _NUM_RE.sub("", row).strip(" :-–%()")
                if text:
                    salvaged = {"label": text[:120]}
                    for field in aliases:
                        if field in numeric and number is not None:
                            salvaged[field] = number
                            break
                    out.append(salvaged)
                    continue
            dropped.append(f"row {index}: not an object ({type(row).__name__})")
            continue
        mapped = {}
        for api_field, candidates in aliases.items():
            value = _match_alias(row, candidates)
            if value is not None:
                mapped[api_field] = (_loose_number(value)
                                     if api_field in numeric else value)
        if not str(mapped.get("label") or "").strip() and "label" in aliases:
            # A row carrying fiscal-year financials landed in a metrics form.
            # It has no label because it is not a metric — saying "no
            # recognisable label" sends the reader looking for a naming bug
            # that isn't there.
            if {"fiscalyear", "financialyear", "fy"} & {
                    _key_norm(k) for k in row}:
                dropped.append(
                    f"row {index}: this is a fiscal-year financial row, not a "
                    f"{section_key} row — the model returned the wrong shape "
                    f"for this section")
            else:
                dropped.append(f"row {index}: no recognisable label in "
                               f"{sorted(row)[:6]}")
            continue
        out.append(mapped)

    if section_key == "revenue_model" and out:
        total = sum(float(r.get("pct") or 0) for r in out)
        priced = [r for r in out if float(r.get("pct") or 0) > 0]
        if not priced:
            # Every row is a LABEL with no percentage — the model described
            # the revenue model instead of splitting it ("subscription",
            # "transaction fees"). That is a reasonable answer to a badly
            # aimed question, not a broken split.
            #
            # v27.3: these rows are now KEPT as an unweighted draft rather
            # than discarded. Two runs on 17 Aug each paid for this call and
            # threw away four correct stream names, leaving the section
            # blank with no on-screen explanation. A named stream with no
            # percentage is still information; the user supplies the split,
            # which is the one part of this they actually know.
            for row in out:
                row.pop("pct", None)
            return out, [f"{len(out)} row(s) named a revenue stream with no "
                         f"percentage split — seeded as an unweighted draft "
                         f"for the user to complete"]
        if len(priced) < len(out):
            dropped.append(f"{len(out) - len(priced)} row(s) had no "
                           f"percentage and were left out of the split")
            out = priced
            total = sum(float(r.get("pct") or 0) for r in out)
        # A split the model rounded to 99 or 101 is not a data error worth
        # discarding four rows over; the validator allows 0.5 tolerance.
        if len(out) > 1 and 50 < total < 150 and abs(total - 100.0) > 0.5:
            for row in out:
                row["pct"] = round(float(row.get("pct") or 0) * 100.0 / total, 2)
    return out, dropped


def _generate_section(profile, cfg, payloads, user=None):
    from fundos.llm import guardrail
    from fundos.llm.adapter import llm_generate

    context = {
        "company": {
            "name": profile.company.name,
            "website": profile.website_url,
            "hqCountry": profile.hq_country,
            "homeCurrency": profile.home_currency,
        },
        "section_key": cfg.section_key,
        "section_label": cfg.label,
        "website_extract": payloads.get("website", {}),
        "document_extracts": payloads.get("documents", []),
        "research": payloads.get("research", {}),
        "founders": payloads.get("founders", []),
        # Conclusions from the Judgement tier, when one has run. Costs a few
        # hundred tokens and stops a cheap Simple call contradicting the
        # adjudication the expensive call already paid for.
        "judgment": judgment_context(profile),
    }

    result = llm_generate(
        role=cfg.llm_role,
        context=context,
        deal_id=None,
        user=user,
        calling_context=f"profile.generate.{cfg.section_key}",
        section_context={"section_key": cfg.section_key,
                         "section_label": cfg.label},
        config_profile=_section_config_profile(cfg.section_key),
    )
    result = guardrail.apply(cfg.llm_role, result)

    # Primary path: the adapter has already normalized provider shape drift
    # into `content`. This is a defensive second pass for any alternate key
    # that slipped through, so a section is never silently saved empty when
    # the model clearly returned prose (LLM_ISSUE2).
    content = (result.get("content") or result.get("summary")
               or result.get("text") or result.get("body")
               or result.get("narrative") or "")
    if not content:
        for v in result.values():
            if isinstance(v, str) and len(v.strip()) > 40:
                content = v
                break
    update_section(
        profile, cfg.section_key,
        content=content,
        user=user, by_ai=True, source="ai_research",
        needs_input=bool(result.get("needsInput")) or not content,
        missing=result.get("missing", []),
        change_summary="Generated from company sources")


def _mark_needs_input(profile, section_key):
    from fundos.profile.models import ProfileSection
    ProfileSection.objects.filter(
        profile=profile, section_key=section_key, is_active=True
    ).update(needs_input=True)


def regenerate_section(profile, section_key, user=None, force=False):
    """Regenerate one section. Refuses to discard a human edit unless forced.

    ``section_key`` arrives from the URL as a WIRE name; rows and section
    config are keyed by STORAGE name. Resolving it here is what stops a
    regenerate of `company_profile` or `news` reporting "not regenerable"
    purely because the caller used the name the API told them to use.
    """
    from fundos.platformcfg.services import profile_sections
    from fundos.profile.models import ProfileSection
    from fundos.profile.spec_serializer import storage_key_for

    section_key = storage_key_for(section_key)
    section = ProfileSection.objects.filter(
        profile=profile, section_key=section_key, is_active=True).first()
    if section and section.edited_by_user and not force:
        return {"status": "confirm_required",
                "message": "This section has been edited. Regenerating will "
                           "replace your text — the previous version stays in "
                           "history."}

    cfg = next((c for c in profile_sections() if c.section_key == section_key),
               None)
    if not cfg or not cfg.llm_role:
        return {"status": "not_regenerable"}

    payloads, _ = collect_sources(profile, user=user)

    # Dispatch by what the section STORES, not uniformly to the prose
    # generator. Sending a structured section through `_generate_section`
    # writes a prose summary into `content` and leaves `structured` — and
    # therefore the section's `data` — empty. That is the Gap2 defect
    # ("populated in content but empty in sections.data") reappearing on the
    # regenerate path, where it was less visible because only one section is
    # affected at a time.
    if section_key in STRUCTURED_GEN_SECTIONS:
        _generate_structured_section(profile, cfg, payloads, user=user)
    elif section_key in STRUCTURED_NARRATIVE_SECTIONS:
        _generate_structured_narrative(profile, cfg, payloads, user=user)
    else:
        _generate_section(profile, cfg, payloads, user=user)
    return {"status": "ok"}


def update_structured_form(profile, section_key, items_payload, user=None):
    """Validate and store a numeric/structured form (Enhancement set).

    Revenue Model / Company Metrics / Financial Summary keep their data in
    ProfileSection.structured. Validation (e.g. revenue shares totalling
    100%) happens in records.validate_structured_form before anything is
    written; the existing update_section path snapshots the prior version,
    so form edits get the same history and rollback as prose sections.

    Tester Issue #1 (25-Jul enhancement round): the valuation engine reads
    ARR/revenue from the deal-scoped CKB, but founders enter those figures
    in the Company Profile's Financial Summary / Company Metrics forms. The
    two stores never reconciled, so a founder who had filled Financial
    Summary was still told to "open the knowledge base". We now mirror the
    financial forms into the CKB on save (same mechanism as deal targets),
    so the number the founder enters in Financial Summary is the number the
    valuation uses — and the guidance can point them to Financial Summary.
    """
    from fundos.profile.records import validate_structured_form
    from fundos.profile.spec_serializer import storage_key_for

    # Wire name -> storage name. A no-op for the three structured forms, whose
    # names are the same either way; done anyway so this does not become the
    # one write path that breaks the next time a section is renamed.
    section_key = storage_key_for(section_key)
    normalised = validate_structured_form(section_key, items_payload)
    section = update_section(
        profile, section_key,
        structured=normalised,
        user=user,
        change_summary="Structured form updated")

    if section_key in ("financial_summary", "company_metrics"):
        try:
            # Its OWN savepoint. Without one, a statement that failed earlier
            # in the surrounding atomic block leaves the transaction aborted,
            # and every query this makes dies with "You can't execute queries
            # until the end of the 'atomic' block" — which reads as a fault
            # in the sync when the sync is merely the next thing to touch the
            # database. The savepoint also means a genuine failure here rolls
            # back only the sync, leaving the section save intact.
            with transaction.atomic():
                _sync_financials_to_ckb(profile, section_key, normalised,
                                        user=user)
        except Exception as e:      # never fail the save on a sync hiccup
            logger.warning("PROFILE: financials→CKB sync failed for %s: %s",
                           section_key, e)
    return section


def _isolated_write(what, *, level="warning"):
    """Context manager: run an ORM write in its own savepoint, swallow failure.

    v27.5 — THE FIX FOR THE ERROR THAT SURVIVED THREE RELEASES.

        PROFILE: financials→CKB sync failed for financial_summary:
        An error occurred in the current transaction. You can't execute
        queries until the end of the 'atomic' block.

    That message has appeared on every run since v27.1, and it was chased in
    the wrong place each time. It is Django's `TransactionManagementError`,
    raised when `connection.needs_rollback` is set -- which happens when a
    statement fails inside an atomic block and the exception is caught rather
    than allowed to unwind. Django then refuses every subsequent query until
    the block exits.

    So the sync was never the fault. It was simply the next thing to touch
    the database after something else had already poisoned the transaction.
    v27.3 correctly added savepoints around the funding and investor writes,
    and the error persisted because the pattern was still present in five
    other places -- including `profile.save(update_fields=["judgment"])`,
    which runs immediately before the sync and matches the log's timing to
    the second.

    The pattern that breaks it:

        with transaction.atomic():          # outer
            try:
                Model.objects.create(...)   # fails
            except Exception:
                log(...)                    # needs_rollback stays SET
            OtherModel.objects.get(...)     # dies here, blamed here

    The savepoint is what makes the swallow legal: rolling back to it clears
    `needs_rollback`, so the surrounding transaction survives intact and the
    error is attributed to the statement that actually failed.

    `_isolated()` already existed for one call site. This is the general form,
    used everywhere the codebase says "best effort" about a write.
    """
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        try:
            with transaction.atomic():
                yield
        except Exception as e:
            getattr(logger, level)("PROFILE: %s skipped: %s", what, e)
    return _cm()


def _sync_financials_to_ckb(profile, section_key, normalised, user=None):
    """Mirror Financial Summary / Company Metrics into the CKB.

    Downstream engines (valuation, readiness, investor matching) read the
    CKB, so writing these through keeps a figure entered in the Company
    Profile indistinguishable from one entered directly in the CKB at the
    point of use. Only fills the CKB fields the engines actually consume:
      * financial_summary → revenue (latest fiscal year with a revenue), and
        ebitda / gross_margin_pct for that year.
      * company_metrics   → arr / mrr when a metric row is labelled as such.
    Values are written as founder-sourced and verified.
    """
    from fundos.core.services import ckb_service

    deal = _company_default_deal(profile.company, user=user)
    if deal is None:
        return
    items = [r for r in ((normalised or {}).get("items") or [])
             if isinstance(r, dict)]
    writes = {}
    ccy = ""

    if section_key == "financial_summary":
        # ACTUALS ONLY. The valuation engine reads CKB `revenue` as the
        # company's revenue, not as its plan. A live profile carried eleven
        # fiscal rows running to FY30, four of them flagged `is_estimate`, and
        # this function's "most recent year with a revenue" selected the FY30
        # projection — so a founder saving the Financial Summary form pushed a
        # forecast into the field a valuation is computed from. A forecast is a
        # legitimate thing to store and an illegitimate thing to value.
        actuals = [r for r in items
                   if r.get("revenue") is not None
                   and not _is_estimate_row(r)]
        dropped = len(
            [r for r in items if r.get("revenue") is not None]) - len(actuals)
        if actuals:
            latest = sorted(actuals, key=_fy_key)[-1]
            # Values pass through in the units they were entered in. The
            # Financial Summary form holds ABSOLUTE amounts denominated by the
            # row's `ccy` — that is the established contract
            # (`test_issue28_fixes` enters 8,000,000 INR and expects
            # 8,000,000) — and the CKB stores a (value, ccy, basis) triple, so
            # the denomination travels with the figure instead of needing to be
            # normalised away. Converting here would both break that contract
            # and duplicate what `ccy` already expresses.
            writes["revenue"] = latest.get("revenue")
            if latest.get("ebitda") is not None:
                writes["ebitda"] = latest.get("ebitda")
            if latest.get("pat") is not None:
                writes["pat"] = latest.get("pat")
            # A margin is a ratio, so it carries no unit and no currency.
            if latest.get("grossMarginPct") is not None:
                writes["gross_margin"] = latest.get("grossMarginPct")
            ccy = str(latest.get("ccy") or "").strip().upper()
            logger.info(
                "PROFILE: syncing %s actuals to the CKB for deal %s in %s "
                "(%s row(s) excluded as estimates)",
                latest.get("fiscalYear") or "?", deal.id, ccy or "unstated",
                dropped)
        elif dropped:
            # Saying nothing here is what made the previous behaviour hard to
            # see: the CKB simply held a number, and nothing recorded that
            # every row behind it was a projection.
            logger.warning(
                "PROFILE: financial summary for company %s carries only "
                "estimated years — nothing was synced to the CKB, because a "
                "projection must not be read as revenue.", profile.company_id)
    elif section_key == "company_metrics":
        # Map common KPI labels to CKB keys.
        alias = {"arr": "arr", "annual recurring revenue": "arr",
                 "mrr": "mrr", "monthly recurring revenue": "mrr",
                 "revenue": "revenue"}
        for row in items:
            label = str(row.get("label") or "").strip().lower()
            key = alias.get(label)
            if not key or row.get("value") is None:
                continue
            writes[key] = row.get("value")
            # A metric's denomination lives in its free-text `unit`. Reading a
            # currency out of it is the difference between storing "12000000"
            # and storing "12000000 INR", and only the second is a figure
            # anything downstream can use without assuming.
            ccy = ccy or _ccy_from_unit(row.get("unit"))
            # A SCALE word in the unit is a different matter, and is refused
            # rather than guessed. "ARR = 12, unit = INR crore" means
            # ₹120,000,000; taken at face value it is twelve rupees. The CKB
            # feeds the valuation and nothing downstream can detect a magnitude
            # that is wrong by seven orders, so a metric whose scale is stated
            # in prose is reported and skipped — the founder can enter it in
            # Financial Summary, which has a defined unit.
            scale = _stated_scale(row.get("unit"))
            if scale:
                writes.pop(key, None)
                logger.warning(
                    "PROFILE: metric %r was not synced to the CKB — its unit "
                    "%r states a scale (%s), so the figure as written is "
                    "ambiguous by a factor of %d. Enter it in Financial "
                    "Summary, which has a defined unit.",
                    row.get("label"), row.get("unit"), scale[0], scale[1])

    # The currency travels WITH the figure. Every money field in this system is
    # a triple (value, ccy, basis) precisely so a number cannot be read in the
    # wrong denomination, and this path was writing the value alone. With
    # nothing stated, fall back to the company's own currency of record — that
    # is what a founder typing into their own profile means.
    ccy = ccy or (profile.home_currency or "").strip().upper() or ""

    for field_key, value in writes.items():
        if value is None:
            continue
        # Per field, not per sync: one figure the CKB's numeric rules reject
        # must not cost the others. Before this, a single out-of-range value
        # raised out of the loop and the fields after it in iteration order
        # were silently never written.
        with _isolated(f"CKB sync of {field_key}"):
            ckb_service.set_field(
                deal, field_key, value, ccy=ccy, basis="actual", user=user,
                source="founder",
                reason=f"synced from profile.{section_key}")


def _is_estimate_row(row):
    """True when a financial row describes a projection rather than an actual.

    Reads the explicit flag first, then falls back to how the year is labelled.
    Models and spreadsheets both mark a forecast in the label — "FY27E",
    "FY 2028 (P)", "FY29 projected" — and a row that says so in its own text
    while carrying no flag is still a projection.
    """
    for key in ("isEstimate", "is_estimate", "estimate", "projected"):
        if key in row:
            value = row.get(key)
            if isinstance(value, bool):
                if value:
                    return True
            elif str(value or "").strip().lower() in ("true", "1", "yes", "y"):
                return True
    label = str(row.get("fiscalYear") or row.get("financial_year")
                or row.get("year") or "")
    return bool(re.search(r"(?:\d\s*)(?:e|p|f)\b|estimat|project|forecast|budget",
                          label, re.IGNORECASE))


def _fy_key(row):
    """Sortable year for a fiscal-year label, normalised to four digits.

    ``"".join(digits)`` alone reads ``FY24`` as 24 and ``FY 2024`` as 2024, so
    a single row written the long way sorts above every other year regardless
    of when it actually is — and this function picks the *last* row. Mixed
    conventions in one table are normal (a model exports "FY 2024", a founder
    types "FY24"), so the normalisation belongs here rather than in a
    convention nobody can enforce.

    A span like ``FY2024-25`` takes the opening year, which is how a fiscal
    year is named.
    """
    label = str(row.get("fiscalYear") or row.get("financial_year")
                or row.get("year") or "")
    years = re.findall(r"\d{2,4}", label)
    if not years:
        return -1
    first = years[0]
    if len(first) <= 2:
        # Two digits: 90-99 are 1990s, everything else this century. A profile
        # with 1990s financials and one with 2090s financials are not both
        # plausible.
        value = int(first)
        return 1900 + value if value >= 90 else 2000 + value
    return int(first[:4])


def _ccy_from_unit(unit):
    """An ISO currency code embedded in a metric's unit string, else ``""``."""
    text = str(unit or "").strip().upper()
    match = re.search(r"\b(INR|USD|EUR|GBP|SGD|AED|JPY|AUD|CAD)\b", text)
    if match:
        return match.group(1)
    if "₹" in str(unit or "") or re.search(r"\bRS\b|\bRUPEE", text):
        return "INR"
    if "$" in str(unit or ""):
        return "USD"
    return ""


# Scale words a metric's free-text `unit` may carry, and what each multiplies
# by. Used only to DETECT an ambiguous unit, never to convert one.
_UNIT_SCALES = (
    ("crore", r"\bcrores?\b|\bcr\b", 10_000_000),
    ("lakh", r"\blakhs?\b|\blacs?\b", 100_000),
    ("billion", r"\bbillions?\b|\bbn\b", 1_000_000_000),
    ("million", r"\bmillions?\b|\bmn\b|\bmm\b", 1_000_000),
    ("thousand", r"\bthousands?\b", 1_000),
)


def _stated_scale(unit):
    """``(word, multiplier)`` when a unit states a scale, else ``None``.

    A metric's value is stored as typed, so a unit that says "crore" means the
    stored number is smaller than the figure by a factor of ten million. That
    is not something to correct silently — the founder may have typed either —
    but it IS something the CKB must not accept, because a magnitude error
    there becomes a valuation error and nothing downstream can detect it.
    """
    text = str(unit or "")
    for word, pattern, multiplier in _UNIT_SCALES:
        if re.search(pattern, text, re.IGNORECASE):
            return word, multiplier
    return None


def _company_default_deal(company, user=None):
    """The company's working/default deal, or None if not resolvable."""
    from fundos.core.models import Deal
    return (Deal.objects.filter(company=company, is_deleted=False)
            .order_by("created_at").first())


def regenerate_field(profile, section_key, field_key, user=None):
    """Regenerate a single field/row of a structured section with AI.

    The enhancement set explicitly asks for *field-level* regeneration, not
    just whole-section. This calls the dedicated `company_profile_field`
    role with the section, the field and the current structured context,
    and merges the returned value back into the one field — leaving the
    rest of the founder's data untouched. Everything else about a section
    (history, provenance) is preserved because the merged result still goes
    through update_structured_form / update_section.
    """
    from fundos.llm import guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.profile.models import ProfileSection
    from fundos.profile.spec_serializer import storage_key_for

    section_key = storage_key_for(section_key)
    section = ProfileSection.objects.filter(
        profile=profile, section_key=section_key, is_active=True).first()
    if section is None:
        # A field can be regenerated before the founder has saved anything,
        # so materialise the section row if the config knows it. Only an
        # unknown section key is a genuine not_found.
        from fundos.platformcfg.services import profile_sections
        known = any(c.section_key == section_key for c in profile_sections())
        if not known:
            return {"status": "not_found"}
        ensure_sections(profile)
        section = ProfileSection.objects.filter(
            profile=profile, section_key=section_key, is_active=True).first()
    if section is None:
        return {"status": "not_found"}

    context = {
        "company": {"name": profile.company.name,
                    "website": profile.website_url,
                    "hqCountry": profile.hq_country,
                    "homeCurrency": profile.home_currency},
        "section_key": section_key,
        "field_key": field_key,
        "current": section.structured or {},
    }
    try:
        result = llm_generate(
            role="company_profile_field",
            context=context, deal_id=None, user=user,
            calling_context=f"profile.field.{section_key}.{field_key}",
            section_context={"section_key": section_key,
                             "field_key": field_key})
        result = guardrail.apply("company_profile_field", result)
    except Exception as e:      # never break the form on an AI hiccup
        logger.warning("PROFILE: field regen %s/%s failed: %s",
                       section_key, field_key, e)
        return {"status": "unavailable",
                "message": "AI is unavailable right now. Enter the value "
                           "manually, or try again shortly."}

    value = result.get("value")
    if value is None:
        value = result.get("content") or result.get("summary") or ""
    return {"status": "ok", "fieldKey": field_key, "value": value}


# --------------------------------------------------------------------------
# Cash position & runway (PRD §6 Step 2 / C5)
# --------------------------------------------------------------------------
def compute_cash_position(company, *, cash_value, cash_ccy, burn_value,
                          burn_ccy, as_of_date=None, deal_id=None,
                          desired_runway_months=None, user=None):
    """Deterministic runway maths. Never an LLM path.

    C5: burn is an input, may be zero or negative (profitable company). In
    that case runway is unbounded and the exhaustion date is undefined —
    reported honestly rather than as a very large number.
    """
    from fundos.platformcfg.services import to_usd
    from fundos.profile.models import CashPosition

    # as_of_date arrives as an ISO string over the API and as a date object
    # from internal callers — normalise before any date arithmetic.
    if isinstance(as_of_date, str):
        try:
            as_of_date = date.fromisoformat(as_of_date[:10])
        except ValueError:
            as_of_date = None
    as_of_date = as_of_date or timezone.now().date()

    cash_usd, cash_rate, cash_fx_date, _ = to_usd(cash_value, cash_ccy)
    burn_usd, _, _, _ = to_usd(burn_value, burn_ccy)

    is_profitable = burn_usd is not None and burn_usd <= 0
    runway_months = None
    exhaustion = None
    additional_required = None

    if not is_profitable and burn_usd and burn_usd > 0 and cash_usd is not None:
        runway_months = Decimal(str(cash_usd)) / Decimal(str(burn_usd))
        exhaustion = as_of_date + timedelta(days=float(runway_months) * 30.44)
        if desired_runway_months:
            shortfall_months = (Decimal(str(desired_runway_months))
                                - runway_months)
            if shortfall_months > 0:
                additional_required = shortfall_months * Decimal(str(burn_usd))
            else:
                additional_required = Decimal("0")

    CashPosition.objects.filter(company=company, deal_id=deal_id,
                                is_active=True).update(is_active=False)
    row = CashPosition.objects.create(
        tenant_id=company.tenant_id, company=company, deal_id=deal_id,
        cash_in_bank_value=cash_value, cash_in_bank_ccy=cash_ccy or "USD",
        monthly_burn_value=burn_value, monthly_burn_ccy=burn_ccy or "USD",
        as_of_date=as_of_date, is_profitable=is_profitable,
        runway_months=(round(runway_months, 2) if runway_months is not None
                       else None),
        cash_exhaustion_date=exhaustion,
        additional_capital_required_usd=additional_required,
        fx_rate_used=cash_rate, fx_as_of=cash_fx_date,
        created_by=user)
    return row


# --------------------------------------------------------------------------
# Traction signals → fund-raise bucket (C9)
# --------------------------------------------------------------------------
def traction_signals(company, profile=None):
    """Assemble the signals the traction rules evaluate."""
    from fundos.core.models import CkbField, Deal
    from fundos.platformcfg.services import to_usd
    from fundos.profile.models import Founder, FundingRound

    signals = {}
    deal_ids = list(Deal.objects.filter(company=company)
                    .values_list("id", flat=True))

    def ckb(key):
        if not deal_ids:
            return None
        row = CkbField.objects.filter(deal_id__in=deal_ids,
                                      field_key=key).first()
        return row.value if row else None

    home_ccy = (profile.home_currency if profile else None) or "USD"

    arr = ckb("arr")
    if arr is not None:
        usd, _, _, _ = to_usd(arr, home_ccy)
        signals["arr_usd"] = float(usd) if usd is not None else None

    customers = ckb("customers")
    if customers is not None:
        try:
            signals["paying_customers"] = float(customers)
        except (TypeError, ValueError):
            pass

    signals["company_registered"] = bool(ckb("incorporation"))

    if profile:
        raised = FundingRound.objects.filter(profile=profile)
        total = Decimal("0")
        for row in raised:
            usd, _, _, _ = to_usd(row.amount_value, row.amount_ccy)
            if usd:
                total += usd
        signals["capital_raised_usd"] = float(total)

        founders = list(Founder.objects.filter(profile=profile))
        signals["founder_full_time"] = any(f.is_full_time for f in founders)
        months = [f.months_full_time for f in founders if f.months_full_time]
        signals["months_full_time"] = max(months) if months else 0

    star = ckb("star_founder")
    if star:
        signals["star_founder"] = bool(star)

    return signals


def recommend_fundraise(company, profile=None):
    """Default bucket plus the reasoning, per C9."""
    from fundos.platformcfg.services import recommend_bucket

    signals = traction_signals(company, profile)
    result = recommend_bucket(signals)
    if not result:
        return None
    bucket = result["bucket"]
    return {
        "bucketNo": bucket.bucket_no,
        "label": bucket.label,
        "amountUsd": float(bucket.amount_usd),
        "typicalStage": bucket.typical_stage,
        "investorTypes": bucket.investor_types,
        "dilutionLowPct": float(bucket.typical_dilution_low_pct),
        "dilutionHighPct": float(bucket.typical_dilution_high_pct),
        "rationale": result["rationale"],
        "advice": result["advice"],
        "upliftApplied": result["uplift_applied"],
        "signals": signals,
        # When True the bucket is a placeholder, not advice — the UI must
        # ask for the missing inputs rather than present a target.
        "insufficientData": result.get("insufficient_data", False),
        "missingSignals": result.get("missing_signals", []),
        "isDefault": not result.get("insufficient_data", False),
    }


# --------------------------------------------------------------------------
# Manual deal targets (C6)
# --------------------------------------------------------------------------
def get_deal_targets(deal_id):
    """The active targets row for a deal, or None."""
    from fundos.profile.targets import DealTargets

    return DealTargets.objects.filter(deal_id=deal_id, is_active=True,
                                      is_deleted=False).first()


def save_deal_targets(company, deal_id, *, target_raise=None,
                      pre_money=None, ccy=None, notes="", user=None,
                      source="founder"):
    """Record founder-entered headline terms.

    Only the two INPUTS are accepted. Post-money and dilution are derived
    on save and cannot be supplied by a caller — if a client sends them
    they are ignored, so the calculated pair can never disagree with the
    inputs it is derived from.

    Supersedes rather than edits: the prior row is deactivated and a new
    one written, so a change of terms leaves a trail.
    """
    from fundos.profile.targets import DealTargets

    ccy = (ccy or "USD").upper()

    previous = get_deal_targets(deal_id)
    if previous:
        # Carry forward whichever input this call does not restate, so a
        # partial update doesn't silently blank the other field.
        if target_raise is None:
            target_raise = previous.target_raise_value
        if pre_money is None:
            pre_money = previous.pre_money_value
        previous.is_active = False
        previous.save(update_fields=["is_active", "updated_at"])

    row = DealTargets(
        tenant_id=company.tenant_id,
        deal_id=deal_id,
        company=company,
        target_raise_value=target_raise,
        target_raise_ccy=ccy,
        pre_money_value=pre_money,
        pre_money_ccy=ccy,
        notes=notes or "",
        source=source,
        entered_by=user,
        created_by=user,
    )
    row.save()   # recalculate() + convert_to_usd() run here

    _sync_targets_to_ckb(company, deal_id, row, user=user)
    return row


def _sync_targets_to_ckb(company, deal_id, row, user=None):
    """Mirror the targets into the knowledge base.

    Downstream consumers — investor matching, materials, the stage-state
    engine — read the CKB, so writing these through keeps a manually
    entered raise indistinguishable from a strategy-derived one at the
    point of use. USD values are used so comparison is currency-neutral.
    """
    from fundos.core.models import CkbField, CompanyKnowledgeBase

    if not row.is_complete:
        return

    ckb, _ = CompanyKnowledgeBase.objects.get_or_create(
        deal_id=deal_id, is_deleted=False,
        defaults={"tenant_id": company.tenant_id})

    values = {
        "target_raise_usd": row.target_raise_usd,
        "pre_money_valuation_usd": row.pre_money_usd,
        "post_money_valuation_usd": row.post_money_usd,
        "expected_dilution_pct": row.dilution_pct,
        # Retained under its former key so anything already reading it
        # keeps working; post-money is the meaningful headline figure.
        "target_valuation_usd": row.post_money_usd,
    }

    for field_key, value in values.items():
        if value is None:
            continue
        field, created = CkbField.objects.get_or_create(
            ckb=ckb, field_key=field_key, is_deleted=False,
            defaults={
                "tenant_id": company.tenant_id,
                "deal_id": deal_id,
                "group_key": "financial",
                "value_num": value,
                "source": "founder",
                "fact_or_inference": "fact",
                "verified": True,
                "created_by": user,
            })
        if not created:
            field.value_num = value
            field.source = "founder"
            field.verified = True
            field.save(update_fields=["value_num", "source", "verified",
                                      "updated_at"])


# --------------------------------------------------------------------------
# THE JUDGEMENT STAGE — CURRENTLY UNREACHABLE. Read this before using it.
#
# A second pass in which a stronger model reasoned over the first pass's
# extraction to adjudicate conflicting figures and write the investment
# thesis. Nothing calls it: the pipeline's single synthesis call now produces
# `investment_thesis` directly from the dossier, and the pipeline is pinned to
# one model throughout, which is the opposite of what a second-opinion stage
# on a frontier model is for.
#
# Kept, not deleted, because whether a profile should get an independent
# adjudication pass over disagreeing sources is a product question rather than
# a defect — and everything it needs (`company_profile_judgment`, the
# `tier.judgment` config profile, its prompt and its tests) is still in place,
# so reinstating it is wiring one call back in. Deleting it would throw that
# away to save nothing at runtime.
#
# Its tests below still pass and still assert real behaviour, but they cover a
# path no generation takes. Do not read them as coverage of a live feature.
#
# Numbers rule, which the live pipeline still observes: the model returns ONLY
# raw sourced figures (revenue, valuations, deal values) for founder
# verification. Every derived ratio — EV/Revenue, YoY growth, implied
# multiples — is computed in `compute_profile_ratios`, never by the model.
# --------------------------------------------------------------------------
def _judgment_stage_enabled():
    """Feature flag for the two-stage dossier (default ON).

    OFF means stage 1's own investment_thesis block is used as-is, exactly
    as before this feature existed — so disabling it is a clean rollback.
    """
    try:
        from fundos.config.feature_flags import get_flag
        return bool(get_flag("PROFILE_JUDGMENT_STAGE", True))
    except Exception:
        return True


# Keys from stage 1 that stage 2 needs in order to reason. Deliberately
# EXCLUDES website_extract, document_extracts and research — sending the raw
# corpus here would defeat the entire point, which is that this call is small
# enough to afford a frontier model.
_JUDGMENT_INPUT_KEYS = (
    "funding_and_valuation", "cap_table_and_investors", "financials",
    "leadership", "business_model", "industry_and_market", "competitors",
    "conflicts", "missing", "story_and_usp",
)


def _apply_judgment_stage(profile, extraction, user=None):
    """Second pass: a stronger model reasons over stage 1's extraction.

    Merges the result back into the extraction dict so every downstream
    consumer (record fan-out, section writes) is unchanged. Never raises —
    a failed judgement stage leaves stage 1's own thesis in place, which is
    a degraded but complete profile rather than no profile.
    """
    if not _judgment_stage_enabled():
        return extraction

    from fundos.llm import guardrail
    from fundos.llm.adapter import llm_generate

    payload = {k: extraction.get(k) for k in _JUDGMENT_INPUT_KEYS
               if extraction.get(k)}
    if not payload:
        logger.info("PROFILE: nothing extracted — skipping judgement stage.")
        return extraction

    try:
        judged = llm_generate(
            role="company_profile_judgment",
            # NO hardcoded config profile. The role's tier ("judgment", from
            # the admin prompt row or the shipped default) resolves through
            # TenantLLMTier to whatever model that tenant has chosen for
            # judgement work. Switching this to Gemini, or to a cheaper
            # model, is an admin change.
            context={"company": {"name": profile.company.name},
                     "company_name": profile.company.name,
                     "extraction": payload},
            user=user,
            calling_context="profile.deep_extract.judgment",
            section_context={"company_name": profile.company.name})
        judged = guardrail.apply("company_profile_judgment", judged)
    except Exception as e:
        logger.warning("PROFILE: judgement stage failed (%s) — keeping "
                       "stage-1 thesis.", e)
        return extraction

    merged = dict(extraction)
    if judged.get("investment_thesis"):
        merged["investment_thesis"] = judged["investment_thesis"]
    if judged.get("conflict_resolution"):
        # Attach the adjudication ALONGSIDE the raw conflicts rather than
        # replacing them. The founder should still see that sources
        # disagreed, not just the model's pick.
        merged["conflict_resolution"] = judged["conflict_resolution"]
    if judged.get("data_quality_notes"):
        merged["data_quality_notes"] = judged["data_quality_notes"]
    for key in ("missing",):
        extra = judged.get(key) or []
        if extra:
            merged[key] = list(dict.fromkeys(list(merged.get(key) or [])
                                             + list(extra)))

    # PERSIST THE JUDGEMENT so cheap calls can reuse it.
    #
    # This is what makes the expensive tier worth paying for more than once.
    # The judgement call adjudicated conflicting figures and identified the
    # real risks; without storing that, every subsequent Simple call would
    # re-derive its own view from the raw sources and could contradict it —
    # the profile would say one thing and the thesis another.
    #
    # Stored once, it becomes authoritative context for every downstream
    # Simple call at a fraction of a token of extra cost.
    # v27.5 — its own savepoint. This write runs immediately before the
    # financials→CKB sync, and when it failed the caught exception left
    # `needs_rollback` set, so the sync died reporting a fault that belonged
    # here. See `_isolated_write` for the full explanation.
    with _isolated_write("judgement persist"):
        profile.judgment = _distil_judgment(judged)
        profile.save(update_fields=["judgment", "updated_at"])
    return merged


def _distil_judgment(judged):
    """Compact the judgement into something cheap to carry as context.

    The full judgement can be long. Simple calls only need the CONCLUSIONS —
    which value won a conflict, what the identified risks are — not the
    reasoning that produced them. Distilling keeps the reuse close to free.
    """
    resolved = {}
    for row in (judged.get("conflict_resolution") or []):
        field = row.get("field")
        if field and row.get("recommended_value") is not None:
            resolved[field] = {
                "value": row.get("recommended_value"),
                "confidence": row.get("confidence"),
                "outlier_flagged": row.get("outlier_flagged") or "",
            }
    thesis = judged.get("investment_thesis") or {}
    return {
        "resolved_values": resolved,
        "risks": (thesis.get("risks_and_open_questions") or [])[:8],
        "leadership_assessment": (thesis.get("leadership_assessment")
                                  or "")[:1200],
        "data_quality_notes": (judged.get("data_quality_notes") or [])[:6],
    }


def judgment_context(profile):
    """Authoritative judgement context for downstream Simple calls.

    Returns {} when no judgement has been made, so callers need no guard.
    """
    return getattr(profile, "judgment", None) or {}



def _partial_date(value):
    """Coerce a partially-specified date to the first of its period.

    Funding rounds are routinely announced with month precision, and the
    month IS the fact — "May 2006" is what the filing says. Handing "2006-05"
    to a DateField raises, and because the whole row is created in one call,
    the amount, the lead investor and the investor list were discarded along
    with the unknown day. Losing a funding round because its day is unknown
    is not an acceptable trade.

    Returns (date_or_None, precision) where precision is one of
    "day" / "month" / "year" / "" so the UI can render "May 2006" rather
    than implying the 1st was the announcement date.
    """
    import datetime as _dt
    import re as _re

    if value in (None, "", "-"):
        return None, ""
    if isinstance(value, _dt.datetime):
        return value.date(), "day"
    if isinstance(value, _dt.date):
        return value, "day"
    text = str(value).strip()
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return _dt.date.fromisoformat(text), "day"
        except ValueError:
            return None, ""
    m = _re.fullmatch(r"(\d{4})[-/](\d{1,2})", text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return _dt.date(year, month, 1), "month"
        return None, ""
    if _re.fullmatch(r"\d{4}", text):
        return _dt.date(int(text), 1, 1), "year"
    return None, ""

def _abandon_ungrounded_run(profile, exc, *, payloads, failures,
                            fingerprint, started_at, user=None):
    """End a run that retrieved nothing, leaving the profile untouched.

    What the founder sees matters here. The previous behaviour published an
    invented dossier; the first attempt at a fix published a WORSE one, one
    section at a time, after paying for the extract twice. Neither told anyone
    what had happened.

    So: the existing profile is left exactly as it was (a previously good
    profile is not replaced by an empty one), status returns to whatever it
    was rather than sticking on "generating", the fingerprint is not stamped
    so the next attempt actually retries, and the reason is returned to the
    caller in the same shape the API already speaks.
    """
    from fundos.profile.trace import FAIL, trace_event

    trace_event("generation.abandoned", FAIL,
                reason=exc.reason,
                mode=exc.mode,
                fields_discarded=exc.populated_fields,
                remedy=("no sections were written and the existing profile is "
                        "unchanged. Fix the source failure above, then "
                        "regenerate."))
    logger.error(
        "PROFILE: abandoning generation for %s — %s. %d generated fields "
        "discarded; the profile is unchanged.",
        profile.company_id, exc.reason, exc.populated_fields)

    # Back to a resting state. Leaving it on "generating" makes the UI spin
    # forever on a run that has already stopped.
    profile.status = "generated" if profile.last_generated_at else "draft"
    profile.save(update_fields=["status", "updated_at"])

    _record_generation_run(
        profile, started_at=started_at, mode="abandoned_ungrounded",
        forced=False, fingerprint=fingerprint, payloads=payloads,
        failures=failures, generated=[], skipped=[], user=user)

    return {
        "generated": [], "skipped": [], "sourceFailures": failures,
        "completenessPct": float(profile.completeness_pct),
        "skippedReason": "no_sources_retrieved",
        "groundingMode": exc.mode,
        "reason": exc.reason,
        "lastGeneratedAt": profile.last_generated_at,
    }


# Re-exported, not redefined. The refusal is raised inside the pipeline, and
# two classes of the same name in two modules is how an `except
# UngroundedGeneration` silently stops catching the thing it was written for —
# the failure mode being especially ugly here, since not catching it means
# publishing a fabricated profile.
from fundos.profile.pipeline.orchestrator import (  # noqa: E402,F401
    UngroundedGeneration,
)


def _count_populated(result, _depth=0):
    """How many leaf fields the model actually filled.

    Used only to make an ungrounded refusal legible: "122 fields returned,
    nothing retrieved" is the sentence that tells an operator this was
    recollection rather than research.
    """
    if _depth > 6:
        return 0
    if isinstance(result, dict):
        return sum(_count_populated(v, _depth + 1) for v in result.values())
    if isinstance(result, (list, tuple)):
        return sum(_count_populated(v, _depth + 1) for v in result)
    if result in (None, "", [], {}):
        return 0
    return 1


def _run_searched_since(since):
    """Did any call in this run actually perform a web search?

    Reads the call log rather than trusting a flag, because the question the
    gate asks is "was anything RETRIEVED", and only the provider metadata
    knows. A None search_count means the provider gave us no count — that is
    NOT evidence of a search, so it does not count as one.
    """
    if since is None:
        return False
    try:
        from django.db.models import Sum
        from fundos.llm.models import LLMCallLog
        total = LLMCallLog.objects.filter(
            created_at__gte=since,
            calling_context__startswith="profile."
        ).aggregate(n=Sum("search_count")).get("n")
        return bool(total and total > 0)
    except Exception as e:
        logger.debug("PROFILE: search-count lookup unavailable: %s", e)
        return False


def _tev_gate(step, status, **fields):
    """trace_event without importing it at module scope (circular import)."""
    try:
        from fundos.profile.trace import trace_event
        trace_event(step, status, **fields)
    except Exception:
        pass


@contextlib.contextmanager
def _isolated(label):
    """Run a best-effort block so its failure cannot poison the transaction.

    THE BUG THIS FIXES
    ------------------
    Several blocks here are deliberately "never fail the run": a competitor
    that will not parse, a financial row that fails validation. Each caught
    its exception and carried on — which is right for an application error
    and WRONG for a database error. A failed statement leaves PostgreSQL's
    transaction aborted, and every subsequent query dies with "You can't
    execute queries until the end of the 'atomic' block".

    That is what the recurring `financials->CKB sync failed` warning actually
    is. The sync is not broken; it is merely the next thing to touch the
    database after something earlier was swallowed. The real error is the one
    nobody logged.

    A savepoint per block means a failure rolls back only that block and the
    connection stays usable, so the next step's error is its own.
    """
    from django.db import transaction as _txn
    try:
        with _txn.atomic():
            yield
    except Exception as e:
        logger.warning("PROFILE: %s skipped: %s: %s",
                       label, type(e).__name__, e)


def _stamp_sources_hash(profile, fingerprint, *, failures, extra_failed=False):
    """Record the fingerprint ONLY for a run worth skipping a repeat of.

    THE BUG THIS FIXES
    ------------------
    The skip gate reads: "sources unchanged since the last SUCCESSFUL run".
    The write did not check success. So on 13 Aug the Deepak Nitrite run lost
    its website to a TLS error, refused the assessment as ungrounded, recorded
    failures=2 — and stamped the fingerprint anyway. The operator's retry 89
    seconds later hit the gate and did nothing at all, while reporting a
    clean skip. From the outside that is indistinguishable from "we retried
    and it failed the same way".

    Two conditions, both necessary:
      * the run must have had no source failures and no downstream refusal;
      * the sources must be non-empty. Two failed collections hash
        identically, so an empty source set would otherwise satisfy a cache
        whose entire premise is "identical sources produce identical output".
    """
    if failures:
        logger.info(
            "PROFILE: not recording the sources fingerprint for %s — %d "
            "source failure(s). The next run must retry rather than skip.",
            profile.company_id, len(failures))
        return False
    if extra_failed:
        logger.info(
            "PROFILE: not recording the sources fingerprint for %s — a "
            "downstream step failed. The next run must retry rather than "
            "skip.", profile.company_id)
        return False
    if not fingerprint or _sources_are_empty(getattr(profile,
                                                     "_last_payloads", None)):
        logger.info(
            "PROFILE: not recording the sources fingerprint for %s — nothing "
            "was retrieved, so there is no successful run to skip a repeat "
            "of.", profile.company_id)
        return False
    profile.sources_hash = fingerprint
    return True




def _year_int(v):
    if v in (None, ""):
        return None
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    try:
        return int(digits[:4]) if digits else None
    except ValueError:
        return None


def compute_profile_ratios(profile):
    """Deterministic multiples/growth — NEVER asked of the model (mirrors the
    numeric-authority rule that compute_cash_position already follows).

    EV is approximated by the highest post-money valuation on record for a
    private company; state that proxy in the UI. Stored on a computed section
    so the serializer/UI can render it read-only.
    """
    from fundos.profile.models import FundingRound, ProfileSection

    fin = ProfileSection.objects.filter(
        profile=profile, section_key="financial_summary", is_active=True).first()
    post_vals = [r.post_money_value for r in
                 FundingRound.objects.filter(profile=profile)
                 if r.post_money_value is not None]
    ev_proxy = max(post_vals) if post_vals else None

    years = []
    if fin:
        years = sorted(((fin.structured or {}).get("items") or []),
                       key=lambda r: str(r.get("fiscalYear") or ""))

    out, prev_rev = [], None
    for row in years:
        rev = row.get("revenue")
        rec = {"fiscalYear": row.get("fiscalYear"), "revenue": rev}
        try:
            if rev and prev_rev:
                rec["yoy_growth_pct"] = round(
                    (float(rev) - float(prev_rev)) / float(prev_rev) * 100, 1)
            if rev and ev_proxy:
                rec["ev_revenue"] = round(float(ev_proxy) / float(rev), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        out.append(rec)
        if rev:
            prev_rev = rev

    ProfileSection.objects.update_or_create(
        profile=profile, section_key="derived_multiples",
        defaults=dict(tenant_id=profile.tenant_id, is_active=True,
                      structured={"ev_basis": "max post-money (proxy)",
                                  "by_year": out}, source="computed"))
    return out


def answer_profile_question(profile, question, user=None):
    """Grounded Q&A over the already-retrieved profile. SIMPLE tier, no web.

    Builds the assembled profile as context and asks the profile_qa role,
    which the tenant's SIMPLE tier routes to a cheap, tools-off model.
    """
    from fundos.llm.adapter import llm_generate
    from fundos.profile import spec_serializer, suggestions

    dossier = spec_serializer.build_sections(profile)
    result = llm_generate(
        role="profile_qa",
        context={"company": {"name": profile.company.name},
                 "profile": dossier, "question": question},
        section_context={"question": question},
        user=user, calling_context="profile.qa")

    # WHAT TO ASK NEXT, from the same dossier this answer was grounded in.
    #
    # The chat lives at this endpoint and nowhere else. Without this it would
    # have to re-fetch the whole profile after every reply just to refresh
    # its chips -- a second round trip for something already computed here,
    # and a window in which the chips describe a state one answer out of
    # date. The dossier is already built; the suggestions are a walk over it.
    if isinstance(result, dict):
        result["readinessBreakdown"] = suggestions.attach(
            spec_serializer._readiness_breakdown(dossier))
    return result
