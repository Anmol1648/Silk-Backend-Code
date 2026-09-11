"""Stage 3 — Investor Discovery API.

Thin views, following the codebase's convention: validate → call service →
serialise. All the logic is in `matching.py` and the filter defaults come from
Stage 1 and Stage 2 data so the screen opens pre-filtered rather than empty.
"""
import datetime as dt
import logging
from decimal import Decimal, InvalidOperation

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fundos.core.api.base import DealScopedAPIView
from fundos.investors import matching
from fundos.investors.models import (CanonicalSector, Investor, InvestorBucket,
                                     InvestorDeal, InvestorShortlist,
                                     MatchConfig, RefreshRun)
from fundos.investors.serializers import (ShortlistSerializer, is_internal,
                                          relationship_block, serialise_match)

logger = logging.getLogger(__name__)


def _dec(value, default=None):
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _ids(request, key):
    raw = request.query_params.getlist(key) or []
    if len(raw) == 1 and "," in raw[0]:
        raw = raw[0].split(",")
    return [r.strip() for r in raw if r.strip()]


class DiscoveryFiltersView(DealScopedAPIView):
    """Filter options plus the AI-prefilled defaults for this deal.

    The screen must open already answering the question, not asking the user
    to configure it. Defaults come from what Stages 1 and 2 already know:
    the deal's sector, its sub-sector, and the recommended raise.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id):
        deal = self.deal
        raise_size, source = self._default_raise(deal)
        sector_ids, sub_ids = self._default_sectors(deal)
        cfg = MatchConfig.active()
        last = RefreshRun.objects.filter(status="completed").first()

        return Response({
            "sectors": [{"id": str(s.id), "name": s.name}
                        for s in CanonicalSector.objects.filter(
                            level="sector", is_active=True)],
            "subSectors": [{"id": str(s.id), "name": s.name,
                            "parentId": str(s.parent_id) if s.parent_id else None}
                           for s in CanonicalSector.objects.filter(
                               level="sub_sector", is_active=True)],
            "buckets": [{"id": str(b.id), "name": b.name}
                        for b in InvestorBucket.objects.all()],
            "defaults": {
                "raiseSizeUsdMn": float(raise_size) if raise_size else None,
                "raiseSource": source,
                "sectorIds": sector_ids,
                "subSectorIds": sub_ids,
                "dateFrom": None,
                "dateTo": None,
                "specialistsOnly": False,
            },
            "universe": {
                "totalDealRows": InvestorDeal.objects.count(),
                "totalInvestors": Investor.objects.filter(is_active=True).count(),
                "dataAsOf": (InvestorDeal.objects.order_by("-deal_date")
                             .values_list("deal_date", flat=True).first()),
                "lastRefreshAt": last.finished_at if last else None,
            },
            "bandTestBasis": cfg.band_test_basis,
        })

    @staticmethod
    def _default_raise(deal):
        """The ask, and where it came from.

        Both branches named fields that do not exist -- `RaiseScenario` holds
        `raise_value`, and `Deal` has no raise column at all -- so this
        always returned ("none") however much the founder had entered. One
        resolver now answers for every consumer of the figure.
        """
        from fundos.profile.current_raise import for_company

        company_id = getattr(deal, "company_id", None)
        if not company_id:
            return None, "none"
        amount, basis = for_company(company_id)
        return amount, (basis or "none")

    @staticmethod
    def _default_sectors(deal):
        sector_ids, sub_ids = [], []
        company = getattr(deal, "company", None)
        for attr, bucket, level in (("sector", sector_ids, "sector"),
                                    ("sub_sector", sub_ids, "sub_sector")):
            name = getattr(company, attr, None) if company else None
            if not name:
                continue
            from fundos.investors import normalizer
            s, _, _ = normalizer.normalize(name, record_misses=False)
            if s and s.level == level:
                bucket.append(str(s.id))
        return sector_ids, sub_ids


class DiscoverySearchView(DealScopedAPIView):
    """Run the match. Every filter is a query parameter; nothing is stored.

    Results are computed per request because the filters are the question —
    caching them would go stale the moment a banker changed the raise size.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id):
        raise_size = _dec(request.query_params.get("raiseSizeUsdMn"))
        if raise_size is None:
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": "raiseSizeUsdMn is required.",
                 "fields": {"raiseSizeUsdMn": "Provide the target raise in USD Mn."}},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            page = max(1, int(request.query_params.get("page", 1)))
            page_size = min(100, max(1, int(request.query_params.get("pageSize", 20))))
        except ValueError:
            page, page_size = 1, 20

        query = matching.MatchQuery(
            raise_size_usd_mn=raise_size,
            sector_ids=_ids(request, "sectorIds"),
            sub_sector_ids=_ids(request, "subSectorIds"),
            date_from=_date(request.query_params.get("dateFrom")),
            date_to=_date(request.query_params.get("dateTo")),
            bucket_ids=_ids(request, "bucketIds"),
            specialists_only=(request.query_params.get("specialistsOnly")
                              in ("1", "true", "True")),
            tenant_id=str(getattr(self.deal, "tenant_id", "") or "") or None,
        )

        tier_filter = request.query_params.get("tier")
        result = matching.run_match(query)
        rows = result["results"]
        if tier_filter in ("1", "2", "3", "4"):
            rows = [r for r in rows if r["tier"] == int(tier_filter)]
        elif tier_filter == "top":
            rows = [r for r in rows if r["tier"] == 1]
        elif tier_filter == "alternatives":
            rows = [r for r in rows if r["tier"] >= 2]

        internal = is_internal(request.user, self.deal)
        total = len(rows)
        start = (page - 1) * page_size
        window = rows[start:start + page_size]

        investors = {}
        if internal:
            investors = {str(i.id): i for i in Investor.objects.filter(
                id__in=[r["investorId"] for r in window])}

        saved = set(InvestorShortlist.objects
                    .filter(deal=self.deal)
                    .values_list("investor_id", flat=True))

        items = []
        for r in window:
            payload = serialise_match(
                r, investor=investors.get(r["investorId"]), internal=internal)
            payload["saved"] = r["investorId"] in {str(s) for s in saved}
            items.append(payload)

        counts = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0}
        for r in result["results"]:
            counts[f"tier{r['tier']}"] = counts.get(f"tier{r['tier']}", 0) + 1

        return Response({
            "items": items,
            "page": page, "pageSize": page_size, "total": total,
            "tierCounts": counts,
            "band": result["band"],
            "window": result["window"],
            "bandTestBasis": result["bandTestBasis"],
            "totalInvestors": result["totalInvestors"],
            "totalMatchedDeals": result["totalMatchedDeals"],
            "audience": "internal" if internal else "founder",
            "savedCount": len(saved),
            # Empty results should name the binding constraint, not just say
            # "no results" — the frontend renders this verbatim.
            "emptyHint": self._empty_hint(result, query) if not rows else None,
        })

    @staticmethod
    def _empty_hint(result, query):
        band = result["band"]
        return (
            f"No investors match a ${band['minUsdMn']:.2f}–"
            f"${band['maxUsdMn']:.2f}M cheque band between "
            f"{result['window']['from']} and {result['window']['to']}. "
            "Widening the date range or clearing the sub-sector filter is "
            "usually what opens this up.")


