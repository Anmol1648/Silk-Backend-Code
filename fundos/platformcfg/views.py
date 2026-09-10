"""
Public configuration endpoints.

These serve admin-owned configuration to the client so the frontend never
hard-codes branding, stage numbering, labels or reference data. An admin
changes a value in Django Admin and the UI reflects it on next load —
no release.
"""
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class AppConfigView(APIView):
    """Bootstrap payload: branding + the stage journey.

    Unauthenticated — the login page needs branding before a token exists.
    Contains no tenant or user data.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        from fundos.platformcfg.services import brand, stages

        return Response({
            "brand": brand(),
            "stages": stages(),
        })


class CountriesView(APIView):
    """Country master for the HQ dropdown (PRD §5.2)."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        from fundos.platformcfg.models import Country

        rows = Country.objects.filter(is_active=True).order_by("sort_order", "name")
        return Response({"items": [{
            "iso2": c.iso2,
            "iso3": c.iso3,
            "name": c.name,
            "dialCode": c.dial_code,
            "homeCurrency": c.home_currency,
        } for c in rows]})


class BucketsView(APIView):
    """The seven fund-raise values (C9)."""

    def get(self, request):
        from fundos.platformcfg.services import buckets

        return Response({"items": [{
            "bucketNo": b.bucket_no,
            "label": b.label,
            "amountUsd": float(b.amount_usd),
            "typicalStage": b.typical_stage,
            "investorTypes": b.investor_types,
            "dilutionLowPct": float(b.typical_dilution_low_pct),
            "dilutionHighPct": float(b.typical_dilution_high_pct),
            "description": b.description,
        } for b in buckets()]})


class ProfileSectionsConfigView(APIView):
    """Section registry so the client renders whatever the admin configured."""

    def get(self, request):
        from fundos.platformcfg.services import profile_sections

        return Response({"items": [{
            "sectionKey": s.section_key,
            "label": s.label,
            "description": s.description,
            "kind": s.kind,
            "sortOrder": s.sort_order,
            "isEditable": s.is_editable,
            "isRegenerable": bool(s.is_regenerable and s.llm_role),
            "requiredForCreation": s.required_for_creation,
            "requiredForCompleteness": s.required_for_completeness,
        } for s in profile_sections()]})


class LookupsView(APIView):
    """All dropdown master data in one call (Req 1).

    Returns sectors, sub_sectors, funding_statuses, revenue_sizes,
    currencies, unit_scales and countries from the dedicated lookup
    tables so the profile page never hard-codes its select options.

    Authenticated — the token tells us the user exists, and the response
    contains no tenant-specific data so there is nothing to scope.
    """

    def get(self, request):
        from fundos.platformcfg.lookup_models import (
            Currency, FundingStatus, RevenueSize, Sector, SubSector,
        )
        from fundos.platformcfg.models import Country

        sectors = list(
            Sector.objects.filter(is_active=True)
            .values("id", "name"))
        sub_sectors = list(
            SubSector.objects.filter(is_active=True)
            .values("id", "name"))
        funding_statuses = list(
            FundingStatus.objects.all()
            .values("id", "name", "sort_order"))
        revenue_sizes = list(
            RevenueSize.objects.all()
            .values("id", "name", "sort_order"))
        currencies = list(
            Currency.objects.all()
            .values("id", "code", "symbol"))

        # Unit scales: denomination labels for monetary inputs (Req 2).
        unit_scales = [
            {"name": "K", "label": "Thousand"},
            {"name": "L", "label": "Lakh"},
            {"name": "M", "label": "Million"},
            {"name": "Cr", "label": "Crore"},
            {"name": "B", "label": "Billion"},
        ]

        # Countries: reuse the same shape as CountriesView.
        countries = [
            {"id": c["id"], "name": c["name"], "code": c["iso2"],
             "homeCurrency": c["home_currency"]}
            for c in Country.objects.filter(is_active=True)
            .order_by("sort_order", "name")
            .values("id", "iso2", "name", "home_currency")
        ]

        return Response({
            "success": True,
            "data": {
                "countries": countries,
                "sectors": sectors,
                "sub_sectors": sub_sectors,
                "funding_statuses": funding_statuses,
                "revenue_sizes": revenue_sizes,
                "currencies": currencies,
                "unit_scales": unit_scales,
            },
        })


class UiCopyView(APIView):
    """Admin-editable user-visible strings."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        from fundos.platformcfg.models import UiCopy

        rows = UiCopy.objects.filter(is_active=True)
        return Response({"items": {r.key: r.text for r in rows}})
