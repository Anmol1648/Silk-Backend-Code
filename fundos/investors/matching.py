"""The investor matching engine — a direct port of the workbook's Dyn Engine.

PROVENANCE
----------
Every formula here was extracted from India_D2C_Investors_Jun26_v27_1.xlsx.
The workbook is the specification; this module is the implementation; and
`tests/investors/test_matching_reference.py` asserts they agree by reproducing
the workbook's own published counts:

    sub-sector D2C · raise $5M · 2023-01-01 to 2026-06-05
        → 301 qualifying investors, 477 matched deals

If a change to this file breaks that test, the change is wrong.

WHY IT COMPUTES PER REQUEST RATHER THAN PRECOMPUTING
----------------------------------------------------
The workbook recalculates the entire engine whenever a control-panel cell
changes, because the filters ARE the question. A banker moves the raise size
from $5M to $8M and expects a different answer immediately. Precomputing
matches into a table would make every stored row stale the moment someone
touched a filter. Over ~7,800 rows this is a fast aggregate query, so there is
nothing to buy by caching and a correctness problem to be created.

THE FIVE STEPS
--------------
  0. Base table          — every InvestorDeal row.
  1. Sector filter       — SelMember, per deal row.
  2. Cheque band + date  — DynBandElig, per deal row.
  3. Tier                — best of 1/2/3/4 per investor.
  4. Rank within tier    — tier-specific scores.
  5. Bucket and present  — the nine investor types.
"""
import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)

TIER_LABELS = {
    1: "Sub-sector, cheque size and date all match",
    2: "Sector and cheque size match",
    3: "Cheque size matches, any sector",
    4: "Close to the cheque band",
}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class MatchQuery:
    """Everything the engine needs. Nothing domain-specific is hardcoded.

    `sector_ids` / `sub_sector_ids` empty means NO RESTRICTION on that half —
    the workbook's master-toggle behaviour, where an unticked master box means
    "include everything" rather than "exclude everything". Getting this
    backwards silently returns zero rows.
    """

    raise_size_usd_mn: Decimal
    sector_ids: list = field(default_factory=list)
    sub_sector_ids: list = field(default_factory=list)
    date_from: dt.date = None
    date_to: dt.date = None
    bucket_ids: list = field(default_factory=list)
    specialists_only: bool = False
    tenant_id: str = None


@dataclass
class Band:
    min_usd_mn: Decimal
    max_usd_mn: Decimal

    @property
    def centre(self):
        """Midpoint, floored so the ticket-fit divisor can never be zero."""
        c = (self.min_usd_mn + self.max_usd_mn) / Decimal(2)
        return max(c, Decimal("0.0001"))


def resolve_band(raise_size_usd_mn):
    """Raise size → cheque band. Exact match or next smaller.

    Mirrors XLOOKUP(..., match_mode=-1) with the workbook's IFERROR fallback
    to the first row, which is what makes a raise below the smallest configured
    band resolve to the smallest band rather than to nothing.
    """
    from fundos.investors.models import RaiseBand

    bands = list(RaiseBand.objects.order_by("raise_size_usd_mn"))
    if not bands:
        raise ValueError(
            "No raise bands configured. Add rows in Admin → Investors → "
            "Raise bands before running a match.")

    size = Decimal(str(raise_size_usd_mn))
    chosen = bands[0]
    for b in bands:
        if b.raise_size_usd_mn <= size:
            chosen = b
        else:
            break
    return Band(chosen.min_cheque_usd_mn, chosen.max_cheque_usd_mn)


# ---------------------------------------------------------------------------
# Per-row flags — steps 1 and 2
# ---------------------------------------------------------------------------