class InvestorDetailView(DealScopedAPIView):
    """Drawer payload: portfolio, momentum stats, and — internal only — the
    relationship block."""

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id, investor_id):
        try:
            investor = Investor.objects.select_related(
                "category", "category__bucket").get(id=investor_id)
        except Investor.DoesNotExist:
            return Response({"error": "E-NOTFOUND-404",
                             "detail": "Investor not found."},
                            status=status.HTTP_404_NOT_FOUND)

        rows = (InvestorDeal.objects.filter(investor=investor)
                .select_related("funding_deal")
                .order_by("-deal_date")[:50])

        cutoff = dt.date.today() - dt.timedelta(days=365)
        recent = [r for r in rows if r.deal_date and r.deal_date >= cutoff]
        leads = [r for r in rows if r.is_lead]

        payload = {
            "investorId": str(investor.id),
            "name": investor.name,
            "category": investor.category.name if investor.category else "",
            "bucket": (investor.category.bucket.name
                       if investor.category else ""),
            "hqCity": investor.hq_city,
            "hqCountry": investor.hq_country,
            "avgTicketUsdMn": (float(investor.avg_ticket_usd_mn)
                               if investor.avg_ticket_usd_mn else None),
            "overallDeals": investor.overall_deals,
            "stats": {
                "dealsLast12Months": len(recent),
                "leadPercent": (round(100 * len(leads) / len(rows))
                                if rows else 0),
                "lastDealDate": (investor.last_deal_date.isoformat()
                                 if investor.last_deal_date else None),
            },
            "portfolio": [{
                "company": r.company_name,
                "roundType": r.funding_deal.round_type,
                "amountUsdMn": (float(r.deal_size_usd_mn)
                                if r.deal_size_usd_mn else None),
                "date": r.deal_date.isoformat() if r.deal_date else None,
                "isLead": r.is_lead,
            } for r in rows[:20]],
            "classification": {
                "source": investor.classification_source,
                "confidence": (float(investor.classification_confidence)
                               if investor.classification_confidence else None),
            },
        }

        if is_internal(request.user, self.deal):
            payload["relationship"] = relationship_block(investor)

        return Response(payload)


