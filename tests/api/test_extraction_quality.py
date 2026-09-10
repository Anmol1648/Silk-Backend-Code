"""Document extraction has to produce data the engine can actually score.

Three things it did not. It never asked for a band, so all 24 anchor-scored
parameters -- the qualitative half of the scorecard -- arrived with `pv.band`
empty and were left unscored for good. It never asked for an evidence tier,
writing every document row as tier 1 "Verified" whether it was read off a
slide or derived from two other figures. And it sent the model a bare list of
keys, with none of the definitions, cut-points or band descriptions the config
already holds, so it was asked for twenty values with no idea what any of them
meant or how precise the answer had to be.
"""
from unittest import mock

from django.test import TestCase

from fundos.assessment import extraction as ex


class TheEvidenceTierIsRead(TestCase):
    """The tier is the model's to report, not a constant to assume."""

    def test_the_four_tiers_map_to_their_stored_ranks(self):
        self.assertEqual(ex._evidence_tier("Verified"), 1)
        self.assertEqual(ex._evidence_tier("Management"), 2)
        self.assertEqual(ex._evidence_tier("Estimate"), 3)

    def test_a_tier_is_read_case_and_space_insensitively(self):
        self.assertEqual(ex._evidence_tier("  verified "), 1)
        self.assertEqual(ex._evidence_tier("MANAGEMENT"), 2)

    def test_not_evidenced_does_not_score(self):
        self.assertIsNone(ex._evidence_tier("Not Evidenced"))

    def test_a_number_is_refused_rather_than_mapped(self):
        """A number here is a citation's source tier, a different scale.

        "Source tier 1" says the document was a filing, not that this
        parameter was verified against it. There is no defensible mapping, so
        the row is excluded rather than admitted on provenance nobody stated.
        """
        for value in ("1", 1, "3", 2.0):
            self.assertIsNone(ex._evidence_tier(value), value)

    def test_an_unknown_tier_excludes_rather_than_admits(self):
        """An allow-list, so a typo narrows the assessment visibly."""
        for value in ("", None, "verifed", "high", "tier one"):
            self.assertIsNone(ex._evidence_tier(value), value)

    def test_only_verified_and_management_are_scoring_tiers(self):
        self.assertEqual(tuple(ex.SCORING_TIERS), (1, 2))


class TheBandIsValidated(TestCase):

    def test_the_four_bands_are_accepted_however_they_are_cased(self):
        self.assertEqual(ex._band("Excellent"), "Excellent")
        self.assertEqual(ex._band("good"), "Good")
        self.assertEqual(ex._band("FAIR"), "Fair")
        self.assertEqual(ex._band(" poor "), "Poor")

    def test_exceptional_is_refused(self):
        """Override-only. A model may not mint a 10/10 with no override record."""
        self.assertIsNone(ex._band("Exceptional"))

    def test_anything_else_is_refused(self):
        for value in ("", None, "Very Good", "9", "A"):
            self.assertIsNone(ex._band(value), value)


class ThePromptSpecifiesEachParameter(TestCase):
    """The config already held all of this and none of it was ever sent."""

    def setUp(self):
        from django.core.management import call_command
        call_command("seed_assessment_config", config_version=1,
                     activate=True, verbosity=0)
        # The prompt is built from these three prose fields, and the config
        # seed ships none of them.
        call_command("seed_parameter_dictionary", verbosity=0)

    def _block(self, key, stage="Series A"):
        from fundos.assessment.models import ConfigParameter
        cfg = ConfigParameter.objects.filter(input_key=key,
                                             is_active=True).first()
        self.assertIsNotNone(cfg, key)
        anchors, rubrics = ex._config_for([cfg], stage, None)
        return ex._parameter_block(cfg, stage, anchors, rubrics)

    def test_an_anchor_row_carries_its_four_band_definitions(self):
        block = self._block("ANC_FDR_EDU")
        self.assertIn('Report "band"', block)
        for band in ("Excellent:", "Good:", "Fair:", "Poor:"):
            self.assertIn(band, block)

    def test_an_anchor_row_says_to_choose_the_band_below_when_unclear(self):
        self.assertIn("choose the one below it", self._block("ANC_FDR_EDU"))

    def test_a_numeric_row_carries_the_cut_points_for_the_stage(self):
        """Without them the model cannot tell which precision matters."""
        block = self._block("TEAM_FDR_EXP", "Series A")
        self.assertIn('Report "value"', block)
        self.assertIn("Excellent", block)
        self.assertIn("Series A", block)

    def test_the_cut_points_follow_the_stage(self):
        seed = self._block("TEAM_FDR_EXP", "Seed")
        growth = self._block("TEAM_FDR_EXP", "Growth")
        self.assertIn("Seed", seed)
        self.assertIn("Growth", growth)
        self.assertNotEqual(seed, growth)

    def test_a_numeric_row_is_not_asked_for_a_band(self):
        self.assertNotIn('Report "band"', self._block("TEAM_FDR_EXP"))

    def test_every_block_carries_the_definition_and_where_to_look(self):
        block = self._block("ANC_FDR_EDU")
        self.assertIn("Definition:", block)
        self.assertIn("Where to look:", block)
        self.assertIn("If the data is missing:", block)


