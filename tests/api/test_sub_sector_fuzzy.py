"""A sub-sector is a join key, and a wrong one is worse than a blank one.

A live Zyla Health run recorded this:

    sub-sector 'Digital Health' matched the benchmark group 'Edtech'
    (fuzzy:85)

The model answered correctly — "Digital Health" is what Zyla is. The RESOLVER
corrupted it: the alias row that maps it to Healthtech was missing from that
database, so it fell through to `fuzz.WRatio` at a threshold of 85 and landed
on education technology. A healthcare company was then benchmarked against
EdTech peers, and Category F is a fifth of the rating.

Nothing flagged it, because Edtech is a real group. That is the whole problem
with this class of bug: the wrong answer is indistinguishable from a right
one, where an unresolved sub-sector is a visible gap that redistributes its
weight.

WRatio is deliberately generous — partial alignments, token sorting,
rescaling — which is right for correcting a typo and wrong for a join key.
These tests stub the scorer so the DECISION is exercised on any machine,
including one where rapidfuzz is not installed (as the development box was,
which is why this never reproduced locally).
"""
import sys
import types
from unittest import mock

from django.core.management import call_command
from django.test import TestCase


def _fake_rapidfuzz(wratio, token_sort):
    """A rapidfuzz stand-in returning the scores a test wants to try."""
    module = types.ModuleType("rapidfuzz")
    fuzz = types.SimpleNamespace(
        WRatio=lambda *a, **k: wratio,
        token_sort_ratio=lambda *a, **k: token_sort)
    process = types.SimpleNamespace(
        extractOne=lambda text, labels, scorer=None: (labels[0], wratio, 0)
        if labels else None)
    module.fuzz = fuzz
    module.process = process
    return module


class TheResolverRefusesAConfidentMismatch(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        self._seed_groups()

    def _seed_groups(self):
        from fundos.assessment.models import SectorDealData

        for name in ("Edtech", "Healthtech"):
            SectorDealData.objects.get_or_create(
                level="sub_sector", name=name,
                defaults={"clubbed_group": name, "deal_count": 10,
                          "sample_quality": "OK",
                          "source_file": "test_fixture.json"})

    def _resolve(self, label, wratio, token_sort):
        """Resolve with the scorers pinned to the given values."""
        from fundos.profile import assessment_extraction

        with mock.patch.dict(sys.modules,
                             {"rapidfuzz": _fake_rapidfuzz(wratio,
                                                           token_sort)}):
            return assessment_extraction.resolve_sub_sector(label)

    def test_the_reported_defect_is_refused(self):
        """WRatio said 85. The words agree nowhere near that."""
        group, method = self._resolve("Digital Health", wratio=85,
                                      token_sort=30)
        self.assertIsNone(group)
        self.assertEqual(method, "unresolved")

    def test_a_genuine_near_miss_still_resolves(self):
        """A typo or a suffix is what fuzzy matching is FOR."""
        group, method = self._resolve("Healthtech platform", wratio=92,
                                      token_sort=80)
        self.assertIsNotNone(group)
        self.assertTrue(method.startswith("fuzzy:"))

    def test_a_low_wratio_is_refused_as_before(self):
        group, _ = self._resolve("Underwater Basketry", wratio=40,
                                 token_sort=20)
        self.assertIsNone(group)

    def test_the_agreement_floor_is_what_rejects_it(self):
        """Same WRatio either side of the word-agreement floor: the second
        scorer is doing the work, not a raised WRatio threshold."""
        self.assertIsNone(self._resolve("X", wratio=90, token_sort=50)[0])
        self.assertIsNotNone(self._resolve("X", wratio=90, token_sort=75)[0])

    def test_the_refusal_is_logged_with_both_scores(self):
        """A silent refusal is a gap nobody can act on. The log names the
        pairing so an administrator can add the alias if it is really right."""
        with self.assertLogs("fundos.profile.assessment_extraction",
                             level="WARNING") as captured:
            self._resolve("Digital Health", wratio=85, token_sort=30)
        joined = " ".join(captured.output)
        self.assertIn("Digital Health", joined)
        self.assertIn("UNRESOLVED", joined)
        self.assertIn("SectorMapping", joined)


class AnExactAliasAlwaysWinsOverFuzzy(TestCase):
    """The fuzzy path is a last resort. When the alias exists, it never runs
    — which is why the development box, whose table HAS the Digital Health
    row, could not reproduce the production failure."""

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        from fundos.assessment.models import SectorDealData, SectorMapping

        SectorDealData.objects.get_or_create(
            level="sub_sector", name="Healthtech",
            defaults={"clubbed_group": "Healthtech", "deal_count": 10,
                      "sample_quality": "OK", "source_file": "t.json"})
        SectorMapping.objects.get_or_create(
            raw_label="Digital Health",
            defaults={"clubbed_group": "Healthtech"})

    def test_the_alias_resolves_without_any_fuzzy_scorer(self):
        from fundos.profile.assessment_extraction import resolve_sub_sector

        # No rapidfuzz in sys.modules at all: if the alias did not win, this
        # would fall through to an ImportError path and return unresolved.
        with mock.patch.dict(sys.modules, {"rapidfuzz": None}):
            group, method = resolve_sub_sector("Digital Health")
        self.assertEqual(group, "Healthtech")
        self.assertEqual(method, "exact")

    def test_seeding_the_taxonomy_is_what_fixes_the_live_company(self):
        """The code change stops the NEXT company being mismatched; the alias
        row is what fixes this one."""
        from fundos.assessment.models import SectorMapping

        self.assertTrue(SectorMapping.objects.filter(
            raw_label__iexact="Digital Health").exists())


class TheThresholdsAreStatedNotScattered(TestCase):

    def test_both_floors_are_named_constants(self):
        from fundos.profile import assessment_extraction

        self.assertEqual(assessment_extraction._FUZZY_ACCEPT, 85)
        self.assertEqual(assessment_extraction._FUZZY_AGREEMENT, 70)