class ShortlistView(DealScopedAPIView):
    """Save-for-outreach. Honestly a shortlist (D-14) — no pipeline stages."""

    permission_classes = [IsAuthenticated]
    required_action = "strategy.edit"

    def get(self, request, deal_id):
        qs = (InvestorShortlist.objects.filter(deal=self.deal)
              .select_related("investor"))
        return Response({"items": ShortlistSerializer(qs, many=True).data,
                         "total": qs.count()})

    def post(self, request, deal_id):
        investor_id = request.data.get("investorId")
        if not investor_id:
            return Response({"error": "E-VALIDATION-400",
                             "detail": "investorId is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            investor = Investor.objects.get(id=investor_id)
        except (Investor.DoesNotExist, ValueError, TypeError):
            return Response({"error": "E-NOTFOUND-404",
                             "detail": "Investor not found."},
                            status=status.HTTP_404_NOT_FOUND)

        obj, created = InvestorShortlist.objects.get_or_create(
            deal=self.deal, investor=investor,
            defaults={"tenant_id": getattr(self.deal, "tenant_id", None),
                      "match_score": _dec(request.data.get("matchScore")),
                      "tier_at_save": request.data.get("tier"),
                      "rationale": request.data.get("rationale", "") or "",
                      "note": request.data.get("note", "") or "",
                      "saved_by": request.user})
        return Response(ShortlistSerializer(obj).data,
                        status=(status.HTTP_201_CREATED if created
                                else status.HTTP_200_OK))

    def delete(self, request, deal_id):
        investor_id = request.query_params.get("investorId")
        InvestorShortlist.objects.filter(
            deal=self.deal, investor_id=investor_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DiscoveryExportView(DealScopedAPIView):
    """Export payload (D-16).

    Excludes relationship data, contact details and full portfolio history
    regardless of who asks — the export leaves our control, so the internal
    view's allowances do not apply to it.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id):
        qs = (InvestorShortlist.objects.filter(deal=self.deal)
              .select_related("investor", "investor__category"))
        as_of = (InvestorDeal.objects.order_by("-deal_date")
                 .values_list("deal_date", flat=True).first())
        return Response({
            "dealName": getattr(self.deal, "name", ""),
            "generatedAt": dt.datetime.utcnow().isoformat() + "Z",
            "dataAsOf": as_of,
            "watermark": "Indicative investor shortlist — not investment advice",
            "items": [{
                "name": s.investor.name,
                "category": (s.investor.category.name
                             if s.investor.category else ""),
                "hqCountry": s.investor.hq_country,
                "avgTicketUsdMn": (float(s.investor.avg_ticket_usd_mn)
                                   if s.investor.avg_ticket_usd_mn else None),
                "matchScore": (float(s.match_score) if s.match_score else None),
                "tier": s.tier_at_save,
                "rationale": s.rationale,
            } for s in qs],
        })


class MatchPreviewView(APIView):
    """Admin-side preview and regression check.

    Running the reference query (D2C, $5M, 2023-01-01 → 2026-06-05) should
    return 301 investors and 477 matched deals — the workbook's own published
    counts. Exposed so an admin can verify after any config change rather than
    discovering a drift weeks later.
    """

    permission_classes = [IsAuthenticated]

    REFERENCE = {"investors": 301, "matchedDeals": 477}

    def get(self, request):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"error": "E-AUTHZ-403"},
                            status=status.HTTP_403_FORBIDDEN)

        query = matching.MatchQuery(
            raise_size_usd_mn=_dec(request.query_params.get("raiseSizeUsdMn"), 5),
            sector_ids=_ids(request, "sectorIds"),
            sub_sector_ids=_ids(request, "subSectorIds"),
            date_from=_date(request.query_params.get("dateFrom")),
            date_to=_date(request.query_params.get("dateTo")))
        result = matching.run_match(query, limit=10)

        matches_reference = (
            result["totalInvestors"] == self.REFERENCE["investors"]
            and result["totalMatchedDeals"] == self.REFERENCE["matchedDeals"])

        return Response({
            "totalInvestors": result["totalInvestors"],
            "totalMatchedDeals": result["totalMatchedDeals"],
            "band": result["band"], "window": result["window"],
            "sample": result["results"],
            "reference": self.REFERENCE,
            "matchesReference": matches_reference,
        })
