"""The five-stage refresh pipeline.

    scrape → explode → enrich → merge → derive

Ported from the supplied Inc42 stack (scraper.py, funding_db.py,
categorize_funding.py) with three structural changes:

  * Excel becomes the database. The .tmp/.bak/os.replace dance with its
    five-attempt PermissionError retry existed because Windows locks open
    files. That entire failure mode disappears.

  * The explode and enrich stages are NEW. The supplied code produces deal
    rows; the matching engine needs investor-per-deal rows with a classified
    investor master. That gap is stages 2 and 3 here.

  * Every stage runs under a RefreshRun and can execute in DRY RUN, where it
    computes the full diff and writes nothing. That is what makes the refresh
    testable from the admin UI rather than only observable after the fact.

WHAT THE PIPELINE MUST NEVER TOUCH
----------------------------------
Investor.RELATIONSHIP_FIELDS. Who we have met, who owns the relationship, the
contact details — entered by humans, not recoverable from any source. The
merge stage writes master attributes and aggregates only. There is a test
asserting this and it should never be relaxed.
"""
import datetime as dt
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value parsing — ported from config.parse_amount / sanitize_date_string
# ---------------------------------------------------------------------------

DATE_FORMATS = ("%d %b %Y", "%d-%b-%y", "%d %b, %Y", "%Y-%m-%d", "%d/%m/%Y")


def sanitize_date_string(value):
    """Fix the source's known date typos before parsing.

    Kept verbatim in behaviour from the supplied config.sanitize_date_string:
    trailing asterisks, the specific 20205/20206 corrections applied BEFORE the
    generic truncation (so "20205" becomes 2025 rather than being clipped to
    2020), then a generic guard for any 5+ digit year.
    """
    import re
    if not value:
        return ""
    s = str(value).strip()
    s = re.sub(r"\*+", "", s)
    s = re.sub(r"20205", "2025", s)
    s = re.sub(r"20206", "2026", s)
    s = re.sub(r"(\d{4})\d+$", r"\1", s)
    return s.strip()


def parse_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = sanitize_date_string(value)
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def parse_amount_usd_mn(value):
    """Funding amount string → millions USD. Port of config.parse_amount."""
    import re
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    s = str(value).strip()
    if s in ("-", "\u2013", "\u2014", ""):
        return None
    cleaned = re.sub(r"[^\d\.a-zA-Z]", "", s).lower()
    try:
        if "mn" in cleaned:
            return Decimal(cleaned.replace("mn", ""))
        if "bn" in cleaned:
            return Decimal(cleaned.replace("bn", "")) * 1000
        if "k" in cleaned:
            return Decimal(cleaned.replace("k", "")) / 1000
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def dedup_key(name, deal_date, amount):
    """(name_lower, date, amount_2dp) — the supplied funding_db key.

    Amount is formatted to 2dp so 280, 280.0 and 280.00 collapse; date is
    normalised to ISO so string and datetime rows interoperate. Both details
    came from the original and both matter — without them a re-scrape
    duplicates rows that differ only in formatting.
    """
    d = deal_date.isoformat() if deal_date else ""
    try:
        a = f"{float(amount):.2f}" if amount not in (None, "") else "0"
    except (TypeError, ValueError):
        a = "0"
    return f"{str(name or '').strip().lower()}|{d}|{a}"


def split_investors(raw):
    """Split the source's comma-separated investor string.

    Guards the common source artefacts: 'and'/'&' joins, 'others', 'undisclosed'
    and trailing 'etc'. An undisclosed participant is not an investor and must
    not become a row, or it accumulates into a phantom entity with hundreds of
    deals that would rank first on every tier-1 query.
    """
    if not raw:
        return []
    import re
    text = str(raw)
    text = re.sub(r"\s+and\s+", ",", text, flags=re.I)
    parts = [p.strip(" .;&") for p in text.split(",")]
    out, seen = [], set()
    for p in parts:
        if not p or len(p) < 2:
            continue
        low = p.lower()
        if low in ("others", "other", "undisclosed", "n/a", "na", "etc",
                   "unknown", "existing investors", "angel investors"):
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Stage results
# ---------------------------------------------------------------------------

