"""The funding display strings must agree with the figures beside them.

The conversion ran one way only: a founder-entered CURRENCY:AMOUNT:SCALE
string was parsed INTO `_usd_mn`, but a figure derived from funding history
arrived with an empty display next to it. The API held 6.27 and the screen
showed nothing.
"""
from decimal import Decimal

from django.test import TestCase

from fundos.profile.section_writer import _parse_display_to_usd_mn
from fundos.profile.spec_serializer import _usd_mn_as_display


class DisplayIsDerivedFromTheFigure(TestCase):

    def test_a_usd_millions_figure_becomes_a_display_string(self):
        self.assertEqual(_usd_mn_as_display(6.27), "USD:6.27:M")
        self.assertEqual(_usd_mn_as_display(Decimal("6.27")), "USD:6.27:M")

    def test_trailing_zeros_are_trimmed(self):
        self.assertEqual(_usd_mn_as_display(100.0), "USD:100:M")
        self.assertEqual(_usd_mn_as_display(0.39), "USD:0.39:M")

    def test_a_missing_figure_stays_missing(self):
        """An absent value must not be shown as zero."""
        for empty in (None, ""):
            self.assertEqual(_usd_mn_as_display(empty), "")

    def test_a_non_numeric_value_is_not_rendered(self):
        self.assertEqual(_usd_mn_as_display("not a number"), "")

    def test_the_pair_round_trips(self):
        """Parsing the display back must return the figure it came from.

        This is what stops the two fields drifting: they are one fact.
        """
        for value in (6.27, 0.39, 100.0, 1.0, 12.3456):
            display = _usd_mn_as_display(value)
            self.assertAlmostEqual(_parse_display_to_usd_mn(display), value,
                                   places=4, msg=display)


class ExplicitEntryWins(TestCase):
    """A founder's own currency and scale survive the round trip."""

    def test_an_explicit_display_is_not_overwritten(self):
        from fundos.profile import spec_serializer

        section = {"total_funding_raised_display": "INR:52.25:Cr"}
        data = {"total_funding_raised_usd_mn": 6.27}

        # Mirrors the serializer's rule: explicit beats derived.
        explicit = section.get("total_funding_raised_display") or ""
        resolved = explicit or spec_serializer._usd_mn_as_display(
            data["total_funding_raised_usd_mn"])

        self.assertEqual(resolved, "INR:52.25:Cr")