def _row_flags(row, q, band, cfg, sector_set, sub_sector_set):
    """Compute the workbook's per-deal-row helper columns for one row.

    Workbook columns reproduced here:
        BG SelMember     sector/sub-sector in the selected scope
        AW DynBandElig   cheque band AND date window
        AT DynPrimary    SelMember AND DynBandElig      → tier 1 evidence
        AZ SecMatch      parent-sector match in band+date → tier 2 evidence
        BD NearFlag      in date window but OUT of band → tier 4 evidence
        BE NearDist      distance to the nearest band edge
    """
    in_date = True
    if q.date_from and (row.deal_date is None or row.deal_date < q.date_from):
        in_date = False
    if q.date_to and (row.deal_date is None or row.deal_date > q.date_to):
        in_date = False

    # Band basis. Confirmed as deal size: the workbook tests column V
    # (Deal Size of Startup) in every one of AT/AW/AZ/BD/BE, and computes
    # New ATS in column P without ever referencing it in the engine.
    if cfg.band_test_basis == "investor_ats":
        basis = row.investor_avg_ticket
    else:
        basis = row.deal_size_usd_mn

    if basis is None:
        in_band, near_dist = False, None
    else:
        basis = Decimal(str(basis))
        in_band = band.min_usd_mn <= basis <= band.max_usd_mn
        if basis < band.min_usd_mn:
            near_dist = band.min_usd_mn - basis
        elif basis > band.max_usd_mn:
            near_dist = basis - band.max_usd_mn
        else:
            near_dist = Decimal(0)

    sel_sector = (not sector_set) or (row.sector_id in sector_set)
    sel_sub = (not sub_sector_set) or (row.sub_sector_id in sub_sector_set)
    sel_member = sel_sector and sel_sub

    band_elig = in_band and in_date
    dyn_primary = band_elig and sel_member

    # Tier 2: the deal's SECTOR is a parent of a selected sub-sector, in band
    # and in date — but the sub-sector itself did not match.
    sec_match = False
    if sub_sector_set and band_elig and row.sector_id:
        sec_match = row.sector_id in _parent_sector_ids(sub_sector_set)

    near_flag = in_date and not in_band

    return {
        "sel_member": sel_member,
        "band_elig": band_elig,
        "dyn_primary": dyn_primary,
        "sec_match": sec_match,
        "near_flag": near_flag,
        "near_dist": near_dist,
        "in_date": in_date,
    }


_PARENT_CACHE = {}


def _parent_sector_ids(sub_sector_ids):
    """Parent sectors of the selected sub-sectors, memoised per call set."""
    key = tuple(sorted(str(s) for s in sub_sector_ids))
    if key in _PARENT_CACHE:
        return _PARENT_CACHE[key]
    from fundos.investors.models import CanonicalSector
    ids = set(
        CanonicalSector.objects
        .filter(id__in=sub_sector_ids, parent__isnull=False)
        .values_list("parent_id", flat=True))
    _PARENT_CACHE[key] = ids
    return ids


def clear_caches():
    """Call after editing the sector tree. Cheap, and avoids stale parents."""
    _PARENT_CACHE.clear()


# ---------------------------------------------------------------------------
# Steps 3, 4 and 5
# ---------------------------------------------------------------------------

def _assign_tier(agg):
    """Best available tier wins — lowest number.

    Workbook column Q (FillTier). An investor with one tier-1 deal and forty
    tier-3 deals is a tier-1 investor: the strongest single piece of evidence
    decides the tier, and volume only decides the rank within it.
    """
    if agg["matched"] > 0:
        return 1
    if agg["sector_matched"] > 0:
        return 2
    if agg["band_elig"] > 0:
        return 3
    if agg["near"] > 0:
        return 4
    return 0


def _rel_score(tier, agg, band, cfg):
    """Tier 3/4 relevance score — workbook column AE.

        ActivityFactor  = ScopeCount / (ScopeCount + k)
        TicketFitFactor = 1 / (1 + |AvgScopeSize - centre| / centre)
        RelScore        = 100 x (wA x fA + wF x fF)

    ActivityFactor saturates: 3 deals → 0.50, 9 → 0.75, 27 → 0.90. More
    activity always helps but with diminishing returns, so a prolific investor
    cannot crowd out a well-fitting one on volume alone.

    TicketFitFactor is a symmetric penalty on distance from the band midpoint,
    normalised by the midpoint — so it is scale-free. Being $2M off a $5M
    centre costs exactly what being $8M off a $20M centre costs.
    """
    if tier < 2:
        return Decimal(0)

    scope = {2: agg["sector_matched"], 3: agg["band_elig"],
             4: agg["near"]}.get(tier, 0)
    avg = agg["avg_scope_size"].get(tier)

    k = Decimal(cfg.activity_constant or 3)
    f_activity = (Decimal(scope) / (Decimal(scope) + k)) if scope else Decimal(0)

    if avg is None:
        f_ticket = Decimal(0)
    else:
        centre = band.centre
        f_ticket = Decimal(1) / (Decimal(1) + abs(Decimal(str(avg)) - centre) / centre)

    wa = Decimal(str(cfg.activity_weight))
    wf = Decimal(str(cfg.ticket_fit_weight))
    return Decimal(100) * (wa * f_activity + wf * f_ticket)


