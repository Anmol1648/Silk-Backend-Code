"""Investor matching and refresh-pipeline tests.

The most important test in this file is `MergePreservesRelationshipsTests`. The
relationship block is human-entered and not recoverable from any source; if a
refresh ever overwrites it the firm loses something a scraper cannot rebuild.
That is asserted rather than trusted to code review.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase

from fundos.investors import identity, matching
from fundos.investors.models import (CanonicalSector, FundingDeal, Investor,
                                     InvestorBucket, InvestorCategory,
                                     InvestorDeal, MatchConfig, RaiseBand)
from fundos.investors.pipeline import (dedup_key, merge_investor,
                                       parse_amount_usd_mn, parse_date,
                                       recompute_investor_aggregates,
                                       sanitize_date_string, split_investors)


def _bands():
    RaiseBand.objects.create(raise_size_usd_mn=Decimal("0.25"),
                             min_cheque_usd_mn=Decimal("0.05"),
                             max_cheque_usd_mn=Decimal("0.50"))
    RaiseBand.objects.create(raise_size_usd_mn=Decimal("1"),
                             min_cheque_usd_mn=Decimal("0.50"),
                             max_cheque_usd_mn=Decimal("2"))
    RaiseBand.objects.create(raise_size_usd_mn=Decimal("5"),
                             min_cheque_usd_mn=Decimal("2"),
                             max_cheque_usd_mn=Decimal("7.5"))


class RaiseBandLookupTests(TestCase):
    """Exact match or next smaller, with a floor at the first row."""

    def setUp(self):
        _bands()

    def test_exact_match(self):
        band = matching.resolve_band(Decimal("5"))
        self.assertEqual(band.min_usd_mn, Decimal("2"))
        self.assertEqual(band.max_usd_mn, Decimal("7.5"))

    def test_next_smaller(self):
        # $3M sits between the $1M and $5M rows, so the $1M row applies.
        band = matching.resolve_band(Decimal("3"))
        self.assertEqual(band.max_usd_mn, Decimal("2"))

    def test_below_smallest_falls_back_to_first_row(self):
        band = matching.resolve_band(Decimal("0.01"))
        self.assertEqual(band.min_usd_mn, Decimal("0.05"))

    def test_above_largest_uses_largest(self):
        # $40M has no configured band. It resolves to the largest rather than
        # failing — but this is exactly the gap flagged for the business: the
        # table needs Series B and Growth rows.
        band = matching.resolve_band(Decimal("40"))
        self.assertEqual(band.max_usd_mn, Decimal("7.5"))

    def test_centre_never_zero(self):
        """The ticket-fit divisor must never be zero, whatever the config."""
        RaiseBand.objects.all().delete()
        RaiseBand.objects.create(raise_size_usd_mn=Decimal("0"),
                                 min_cheque_usd_mn=Decimal("0"),
                                 max_cheque_usd_mn=Decimal("0"))
        band = matching.resolve_band(Decimal("0"))
        self.assertGreater(band.centre, 0)


class RelScoreTests(TestCase):
    """The two factors that were missing from the written spec."""

    def setUp(self):
        _bands()
        self.cfg = MatchConfig(activity_weight=Decimal("0.7"),
                               ticket_fit_weight=Decimal("0.3"),
                               activity_constant=3)
        self.band = matching.resolve_band(Decimal("5"))

    def _agg(self, scope, avg):
        return {"sector_matched": scope, "band_elig": scope, "near": scope,
                "matched": 0, "avg_scope_size": {2: avg, 3: avg, 4: avg}}

    def test_activity_saturates(self):
        """n/(n+3): 3 deals → 0.50, 9 → 0.75, 27 → 0.90.

        Diminishing returns are the point — a prolific investor must not be
        able to crowd out a well-fitting one on volume alone.
        """
        centre = float(self.band.centre)
        for scope, expected_fa in ((3, 0.50), (9, 0.75), (27, 0.90)):
            score = matching._rel_score(3, self._agg(scope, centre), self.band,
                                        self.cfg)
            # Perfect ticket fit, so RelScore = 100 * (0.7*fa + 0.3*1)
            self.assertAlmostEqual(float(score),
                                   100 * (0.7 * expected_fa + 0.3), places=4)

    def test_ticket_fit_is_scale_free(self):
        """Distance is normalised by the midpoint, so it is scale-invariant."""
        centre = float(self.band.centre)
        near = matching._rel_score(3, self._agg(9, centre), self.band, self.cfg)
        far = matching._rel_score(3, self._agg(9, centre * 3), self.band,
                                  self.cfg)
        self.assertGreater(near, far)

    def test_tier_one_scores_zero_relscore(self):
        """RelScore applies only from tier 2 down; tiers 1-2 rank on volume."""
        self.assertEqual(
            matching._rel_score(1, self._agg(5, 4.75), self.band, self.cfg),
            Decimal(0))

    def test_weights_are_configurable(self):
        cfg = MatchConfig(activity_weight=Decimal("1.0"),
                          ticket_fit_weight=Decimal("0.0"),
                          activity_constant=3)
        score = matching._rel_score(3, self._agg(3, 999999), self.band, cfg)
        self.assertAlmostEqual(float(score), 50.0, places=4)


class TierAssignmentTests(TestCase):
    """Best available tier wins — the strongest evidence decides."""

    def test_best_tier_wins(self):
        agg = {"matched": 1, "sector_matched": 40, "band_elig": 90, "near": 5}
        self.assertEqual(matching._assign_tier(agg), 1)

    def test_falls_through(self):
        self.assertEqual(matching._assign_tier(
            {"matched": 0, "sector_matched": 0, "band_elig": 0, "near": 3}), 4)

    def test_no_evidence_is_tier_zero(self):
        self.assertEqual(matching._assign_tier(
            {"matched": 0, "sector_matched": 0, "band_elig": 0, "near": 0}), 0)


class MatchQueryTests(TestCase):
    """End-to-end over a small fixture universe."""

    def setUp(self):
        _bands()
        MatchConfig.objects.create(name="default", is_active=True,
                                   band_test_basis="deal_size")
        bucket = InvestorBucket.objects.create(name="Domestic VC")
        self.cat = InvestorCategory.objects.create(name="Domestic VC",
                                                   bucket=bucket)
        self.sector = CanonicalSector.objects.create(name="Consumer",
                                                     level="sector")
        self.sub = CanonicalSector.objects.create(name="D2C",
                                                  level="sub_sector",
                                                  parent=self.sector)
        self.other_sub = CanonicalSector.objects.create(
            name="Marketplace", level="sub_sector", parent=self.sector)

    def _deal(self, investor, size, date, sub=None, sector=None):
        fd = FundingDeal.objects.create(
            company_name=f"Co{size}{date}", deal_date=date,
            amount_usd_mn=Decimal(str(size)),
            sector=sector or self.sector, sub_sector=sub or self.sub,
            dedup_key=dedup_key(f"Co{size}{date}{investor.name}", date, size))
        return InvestorDeal.objects.create(
            investor=investor, funding_deal=fd, deal_date=date,
            deal_size_usd_mn=Decimal(str(size)),
            sector=fd.sector, sub_sector=fd.sub_sector,
            company_name=fd.company_name, investors_in_deal=1,
            fundraise_per_investor=Decimal(str(size)))

    def _investor(self, name):
        return Investor.objects.create(
            name=name, normalised_name=identity.normalise_name(name),
            category=self.cat)

    def test_tier_one_requires_subsector_band_and_date(self):
        inv = self._investor("Alpha")
        self._deal(inv, 5, dt.date(2025, 1, 1))
        result = matching.run_match(matching.MatchQuery(
            raise_size_usd_mn=Decimal("5"),
            sub_sector_ids=[str(self.sub.id)],
            date_from=dt.date(2024, 1, 1), date_to=dt.date(2026, 1, 1)))
        row = result["results"][0]
        self.assertEqual(row["tier"], 1)
        self.assertEqual(row["trace"]["matchedDeals"], 1)

    def test_out_of_band_deal_drops_to_tier_four(self):
        """In date, outside the cheque band — near, not matched."""
        inv = self._investor("Beta")
        self._deal(inv, 50, dt.date(2025, 1, 1))
        result = matching.run_match(matching.MatchQuery(
            raise_size_usd_mn=Decimal("5"),
            sub_sector_ids=[str(self.sub.id)],
            date_from=dt.date(2024, 1, 1), date_to=dt.date(2026, 1, 1)))
        self.assertEqual(result["results"][0]["tier"], 4)

    def test_out_of_date_window_is_excluded_entirely(self):
        inv = self._investor("Gamma")
        self._deal(inv, 5, dt.date(2019, 1, 1))
        result = matching.run_match(matching.MatchQuery(
            raise_size_usd_mn=Decimal("5"),
            sub_sector_ids=[str(self.sub.id)],
            date_from=dt.date(2024, 1, 1), date_to=dt.date(2026, 1, 1)))
        self.assertEqual(result["results"], [])

    def test_empty_subsector_filter_means_no_restriction(self):
        """An unticked master box includes everything.

        Getting this backwards returns zero rows silently, which is why it is
        tested rather than assumed.
        """
        inv = self._investor("Delta")
        self._deal(inv, 5, dt.date(2025, 1, 1), sub=self.other_sub)
        result = matching.run_match(matching.MatchQuery(
            raise_size_usd_mn=Decimal("5"), sub_sector_ids=[],
            date_from=dt.date(2024, 1, 1), date_to=dt.date(2026, 1, 1)))
        self.assertEqual(result["results"][0]["tier"], 1)

    def test_band_boundaries_are_inclusive(self):
        """The workbook uses >= and <=; both edges must qualify."""
        for size in (2, 7.5):
            Investor.objects.all().delete()
            inv = self._investor(f"Edge{size}")
            self._deal(inv, size, dt.date(2025, 1, 1))
            result = matching.run_match(matching.MatchQuery(
                raise_size_usd_mn=Decimal("5"),
                sub_sector_ids=[str(self.sub.id)],
                date_from=dt.date(2024, 1, 1), date_to=dt.date(2026, 1, 1)))
            self.assertEqual(result["results"][0]["tier"], 1,
                             f"deal size {size} should be in band")

    def test_tier_one_ranks_on_overall_volume_then_matches(self):
        big = self._investor("BigFund")
        small = self._investor("SmallFund")
        self._deal(big, 5, dt.date(2025, 1, 1))
        for i in range(6):
            self._deal(big, 5, dt.date(2025, 2, i + 1), sub=self.other_sub)
        self._deal(small, 5, dt.date(2025, 1, 2))
        result = matching.run_match(matching.MatchQuery(
            raise_size_usd_mn=Decimal("5"),
            sub_sector_ids=[str(self.sub.id)],
            date_from=dt.date(2024, 1, 1), date_to=dt.date(2026, 1, 1)))
        self.assertEqual(result["results"][0]["name"], "BigFund")

    def test_trace_explains_the_rank(self):
        """"Why is this one fourth" must be answerable from the payload."""
        inv = self._investor("Traced")
        self._deal(inv, 5, dt.date(2025, 1, 1))
        row = matching.run_match(matching.MatchQuery(
            raise_size_usd_mn=Decimal("5"),
            sub_sector_ids=[str(self.sub.id)],
            date_from=dt.date(2024, 1, 1),
            date_to=dt.date(2026, 1, 1)))["results"][0]
        for key in ("overallDeals", "matchedDeals", "bandEligibleDeals",
                    "relScore", "fillScore", "bandCentre"):
            self.assertIn(key, row["trace"])


class MergePreservesRelationshipsTests(TestCase):
    """The pipeline must never write relationship data.

    Contact names, mobiles, meeting history and relationship warmth are
    entered by humans and cannot be rebuilt from any source. A refresh that
    overwrites them is a bug, and this is the assertion that catches it.
    """

    def setUp(self):
        bucket = InvestorBucket.objects.create(name="Global VC")
        self.cat = InvestorCategory.objects.create(name="Global VC",
                                                   bucket=bucket)
        self.investor = Investor.objects.create(
            name="Relationship Fund", normalised_name="relationship fund",
            rel_contact_name="A Partner", rel_mobile="+91 90000 00000",
            rel_relationship="Warm", rel_met=True,
            rel_emails=["partner@example.com"], rel_owner="Our Lead",
            rel_notes="Met at a conference in 2024.")

    def test_master_attributes_are_written(self):
        changed = merge_investor(self.investor,
                                 {"category": self.cat, "hq_city": "Menlo Park",
                                  "hq_country": "USA"})
        self.investor.refresh_from_db()
        self.assertEqual(self.investor.hq_city, "Menlo Park")
        self.assertIn("category", changed)

    def test_relationship_fields_are_refused(self):
        before = {f: getattr(self.investor, f)
                  for f in Investor.RELATIONSHIP_FIELDS}
        merge_investor(self.investor, {
            "hq_country": "USA",
            "rel_contact_name": "SCRAPED NAME",
            "rel_mobile": "+1 555 0000",
            "rel_relationship": "Cold",
            "rel_emails": ["scraped@example.com"],
            "rel_notes": "overwritten by pipeline",
        })
        self.investor.refresh_from_db()
        for field, original in before.items():
            self.assertEqual(getattr(self.investor, field), original,
                             f"pipeline overwrote protected field {field}")
        # The permitted attribute still landed.
        self.assertEqual(self.investor.hq_country, "USA")

    def test_identity_merge_keeps_relationship_data_from_both(self):
        """A merge absorbs relationship data rather than discarding it.

        Someone typed it somewhere and it is not recoverable, so the surviving
        record takes anything the duplicate had that it lacks.
        """
        dup = Investor.objects.create(
            name="Relationship Fund LLP",
            normalised_name=identity.normalise_name("Relationship Fund LLP"),
            rel_contact_title="Managing Partner")
        from fundos.investors.models import InvestorAliasCandidate
        cand = InvestorAliasCandidate.objects.create(
            raw_name="Relationship Fund LLP", candidate=self.investor,
            fuzzy_score=Decimal("88"))
        identity.merge(cand)
        self.investor.refresh_from_db()
        self.assertEqual(self.investor.rel_contact_name, "A Partner")
        self.assertEqual(self.investor.rel_contact_title, "Managing Partner")
        self.assertFalse(Investor.objects.filter(id=dup.id).exists())


class IdentityResolutionTests(TestCase):
    """Below 90% must NOT auto-merge — it queues for a human."""

    def test_exact_normalised_match(self):
        Investor.objects.create(name="Accel",
                                normalised_name=identity.normalise_name("Accel"))
        inv, action = identity.resolve("Accel LLP")
        self.assertEqual(action, "exact")
        self.assertEqual(inv.name, "Accel")

    def test_legal_suffixes_are_stripped(self):
        self.assertEqual(identity.normalise_name("Foundamental GmbH"),
                         identity.normalise_name("Foundamental"))

    def test_ampersand_and_and_are_equivalent(self):
        self.assertEqual(
            identity.normalise_name("Michael & Susan Dell Foundation"),
            identity.normalise_name("Michael and Susan Dell Foundation"))

    def test_regional_tokens_are_not_stripped(self):
        """'India' distinguishes an arm from its parent — it must survive.

        Stripping it would auto-merge exactly the cases that most need a human.
        """
        self.assertNotEqual(identity.normalise_name("Vertex Ventures India"),
                            identity.normalise_name("Vertex Ventures"))

    def test_near_miss_creates_investor_and_queues_candidate(self):
        from fundos.investors.models import InvestorAliasCandidate
        Investor.objects.create(
            name="Blume Ventures",
            normalised_name=identity.normalise_name("Blume Ventures"))
        inv, action = identity.resolve("Blume Venture Partners")
        self.assertEqual(action, "created")
        self.assertTrue(
            InvestorAliasCandidate.objects.filter(
                raw_name="Blume Venture Partners", status="pending").exists(),
            "a below-threshold near miss must be queued, not silently merged")


class ParsingTests(TestCase):
    """Ported behaviour from the supplied config.py, asserted."""

    def test_amount_units(self):
        self.assertEqual(parse_amount_usd_mn("$15 Mn"), Decimal("15"))
        self.assertEqual(parse_amount_usd_mn("150K"), Decimal("0.15"))
        self.assertEqual(parse_amount_usd_mn("$1.5 Bn"), Decimal("1500"))
        self.assertIsNone(parse_amount_usd_mn("-"))

    def test_date_typo_fixes_apply_before_truncation(self):
        """'20205' must become 2025, not be clipped to 2020."""
        self.assertEqual(sanitize_date_string("15 Dec 20205"), "15 Dec 2025")
        self.assertEqual(sanitize_date_string("29 Nov 2025**"), "29 Nov 2025")
        self.assertEqual(parse_date("15 Dec 20205"), dt.date(2025, 12, 15))

    def test_dedup_key_survives_format_drift(self):
        a = dedup_key("Acme", dt.date(2025, 1, 1), 280)
        b = dedup_key("  ACME ", dt.date(2025, 1, 1), Decimal("280.00"))
        self.assertEqual(a, b)

    def test_investor_split_drops_non_entities(self):
        """'Undisclosed' must not become an investor.

        Left in, it accumulates into a phantom entity with hundreds of deals
        that ranks first on every tier-1 query.
        """
        parts = split_investors(
            "Accel, Blume Ventures and Undisclosed, others, Accel")
        self.assertEqual(parts, ["Accel", "Blume Ventures"])


class AggregateTests(TestCase):
    """Average ticket excludes zero-size deals rather than counting them."""

    def test_avg_ticket_ignores_unpriced_deals(self):
        inv = Investor.objects.create(name="Ticket Fund",
                                      normalised_name="ticket fund")
        sector = CanonicalSector.objects.create(name="S", level="sector")
        for i, size in enumerate([Decimal("4"), Decimal("6"), Decimal("0")]):
            fd = FundingDeal.objects.create(
                company_name=f"C{i}", deal_date=dt.date(2025, 1, i + 1),
                amount_usd_mn=size, sector=sector,
                dedup_key=f"k{i}")
            InvestorDeal.objects.create(
                investor=inv, funding_deal=fd, deal_date=fd.deal_date,
                deal_size_usd_mn=size, sector=sector, company_name=fd.company_name,
                investors_in_deal=1, fundraise_per_investor=size)
        recompute_investor_aggregates(investor_ids=[inv.id])
        inv.refresh_from_db()
        self.assertEqual(inv.overall_deals, 3)
        # Mean of 4 and 6 — the zero is excluded, not averaged in as zero.
        self.assertEqual(inv.avg_ticket_usd_mn, Decimal("5.0000"))