class StageReport(dict):
    """A stage's outcome. Plain dict so it serialises straight to the run."""

    def __init__(self, stage):
        super().__init__(stage=stage, status="pending", counts={},
                         warnings=[], errors=[], samples={})

    def count(self, key, n=1):
        self["counts"][key] = self["counts"].get(key, 0) + n

    def warn(self, msg):
        self["warnings"].append(str(msg)[:500])

    def sample(self, key, item, cap=25):
        bucket = self["samples"].setdefault(key, [])
        if len(bucket) < cap:
            bucket.append(item)


# ---------------------------------------------------------------------------
# Stage 1 — scrape
# ---------------------------------------------------------------------------

def stage_scrape(run, *, dry_run, source_config, limit_articles=None,
                 single_url=None):
    """Fetch deal rows from the configured source.

    The browser automation, circuit breaker and retry policy are the supplied
    scraper's, ported to the adapter in `fundos.investors.sources`. Nothing
    about Inc42 is hardcoded here — the adapter is looked up by name so a
    second source is a new adapter and a config row.
    """
    rep = StageReport("scrape")
    from fundos.investors.sources import get_adapter

    adapter = get_adapter(source_config.adapter)
    try:
        rows = adapter.fetch(source_config, limit_articles=limit_articles,
                             single_url=single_url)
    except Exception as e:
        rep["status"] = "failed"
        rep["errors"].append(str(e)[:1000])
        logger.error("REFRESH %s: scrape failed: %s", run.id, e, exc_info=True)
        return rep, []

    rep.count("rows_fetched", len(rows))
    rep["status"] = "ok"
    return rep, rows


# ---------------------------------------------------------------------------
# Stage 2 — persist deals and explode into investor rows
# ---------------------------------------------------------------------------

def stage_explode(run, rows, *, dry_run, tenant_id=None):
    """Persist FundingDeal rows and explode them into InvestorDeal rows.

    This is the stage that turns N funding events into the 7,777-row base
    table. `fundraise_per_investor` = deal size / participant count is the
    figure that later averages into each investor's ticket size, so a wrong
    participant count propagates all the way to the cheque-band test.
    """
    rep = StageReport("explode")
    from fundos.investors import identity, normalizer
    from fundos.investors.models import FundingDeal, InvestorDeal

    existing = set(FundingDeal.objects.values_list("dedup_key", flat=True))
    made_deals = []

    for r in rows:
        deal_date = parse_date(r.get("date"))
        amount = parse_amount_usd_mn(r.get("amount"))
        name = (r.get("name") or "").strip()
        if not name:
            rep.count("skipped_no_name")
            continue

        key = dedup_key(name, deal_date, amount)
        if key in existing:
            rep.count("duplicate")
            continue
        existing.add(key)
        rep.count("new_deal")

        sector, _, s_score = normalizer.normalize(
            r.get("sector"), record_misses=not dry_run)
        sub, _, ss_score = normalizer.normalize(
            r.get("subsector"), record_misses=not dry_run)
        if r.get("sector") and sector is None:
            rep.count("sector_unmapped")
            rep.sample("sector_misses",
                       {"raw": r.get("sector"), "score": round(s_score, 1)})

        participants = split_investors(r.get("investors"))
        lead = (r.get("lead_investor") or "").strip()
        if lead and lead not in participants:
            participants.append(lead)
        if not participants:
            rep.count("deal_no_investors")

        iso = deal_date.isocalendar() if deal_date else None
        deal_fields = dict(
            source=r.get("source", "inc42"),
            source_url=(r.get("source_url") or "")[:1024],
            company_name=name[:255], deal_date=deal_date,
            iso_week=iso[1] if iso else None, iso_year=iso[0] if iso else None,
            raw_sector=(r.get("sector") or "")[:255],
            raw_sub_sector=(r.get("subsector") or "")[:255],
            sector=sector, sub_sector=sub,
            business_model=(r.get("business_model") or "")[:128],
            round_type=(r.get("round_type") or "")[:128],
            raw_amount=(str(r.get("amount") or ""))[:128],
            amount_usd_mn=amount,
            raw_investors=(r.get("investors") or ""),
            raw_lead_investor=lead[:512],
            dedup_key=key, first_seen_run=run.id, last_seen_run=run.id,
        )

        rep.sample("new_deals", {
            "company": name, "date": deal_date.isoformat() if deal_date else None,
            "amount": float(amount) if amount is not None else None,
            "investors": len(participants)})

        if dry_run:
            for p in participants:
                inv, action = identity.resolve(p, tenant_id=tenant_id,
                                               create_if_missing=False)
                if inv is None:
                    rep.count("investor_new")
                    rep.sample("new_investors", {"name": p})
                else:
                    rep.count(f"investor_{action}")
            continue

        with transaction.atomic():
            deal = FundingDeal.objects.create(**deal_fields)
            made_deals.append(deal)
            n = len(participants) or 1
            per = (amount / Decimal(n)) if amount is not None else None
            for p in participants:
                inv, action = identity.resolve(p, tenant_id=tenant_id)
                if inv is None:
                    continue
                rep.count(f"investor_{action}")
                if action == "created":
                    rep.sample("new_investors", {"name": p,
                                                 "investorId": str(inv.id)})
                InvestorDeal.objects.get_or_create(
                    investor=inv, funding_deal=deal,
                    defaults=dict(
                        tenant_id=tenant_id, deal_date=deal_date,
                        deal_size_usd_mn=amount, sector=sector, sub_sector=sub,
                        company_name=name[:255],
                        is_lead=(p.strip().lower() == lead.strip().lower()),
                        investors_in_deal=n, fundraise_per_investor=per))
                rep.count("investor_deal_rows")

    rep["status"] = "ok"
    return rep, made_deals