class TheSystemPromptStatesTheContract(TestCase):

    def test_it_names_the_four_evidence_tiers(self):
        for tier in ("Verified", "Management", "Estimate", "Not Evidenced"):
            self.assertIn(tier, ex.EXTRACTION_SYSTEM)

    def test_it_says_which_tiers_score(self):
        self.assertIn("excluded", ex.EXTRACTION_SYSTEM.lower())

    def test_it_forbids_scoring(self):
        self.assertIn("DO NOT SCORE", ex.EXTRACTION_SYSTEM)

    def test_it_asks_for_a_locator_and_a_quote(self):
        self.assertIn("locator", ex.EXTRACTION_SYSTEM)
        self.assertIn("quote", ex.EXTRACTION_SYSTEM)

    def test_it_tells_the_model_a_gap_costs_the_company_nothing(self):
        """The line that stops a guess being offered in place of a blank."""
        self.assertIn("costs the company nothing", ex.EXTRACTION_SYSTEM)


class TheSectorTaxonomyResolves(TestCase):
    """Categories E and F are 30% of the rating and read cohort names."""

    def setUp(self):
        from django.core.management import call_command
        call_command("seed_sector_taxonomy", verbosity=0)

    def test_the_full_taxonomy_is_loaded(self):
        from fundos.assessment.models import SectorDealData, SectorMapping
        self.assertEqual(
            SectorDealData.objects.filter(level="sector").count(), 18)
        self.assertEqual(
            SectorDealData.objects.filter(level="sub_sector").count(), 50)
        self.assertGreaterEqual(SectorMapping.objects.count(), 104)

    def test_every_sub_sector_row_names_its_own_clubbed_group(self):
        """Resolution step 2 matches on clubbed_group; blank disables it."""
        from fundos.assessment.models import SectorDealData
        blank = SectorDealData.objects.filter(
            level="sub_sector", clubbed_group="").count()
        self.assertEqual(blank, 0)

    def test_every_mapping_target_has_a_deal_row_to_score_against(self):
        """A group that resolves but holds no deal data still scores blank."""
        from fundos.assessment.models import SectorDealData, SectorMapping
        have = set(SectorDealData.objects.filter(
            level="sub_sector").values_list("name", flat=True))
        targets = set(SectorMapping.objects.values_list("clubbed_group",
                                                        flat=True))
        self.assertFalse(targets - have, "mapping points at a missing cohort")

    def test_the_extraction_vocabulary_is_not_empty(self):
        """An empty closed list is why the model answered in free text.

        The prompt tells the model to copy a sector character for character
        from `sector_vocabulary`. With no rows loaded that list is empty, the
        instruction closes over nothing, and the model falls back to
        describing the company -- "Healthcare", "Digital Health" -- which
        matches no cohort and silently removes 30% of the scorecard.
        """
        from fundos.profile.assessment_extraction import vocabulary
        sectors, groups = vocabulary()
        self.assertEqual(len(sectors), 18)
        self.assertEqual(len(groups), 50)

    def test_the_reported_label_now_resolves_both_halves(self):
        from fundos.profile.assessment_extraction import (resolve_sector,
                                                          resolve_sub_sector)
        for label in ("Healthcare", "Digital Health", "Healthtech"):
            self.assertEqual(resolve_sector(label), "Healthtech", label)
            group, how = resolve_sub_sector(label)
            self.assertEqual(group, "Healthtech", label)
            self.assertNotEqual(how, "unresolved", label)

    def test_a_label_naming_no_known_market_stays_blank(self):
        """Blank stays blank: a wrong cohort is worse than none."""
        from fundos.profile.assessment_extraction import (resolve_sector,
                                                          resolve_sub_sector)
        self.assertIsNone(resolve_sector("Sponge Manufacturing"))
        self.assertEqual(resolve_sub_sector("Sponge Manufacturing"),
                         (None, "unresolved"))

    def test_the_health_cohorts_carry_deal_data(self):
        """The 17 groups that were missing included four health cohorts."""
        from fundos.assessment.models import SectorDealData
        for name in ("Healthtech", "Healthcare Services", "Healthcare SaaS",
                     "Medtech", "Lifescience"):
            row = SectorDealData.objects.filter(level="sub_sector",
                                                name=name).first()
            self.assertIsNotNone(row, name)
            self.assertTrue(row.deal_count, name)