def _fill_score(tier, agg, rel_score):
    """Rank key within tier — workbook column T.

    Tiers 1 and 2 multiply the primary signal by 100,000 and add the secondary
    as a tiebreak, which is just a lexicographic sort expressed arithmetically.
    Kept in that form so a number here can be compared against a workbook cell
    during verification.
    """
    if tier == 1:
        return Decimal(agg["overall"]) * 100000 + Decimal(agg["matched"])
    if tier == 2:
        return (Decimal(agg["top_sector_volume"]) * 100000
                + Decimal(agg["sector_matched"]))
    return rel_score


def _match_percent(tier, rel_score, agg, band):
    """A 0–100 figure for display. NOT part of the ranking.

    The ranking is FillScore. This is a human-readable summary of fit derived
    from the same components, banded by tier so a tier-1 investor always reads
    higher than a tier-3 one. Presenting FillScore directly would be
    meaningless on screen — tier 1 values run into the millions.
    """
    ceilings = {1: (88, 99), 2: (78, 90), 3: (62, 80), 4: (45, 65)}
    lo, hi = ceilings.get(tier, (0, 0))
    if tier == 1:
        depth = min(agg["matched"], 10) / 10
    elif tier == 2:
        depth = min(agg["sector_matched"], 10) / 10
    else:
        depth = float(rel_score) / 100 if rel_score else 0
    return round(lo + (hi - lo) * depth)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_match(query: MatchQuery, *, limit=None, config=None):
    """Run the five-step pipeline. Returns ranked results plus a trace.

    The trace is not decoration. "Why is this investor ranked fourth" has to be
    answerable exactly, from stored data, without re-running anything — so
    every intermediate value that fed the rank comes back with the result.
    """
    from fundos.investors.models import Investor, InvestorDeal, MatchConfig

    cfg = config or MatchConfig.active()
    band = resolve_band(query.raise_size_usd_mn)

    date_from = query.date_from
    date_to = query.date_to or dt.date.today()
    if date_from is None:
        months = cfg.default_window_months or 30
        date_from = date_to - dt.timedelta(days=int(months * 30.44))

    q = MatchQuery(
        raise_size_usd_mn=query.raise_size_usd_mn,
        sector_ids=query.sector_ids, sub_sector_ids=query.sub_sector_ids,
        date_from=date_from, date_to=date_to,
        bucket_ids=query.bucket_ids, specialists_only=query.specialists_only,
        tenant_id=query.tenant_id)

    sector_set = {str(s) for s in (q.sector_ids or [])}
    sub_sector_set = {str(s) for s in (q.sub_sector_ids or [])}

    rows = (InvestorDeal.objects
            .select_related("investor")
            .only("investor_id", "deal_date", "deal_size_usd_mn", "sector_id",
                  "sub_sector_id", "company_name", "fundraise_per_investor",
                  "is_lead", "investor__name", "investor__avg_ticket_usd_mn"))
    if q.tenant_id:
        rows = rows.filter(tenant_id=q.tenant_id)

    # --- Aggregate per investor -------------------------------------------
    agg_by_investor = {}
    for row in rows.iterator(chunk_size=2000):
        row.sector_id = str(row.sector_id) if row.sector_id else None
        row.sub_sector_id = str(row.sub_sector_id) if row.sub_sector_id else None
        row.investor_avg_ticket = row.investor.avg_ticket_usd_mn

        a = agg_by_investor.setdefault(row.investor_id, _new_agg(row.investor))
        a["overall"] += 1

        f = _row_flags(row, q, band, cfg, sector_set, sub_sector_set)
        if f["sel_member"]:
            a["in_scope"] += 1
        if f["dyn_primary"]:
            a["matched"] += 1
            _push_scope(a, 1, row)
        if f["sec_match"] and not f["dyn_primary"]:
            a["sector_matched"] += 1
            _push_scope(a, 2, row)
        if f["band_elig"]:
            a["band_elig"] += 1
            _push_scope(a, 3, row)
        if f["near_flag"]:
            a["near"] += 1
            _push_scope(a, 4, row)
            if f["near_dist"] is not None:
                cur = a["near_min_dist"]
                a["near_min_dist"] = (f["near_dist"] if cur is None
                                      else min(cur, f["near_dist"]))
        if f["sec_match"]:
            a["top_sector_volume"] = max(a["top_sector_volume"],
                                         a["sector_matched"])
        if f["dyn_primary"] or f["sec_match"] or f["band_elig"]:
            a["representative"] = a["representative"] or {
                "company": row.company_name,
                "amount": float(row.deal_size_usd_mn or 0),
                "date": row.deal_date.isoformat() if row.deal_date else None,
            }

    # --- Score and rank ----------------------------------------------------
    results = []
    for investor_id, a in agg_by_investor.items():
        for t in (1, 2, 3, 4):
            sizes = a["_scope_sizes"].get(t)
            a["avg_scope_size"][t] = (sum(sizes) / len(sizes)) if sizes else None

        tier = _assign_tier(a)
        if tier == 0:
            continue

        rel = _rel_score(tier, a, band, cfg)
        fill = _fill_score(tier, a, rel)

        excl_share = (Decimal(a["in_scope"]) / Decimal(a["overall"])
                      if a["overall"] else Decimal(0))
        exclusive = (a["overall"] > 0 and a["in_scope"] > 0
                     and excl_share >= Decimal(str(cfg.exclusivity_threshold))
                     and a["overall"] >= cfg.exclusivity_min_deals)

        if q.specialists_only and not exclusive:
            continue
        if q.bucket_ids:
            bid = a["bucket_id"]
            if not bid or str(bid) not in {str(b) for b in q.bucket_ids}:
                continue

        results.append({
            "investorId": str(investor_id),
            "name": a["name"],
            "categoryName": a["category_name"],
            "bucketName": a["bucket_name"],
            "bucketId": str(a["bucket_id"]) if a["bucket_id"] else None,
            "hqCountry": a["hq_country"],
            "avgTicketUsdMn": (float(a["avg_ticket"])
                               if a["avg_ticket"] is not None else None),
            "tier": tier,
            "tierLabel": TIER_LABELS.get(tier, ""),
            "matchPercent": _match_percent(tier, rel, a, band),
            "exclusive": exclusive,
            "representativeDeal": a["representative"],
            # The trace: every value that produced the rank.
            "trace": {
                "overallDeals": a["overall"],
                "matchedDeals": a["matched"],
                "sectorMatchedDeals": a["sector_matched"],
                "bandEligibleDeals": a["band_elig"],
                "nearDeals": a["near"],
                "nearMinDistance": (float(a["near_min_dist"])
                                    if a["near_min_dist"] is not None else None),
                "scopeCount": {2: a["sector_matched"], 3: a["band_elig"],
                               4: a["near"]}.get(tier, a["matched"]),
                "avgScopeSize": (float(a["avg_scope_size"][tier])
                                 if a["avg_scope_size"].get(tier) is not None
                                 else None),
                "bandCentre": float(band.centre),
                "relScore": float(rel),
                "fillScore": float(fill),
                "exclusivityShare": float(excl_share),
            },
            "_sort": (tier, -float(fill)),
        })

    results.sort(key=lambda r: r.pop("_sort"))
    total = len(results)
    if limit:
        results = results[:limit]

    return {
        "band": {"minUsdMn": float(band.min_usd_mn),
                 "maxUsdMn": float(band.max_usd_mn),
                 "centreUsdMn": float(band.centre)},
        "window": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "bandTestBasis": cfg.band_test_basis,
        "totalInvestors": total,
        "totalMatchedDeals": sum(a["matched"] for a in agg_by_investor.values()),
        "results": results,
    }


def _new_agg(investor):
    cat = investor.category if investor.category_id else None
    return {
        "name": investor.name,
        "category_name": cat.name if cat else "",
        "bucket_id": cat.bucket_id if cat else None,
        "bucket_name": cat.bucket.name if cat else "",
        "hq_country": investor.hq_country,
        "avg_ticket": investor.avg_ticket_usd_mn,
        "overall": 0, "in_scope": 0, "matched": 0, "sector_matched": 0,
        "band_elig": 0, "near": 0, "top_sector_volume": 0,
        "near_min_dist": None, "representative": None,
        "avg_scope_size": {}, "_scope_sizes": {},
    }


def _push_scope(agg, tier, row):
    if row.deal_size_usd_mn is not None:
        agg["_scope_sizes"].setdefault(tier, []).append(
            float(row.deal_size_usd_mn))
