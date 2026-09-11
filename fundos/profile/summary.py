"""
Per-company summary fields for the All Companies view (Gap doc §3.2 / §10.1).

GET /me/contexts company-scope items must carry the five fields the All
Companies table renders per row: Company Name, Last Raise, Total Funding
Received, Sector, Attachment Links. Company Name / role already come from
the membership; this module derives the other four from the company's
profile (funding_history + company_profile + documents).

Everything here is defensive: a company without a generated profile simply
yields empty/zero values rather than raising, so the contexts endpoint
never fails because one company is half-set-up.
"""
from decimal import Decimal


def _f(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


# FundingRound.amount_value is stored in USD MILLIONS, which is how the
# profile serializer reads it (`amount_usd_mn: _f(r.amount_value)`) and how
# the section writer accepts it (`amount_usd_mn` -> `amount_value`).
#
# This function previously divided by 1,000,000 on the assumption the column
# held whole dollars, so a $5M round entered as 5 was reported to the
# dashboard as 0.000005 and rounded away to 0.0 — the same figure the profile
# page showed as $5 Mn. One column cannot be both; millions is the convention
# every other reader and writer follows, so this is now consistent with them.
def _to_usd_mn(amount):
    if amount is None:
        return 0.0
    return round(float(amount), 4)


def _round_usd_mn(row):
    """A funding round in USD millions, or 0.0 when it cannot be expressed.

    A round is stored in the currency and scale its source used, so
    `amount_value` alone is 20 (₹20 crore) sitting beside 5 (US$5 Mn).
    """
    from fundos.core.services import money

    usd, _rate = money.reported_usd_mn(
        row.amount_value,
        getattr(row, "amount_ccy", "") or "",
        getattr(row, "amount_denomination", "") or "",
        derived=getattr(row, "amount_usd_mn", None))
    return usd if usd is not None else 0.0


def company_summaries(company_ids):
    """Return {company_id(str): {sector, lastRaise, totalFundingReceivedUsdMn,
    attachmentLinks}} for the given company ids.

    Bulk-computed to avoid an N+1 across the contexts listing.
    """
    from fundos.profile.models import (
        CompanyProfile, FundingRound, ProfileDocument,
    )

    ids = [cid for cid in company_ids if cid]
    if not ids:
        return {}

    profiles = {
        p.company_id: p
        for p in CompanyProfile.objects.filter(company_id__in=ids)
    }
    profile_ids = [p.id for p in profiles.values()]

    # Funding rounds grouped by profile.
    rounds_by_profile = {}
    if profile_ids:
        for r in FundingRound.objects.filter(
                profile_id__in=profile_ids).order_by("-announced_date"):
            rounds_by_profile.setdefault(r.profile_id, []).append(r)

    # Documents grouped by profile (for attachment links).
    docs_by_profile = {}
    if profile_ids:
        for d in ProfileDocument.objects.filter(profile_id__in=profile_ids):
            docs_by_profile.setdefault(d.profile_id, []).append(d)

    # Sector lives on the company_overview/company_profile section structured
    # blob (macro_sector). Pull those section rows in one query.
    sector_by_profile = _sectors_by_profile(profile_ids)

    out = {}
    for cid in ids:
        profile = profiles.get(cid)
        if not profile:
            out[str(cid)] = _empty_summary()
            continue

        rounds = rounds_by_profile.get(profile.id, [])
        last_raise = {"round": "", "date": ""}
        if rounds:
            latest = rounds[0]
            last_raise = {
                "round": latest.round_name or "",
                "date": (latest.announced_date.isoformat()
                         if latest.announced_date else ""),
            }
        # The same rule the profile panel applies, so the card and the page
        # cannot report different totals for the same rounds: each round's
        # USD companion, and a round whose currency has no rate left out
        # rather than added in as though it were dollars.
        total_usd_mn = round(sum(_round_usd_mn(r) for r in rounds), 4)

        out[str(cid)] = {
            "sector": sector_by_profile.get(profile.id, "")
            or "",
            "logoUrl": profile.logo_url or "",
            # The dashboard card branches on this to decide whether to open
            # the profile or resume the deal workspace; it was never sent, so
            # every card always opened the profile.
            "profileComplete": bool(profile.is_complete),
            "profileStatus": profile.status,
            "lastRaise": last_raise,
            "totalFundingReceivedUsdMn": total_usd_mn,
            "attachmentLinks": _attachment_links(
                profile, docs_by_profile.get(profile.id, [])),
        }
    return out


def _empty_summary():
    return {
        "sector": "",
        "logoUrl": "",
        "profileComplete": False,
        "profileStatus": "draft",
        "lastRaise": {"round": "", "date": ""},
        "totalFundingReceivedUsdMn": 0.0,
        "attachmentLinks": {
            "founderProfile": "",
            "companyUrl": "",
            "companyPresentation": "",
            "financialModel": "",
            "annualReportFinancialStatements": "",
            "otherDocuments": [],
        },
    }


def _sectors_by_profile(profile_ids):
    """macro_sector per profile from the company_profile/overview section."""
    if not profile_ids:
        return {}
    from fundos.profile.models import ProfileSection

    out = {}
    rows = ProfileSection.objects.filter(
        profile_id__in=profile_ids,
        section_key__in=["company_profile", "company_overview"],
        is_active=True)
    for s in rows:
        st = s.structured or {}
        sector = ""
        if isinstance(st, dict):
            sector = st.get("macro_sector") or st.get("sub_sector") or ""
        # First non-empty wins per profile.
        if sector and not out.get(s.profile_id):
            out[s.profile_id] = sector
    return out


# Map ProfileDocument categories onto the named attachment slots. Names match
# the four upload categories used by the Create Company modal + upload endpoint
# (§5.2): companyPresentation / financialModel / annualReportFinancialStatements
# / otherDocuments (backend-issues combined report #5).
_CATEGORY_TO_SLOT = {
    "company_presentation": "companyPresentation",
    "financial_model": "financialModel",
    "annual_report": "annualReportFinancialStatements",
}


def _attachment_links(profile, docs):
    links = {
        # Reference links — kept alongside the document categories.
        # CR-02: companyLinkedin is the company's own page. founderProfile is
        # retained for any caller still reading it, but the dashboard shortcut
        # now uses companyLinkedin and hides itself when that is empty, rather
        # than silently substituting a founder's personal profile.
        "companyLinkedin": profile.linkedin_url or "",
        "founderProfile": "",
        "companyUrl": profile.website_url or "",
        # The four upload categories (§5.2), named to match the rest of the API.
        "companyPresentation": "",
        "financialModel": "",
        "annualReportFinancialStatements": "",
        "otherDocuments": [],
    }
    # First founder LinkedIn, if any, populates founderProfile.
    from fundos.profile.models import Founder
    founder = (Founder.objects.filter(profile=profile)
               .exclude(linkedin_url="").order_by("sort_order").first())
    if founder:
        links["founderProfile"] = founder.linkedin_url

    for d in docs:
        slot = _CATEGORY_TO_SLOT.get(d.category)
        url = _doc_download_url(d)
        if slot and not links[slot]:
            links[slot] = url or d.filename or ""
        elif not slot:
            # Anything not in the three single-value categories (incl. the
            # "other" upload category) collects under otherDocuments.
            links["otherDocuments"].append(url or d.filename or "")
    return links


def _doc_download_url(d):
    """Signed URL for a profile document, mirroring _document_payload; never
    raises."""
    if not d.document_id:
        return ""
    try:
        from fundos.docs.models import Document, DocumentVersion
        from fundos.docs.storage import get_storage

        document = Document.objects.filter(id=d.document_id).first()
        version = None
        if document and document.current_version_id:
            version = DocumentVersion.objects.filter(
                id=document.current_version_id).first()
        if version is None and document:
            version = (DocumentVersion.objects
                       .filter(document_id=document.id)
                       .order_by("-version_no").first())
        if version and version.storage_uri:
            return get_storage().signed_url(version.storage_uri)
    except Exception:
        return ""
    return ""