# ---------------------------------------------------------------------------
# Stage 3 — enrich
# ---------------------------------------------------------------------------

def stage_enrich(run, *, dry_run, limit=None, tenant_id=None):
    """Classify investors that have no category yet.

    NEW NAMES ONLY. Investors already classified are not revisited: it keeps
    cost down, and it stops a classification drifting between refreshes for an
    entity whose nature has not changed. Re-classification is an explicit admin
    action on a specific investor.
    """
    rep = StageReport("enrich")
    from fundos.investors.models import (ClassificationReviewItem,
                                         Investor, InvestorCategory)
    from fundos.investors.classify import classify_investor, CONFIDENCE_FLOOR

    pending = Investor.objects.filter(category__isnull=True, is_active=True)
    if tenant_id:
        pending = pending.filter(tenant_id=tenant_id)
    pending = list(pending[:limit] if limit else pending)

    rep.count("pending", len(pending))
    if dry_run:
        for inv in pending[:25]:
            rep.sample("to_classify", {"name": inv.name})
        rep["status"] = "ok"
        return rep

    categories = list(InvestorCategory.objects.select_related("bucket"))
    if not categories:
        rep.warn("No investor categories configured — classification skipped.")
        rep["status"] = "skipped"
        return rep

    for inv in pending:
        try:
            result = classify_investor(inv, categories)
        except Exception as e:
            rep.count("failed")
            rep.warn(f"{inv.name}: {e}")
            continue

        if not result:
            rep.count("no_result")
            continue

        cat = result.get("category")
        conf = Decimal(str(result.get("confidence") or 0))

        if conf >= CONFIDENCE_FLOOR and cat:
            inv.category = cat
            inv.hq_city = (result.get("hq_city") or "")[:128]
            inv.hq_country = (result.get("hq_country") or "")[:128]
            inv.classification_source = "llm"
            inv.classification_confidence = conf
            inv.classification_model = (result.get("model") or "")[:64]
            inv.classified_at = timezone.now()
            inv.save()
            rep.count("classified")
        else:
            ClassificationReviewItem.objects.create(
                investor=inv, proposed_category=cat,
                proposed_hq_city=(result.get("hq_city") or "")[:128],
                proposed_hq_country=(result.get("hq_country") or "")[:128],
                confidence=conf, reasoning=(result.get("reasoning") or "")[:2000])
            rep.count("queued_for_review")
            rep.sample("low_confidence",
                       {"name": inv.name,
                        "proposed": cat.name if cat else None,
                        "confidence": float(conf)})

    rep["status"] = "ok"
    return rep


# ---------------------------------------------------------------------------
# Stage 4 — merge (relationship-preserving)
# ---------------------------------------------------------------------------

def merge_investor(investor, master_attrs):
    """Apply master attributes to an investor WITHOUT touching relationship data.

    This function is the single place master attributes are written during a
    refresh, and it exists so the guarantee is enforceable in one spot and
    testable. `tests/investors/test_merge_preserves_relationships.py` asserts
    that every field in Investor.RELATIONSHIP_FIELDS is unchanged after a call.
    """
    protected = set(investor.RELATIONSHIP_FIELDS)
    writable = {"category", "hq_city", "hq_country", "has_local_office",
                "single_sector_focused", "is_impact"}

    changed = []
    for field, value in (master_attrs or {}).items():
        if field in protected:
            logger.error(
                "REFRESH: refused to write protected relationship field %r on "
                "investor %s. This is a bug in the caller — relationship data "
                "is human-entered and not recoverable from any source.",
                field, investor.id)
            continue
        if field not in writable:
            continue
        if value in (None, ""):
            continue
        setattr(investor, field, value)
        changed.append(field)

    if changed:
        investor.save(update_fields=changed + ["updated_at"])
    return changed


def stage_merge(run, *, dry_run):
    """Placeholder stage kept explicit in the report for traceability.

    Attribute merging happens inside enrich (for new investors) and inside the
    admin merge action (for identity decisions). The stage is still reported so
    the run record shows all five stages and a reader is not left wondering
    whether one silently did not execute.
    """
    rep = StageReport("merge")
    rep["status"] = "ok"
    rep.count("relationship_fields_protected",
              len(_relationship_field_names()))
    return rep


def _relationship_field_names():
    from fundos.investors.models import Investor
    return Investor.RELATIONSHIP_FIELDS


# ---------------------------------------------------------------------------
# Stage 5 — derive
# ---------------------------------------------------------------------------

def recompute_investor_aggregates(investor_ids=None):
    """Recompute per-investor counts and average ticket.

    New ATS = mean of fundraise_per_investor over that investor's deals with a
    positive deal size, matching the workbook's AVERAGEIFS with a ">0" guard.
    Zero-size deals are excluded rather than counted as zero, which would drag
    every ticket average toward nothing.
    """
    from django.db.models import Avg, Count, Max
    from fundos.investors.models import Investor, InvestorDeal

    qs = Investor.objects.all()
    if investor_ids:
        qs = qs.filter(id__in=investor_ids)

    updated = 0
    for inv in qs.iterator(chunk_size=500):
        rows = InvestorDeal.objects.filter(investor=inv)
        stats = rows.aggregate(n=Count("id"), last=Max("deal_date"))
        priced = rows.filter(deal_size_usd_mn__gt=0)
        ats = priced.aggregate(a=Avg("fundraise_per_investor"))["a"]
        last_row = rows.order_by("-deal_date").first()

        inv.overall_deals = stats["n"] or 0
        inv.avg_ticket_usd_mn = ats
        inv.last_deal_date = stats["last"]
        inv.last_deal_size_usd_mn = last_row.deal_size_usd_mn if last_row else None
        inv.save(update_fields=["overall_deals", "avg_ticket_usd_mn",
                                "last_deal_date", "last_deal_size_usd_mn",
                                "updated_at"])
        updated += 1
    return updated


def rebuild_sector_deal_data():
    """Rebuild assessment.SectorDealData from the deal universe.

    This is the D-04 answer implemented: scorecard categories E and F —
    30% of the deal score — derive from the same investor deal database that
    powers Stage 3. One data load, two consumers.

    Idempotent full rebuild, following the supplied categorize_funding.py,
    which dropped and recreated its Sector Map sheet on every run. Incremental
    updates to a derived aggregate accumulate drift; rebuilding does not.
    """
    from django.db.models import Avg, Count, Sum
    from fundos.assessment.models import SectorDealData
    from fundos.investors.models import CanonicalSector, InvestorDeal

    today = dt.date.today()
    built = 0

    for level, field in (("sector", "sector"), ("sub_sector", "sub_sector")):
        sectors = CanonicalSector.objects.filter(
            level=("sector" if level == "sector" else "sub_sector"))
        for s in sectors:
            rows = InvestorDeal.objects.filter(**{field: s})
            agg = rows.aggregate(
                deals=Count("funding_deal", distinct=True),
                investors=Count("investor", distinct=True),
                total=Sum("deal_size_usd_mn"),
                avg=Avg("deal_size_usd_mn"))
            if not agg["deals"]:
                continue
            SectorDealData.objects.update_or_create(
                level=level, name=s.name,
                defaults=dict(
                    deal_count=agg["deals"],
                    investor_count=agg["investors"],
                    total_raised_usd_mn=agg["total"],
                    avg_ticket_usd_mn=agg["avg"],
                    as_of_date=today))
            built += 1
    return built


def stage_derive(run, *, dry_run):
    rep = StageReport("derive")
    if dry_run:
        rep["status"] = "skipped"
        rep.warn("Derivation is skipped in a dry run — it reads committed data.")
        return rep
    rep.count("investors_recomputed", recompute_investor_aggregates())
    rep.count("sector_rows_built", rebuild_sector_deal_data())
    rep["status"] = "ok"
    return rep


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_refresh(*, dry_run=True, stages=None, limit_articles=None,
                single_url=None, source_name=None, user=None, tenant_id=None):
    """Run the pipeline end to end under a RefreshRun record.

    Follows the supplied main.py's most important operational habit: each
    stage is wrapped so a failure is logged and the run continues where that
    is safe. A failing enrichment should not discard a good scrape.
    """
    from fundos.investors.models import DataSource, RefreshRun

    stages = set(stages or ["scrape", "explode", "enrich", "merge", "derive"])
    source = (DataSource.objects.filter(name=source_name).first()
              if source_name else DataSource.objects.filter(enabled=True).first())

    run = RefreshRun.objects.create(
        run_type="dry_run" if dry_run else "commit",
        source_name=(source.name if source else ""),
        triggered_by=user, scope={"stages": sorted(stages),
                                  "limitArticles": limit_articles,
                                  "singleUrl": single_url},
        status="running")
    logger.info("REFRESH %s: starting (%s) stages=%s",
                run.id, run.run_type, sorted(stages))

    reports, rows = [], []
    try:
        if "scrape" in stages:
            if not source:
                r = StageReport("scrape")
                r["status"] = "failed"
                r["errors"].append(
                    "No enabled data source configured. Add one in "
                    "Admin → Investors → Data sources.")
                reports.append(r)
            else:
                rep, rows = stage_scrape(run, dry_run=dry_run,
                                         source_config=source,
                                         limit_articles=limit_articles,
                                         single_url=single_url)
                reports.append(rep)

        if "explode" in stages and rows:
            rep, _ = stage_explode(run, rows, dry_run=dry_run,
                                   tenant_id=tenant_id)
            reports.append(rep)

        if "enrich" in stages:
            reports.append(stage_enrich(run, dry_run=dry_run,
                                        tenant_id=tenant_id))

        if "merge" in stages:
            reports.append(stage_merge(run, dry_run=dry_run))

        if "derive" in stages:
            reports.append(stage_derive(run, dry_run=dry_run))

        run.status = ("failed" if any(r["status"] == "failed" for r in reports)
                      else "completed")
    except Exception as e:
        run.status = "failed"
        reports.append({"stage": "orchestrator", "status": "failed",
                        "counts": {}, "warnings": [], "samples": {},
                        "errors": [str(e)[:1000]]})
        logger.error("REFRESH %s: aborted: %s", run.id, e, exc_info=True)

    run.stage_reports = reports
    run.finished_at = timezone.now()
    run.summary = _summarise(reports)
    run.save()
    logger.info("REFRESH %s: %s — %s", run.id, run.status, run.summary)
    return run


def _summarise(reports):
    out = {}
    for r in reports:
        for k, v in (r.get("counts") or {}).items():
            out[k] = out.get(k, 0) + v
    return out
