"""Data integrity at the model/storage boundary, and the write-provenance rules.

Every test here pins a defect observed on a real generated profile (Zyla
Health, 21 Aug 2026) rather than an imagined one. Each names the failure it
exists to catch, because each of these produced a *plausible* value — a number
in the wrong unit, a citation that resolves today, a founder record that looks
merely thin — and a plausible wrong answer is the kind this system is least
equipped to notice on its own.
"""
from decimal import Decimal

from django.test import TestCase

from fundos.core.scoping import tenant_context
from tests.conftest_helpers import make_world


# ---------------------------------------------------------------------------
# Markdown must not reach a JSON string
# ---------------------------------------------------------------------------
class MarkdownIsStrippedTests(TestCase):
    """These strings are rendered into PDF, PPTX and DOCX, none of which
    interpret markdown, so an asterisk the model added for emphasis reaches an
    investor as literal punctuation."""

    def test_emphasis_headings_and_bullets_are_removed(self):
        from fundos.profile.sanitize import strip_markdown

        source = ("## Overview\n\n"
                  "**Zyla Health** is India's *highest-rated* care platform.\n"
                  "- serves `insurers`\n"
                  "- serves ~~hospitals~~ corporates\n"
                  "> and pharma\n")
        out = strip_markdown(source)
        for marker in ("**", "##", "~~", "`", "> "):
            self.assertNotIn(marker, out, f"{marker!r} survived stripping")
        self.assertIn("Zyla Health", out)
        self.assertIn("highest-rated", out)
        self.assertIn("insurers", out)
        self.assertIn("corporates", out)

    def test_a_link_keeps_both_its_label_and_its_url(self):
        from fundos.profile.sanitize import strip_markdown

        out = strip_markdown("See [the filing](https://mca.gov.in/x) for more.")
        self.assertIn("the filing", out)
        self.assertIn("https://mca.gov.in/x", out)
        self.assertNotIn("](", out)

    def test_arithmetic_and_identifiers_are_not_mangled(self):
        """The failure that matters is not a surviving asterisk, it is a
        mangled figure — and a stripped number is indistinguishable from a
        researched one, with no second copy to check it against."""
        from fundos.profile.sanitize import strip_markdown

        for text in ("EBITDA * 2 = 14", "gross_margin_pct is 62",
                     "revenue * headcount", "a_b_c_d"):
            self.assertEqual(strip_markdown(text), text,
                             f"{text!r} was altered")

    def test_control_characters_are_removed(self):
        from fundos.profile.sanitize import strip_markdown

        out = strip_markdown("clean\x00text\x07here")
        self.assertEqual(out, "cleantexthere")

    def test_normalization_strips_markup_from_a_section(self):
        from fundos.profile import schema

        raw = {"sections": {"company_profile": {"data": {
            "description_of_business": "**Bold** and _italic_ prose.",
        }}}}
        profile = schema.normalize_profile(raw)
        text = profile["sections"]["company_profile"]["data"][
            "description_of_business"]
        self.assertEqual(text, "Bold and italic prose.")


# ---------------------------------------------------------------------------
# A scalar field takes one value
# ---------------------------------------------------------------------------
class SingleTermTaxonomyTests(TestCase):
    """`sub_sector` is matched against the benchmark table by exact name.

    A live profile returned five comma-separated terms; every lookup stage in
    `resolve_sub_sector` — exact, clubbed group, name, then fuzzy at a >=88
    floor — missed it, so the company was scored with no peer comparables and
    nothing said so.
    """

    def test_a_five_term_answer_is_narrowed_to_the_first(self):
        from fundos.profile import schema

        notes = []
        raw = {"sections": {"company_profile": {"data": {
            "sub_sector": ("Telemedicine, Patient Engagement, Medical AI, "
                           "InsurTech, Personalized Care Management"),
        }}}}
        profile = schema.normalize_profile(raw, notes=notes)
        self.assertEqual(
            profile["sections"]["company_profile"]["data"]["sub_sector"],
            "Telemedicine")
        self.assertTrue(any("sub_sector" in n for n in notes),
                        "the narrowing was applied but never reported")

    def test_a_single_term_answer_is_left_alone(self):
        from fundos.profile import schema

        raw = {"sections": {"company_profile": {"data": {
            "sub_sector": "Digital Health"}}}}
        profile = schema.normalize_profile(raw)
        self.assertEqual(
            profile["sections"]["company_profile"]["data"]["sub_sector"],
            "Digital Health")

    def test_an_enumerated_field_snaps_onto_a_permitted_value(self):
        from fundos.profile import schema

        raw = {"sections": {"company_profile": {"data": {
            "funding_status_name": "vc-funded"}}}}
        profile = schema.normalize_profile(raw)
        self.assertEqual(
            profile["sections"]["company_profile"]["data"][
                "funding_status_name"],
            "VC Funded")


# ---------------------------------------------------------------------------
# Citations must outlive the run that produced them
# ---------------------------------------------------------------------------
class CitationUrlTests(TestCase):
    """Grounding redirects expire in roughly a month, so storing one as a news
    citation guarantees a dead link in every export that outlives it."""

    def test_a_grounding_redirect_is_rejected(self):
        from fundos.profile.sanitize import clean_url, is_redirect_url

        url = ("https://vertexaisearch.cloud.google.com/"
               "grounding-api-redirect/AUZIYQE8krsh9iGNcNTH")
        self.assertTrue(is_redirect_url(url))
        cleaned, problem = clean_url(url)
        self.assertEqual(cleaned, "")
        self.assertIn("redirect", problem)

    def test_a_publisher_url_is_kept(self):
        from fundos.profile.sanitize import clean_url

        cleaned, problem = clean_url("https://economictimes.com/a/b")
        self.assertEqual(cleaned, "https://economictimes.com/a/b")
        self.assertEqual(problem, "")

    def test_a_redirect_link_is_dropped_but_the_publisher_survives(self):
        from fundos.profile import schema

        notes = []
        raw = {"sections": {"news": {"data": [{
            "title": "Zyla raises $5M",
            "source": "Signalbase",
            "link": ("https://vertexaisearch.cloud.google.com/"
                     "grounding-api-redirect/AUZIYQE8"),
        }]}}}
        profile = schema.normalize_profile(raw, notes=notes)
        row = profile["sections"]["news"]["data"][0]
        self.assertEqual(row["link"], "")
        self.assertEqual(row["source"], "Signalbase",
                         "the durable citation must survive the dead link")
        self.assertTrue(any("link dropped" in n for n in notes))

    def test_a_script_scheme_never_reaches_a_rendered_field(self):
        from fundos.profile.sanitize import clean_url

        for hostile in ("javascript:alert(1)",
                        "data:text/html;base64,PHNjcmlwdD4=",
                        "file:///etc/passwd"):
            cleaned, problem = clean_url(hostile)
            self.assertEqual(cleaned, "", f"{hostile} was accepted")
            self.assertTrue(problem)

    def test_a_private_host_is_refused(self):
        from fundos.profile.sanitize import clean_url

        for hostile in ("http://127.0.0.1/admin", "http://192.168.1.1/",
                        "http://169.254.169.254/latest/meta-data/"):
            cleaned, _ = clean_url(hostile)
            self.assertEqual(cleaned, "", f"{hostile} was accepted")

    def test_a_supplied_logo_url_is_validated_not_stored_verbatim(self):
        from fundos.profile.logo import resolve_logo

        class _Stub:
            company_id = "test"

        self.assertEqual(resolve_logo(_Stub(), logo_url="javascript:alert(1)"),
                         "")
        self.assertEqual(
            resolve_logo(_Stub(), logo_url="https://img.logo.dev/zyla.in?t=1"),
            "https://img.logo.dev/zyla.in?t=1")


# ---------------------------------------------------------------------------
# One representation of "absent" per field
# ---------------------------------------------------------------------------
class NumericFieldTests(TestCase):
    """A live competitors array shipped `revenue: ""` beside `revenue: 28.5`
    beside `funding_usd_mn: null` — three spellings of absent in two columns of
    the same six rows. A client doing arithmetic gets NaN on some of them."""

    def test_an_empty_string_in_a_number_field_becomes_null(self):
        from fundos.profile import schema

        raw = {"sections": {"competitors": {"data": [
            {"name": "BeatO", "revenue": "", "funding_usd_mn": 50.8},
            {"name": "HealthifyMe", "revenue": 28.5},
        ]}}}
        profile = schema.normalize_profile(raw)
        rows = profile["sections"]["competitors"]["data"]
        self.assertIsNone(rows[0]["revenue"])
        self.assertEqual(rows[1]["revenue"], 28.5)

    def test_a_formatted_number_is_parsed(self):
        from fundos.profile.sanitize import as_number

        self.assertEqual(as_number("1,250"), 1250.0)
        self.assertEqual(as_number("45%"), 45.0)
        self.assertEqual(as_number("USD 15.5M"), 15.5)
        self.assertEqual(as_number("₹8.6 crore"), 8.6)

    def test_accounting_parentheses_are_a_negative(self):
        """"(6.32)" is a loss. Reading it as +6.32 flips the sign on an
        EBITDA, which is not a rounding error — it is the opposite claim."""
        from fundos.profile.sanitize import as_number

        self.assertEqual(as_number("(6.32)"), -6.32)

    def test_non_numbers_become_none_rather_than_zero(self):
        from fundos.profile.sanitize import as_number

        for value in (None, "", "not disclosed", "n/a", "TBD", [1, 2],
                      float("nan"), float("inf"), True):
            self.assertIsNone(as_number(value), f"{value!r} became a number")

    def test_a_scale_word_is_reported_not_silently_applied(self):
        """Converting "8.6 crore" to 86,000,000 inside a field named `_m`
        would replace a visible inconsistency with an invisible one."""
        from fundos.profile.sanitize import describe_number

        number, scale, currency = describe_number("₹8.6 crore")
        self.assertEqual(number, 8.6)
        self.assertEqual(scale, "crore")
        self.assertEqual(currency, "INR")

    def test_a_numeric_field_carrying_a_scale_word_is_reported(self):
        from fundos.profile import schema

        notes = []
        raw = {"sections": {"company_profile": {"data": {
            "total_funding_raised_usd_mn": "₹80 crore"}}}}
        schema.normalize_profile(raw, notes=notes)
        self.assertTrue(any("crore" in n for n in notes),
                        "a unit mismatch was stored with no record of it")


# ---------------------------------------------------------------------------
# Fiscal years, estimates, and what may reach a valuation
# ---------------------------------------------------------------------------
class FiscalYearTests(TestCase):

    def test_mixed_year_conventions_sort_by_actual_year(self):
        """"".join(digits)" reads FY24 as 24 and "FY 2024" as 2024, so one row
        written the long way sorts above every other year — and the caller
        picks the last row."""
        from fundos.profile.services import _fy_key

        rows = [{"fiscalYear": "FY24"}, {"fiscalYear": "FY 2023"},
                {"fiscalYear": "FY25"}, {"fiscalYear": "FY2021-22"}]
        latest = sorted(rows, key=_fy_key)[-1]
        self.assertEqual(latest["fiscalYear"], "FY25")

    def test_a_projection_is_recognised_from_its_flag_or_its_label(self):
        from fundos.profile.services import _is_estimate_row

        self.assertTrue(_is_estimate_row({"isEstimate": True}))
        self.assertTrue(_is_estimate_row({"fiscalYear": "FY27E"}))
        self.assertTrue(_is_estimate_row({"fiscalYear": "FY29 projected"}))
        self.assertFalse(_is_estimate_row({"fiscalYear": "FY24"}))

    def test_pat_is_no_longer_written_into_the_growth_column(self):
        """`pick("growth_pct", "pat", ...)` stored a profit-after-tax as a
        percentage: a loss of -6.32 crore was rendered as -6.32% YoY growth.
        A plausible number, in the right shape, describing something else."""
        from fundos.profile.services import _fin_row_to_spec

        row = _fin_row_to_spec({"financial_year": "FY25", "revenue": 16.5,
                                "pat": -7.4, "currency": "INR"})
        self.assertIsNone(row["growth_pct"])
        self.assertEqual(row["pat_m"], -7.4)
        self.assertEqual(row["currency"], "INR")

    def test_the_financial_form_keeps_pat_currency_and_the_estimate_flag(self):
        from fundos.profile.records import validate_structured_form

        out = validate_structured_form("financial_summary", {"items": [
            {"fiscalYear": "FY27E", "revenue": 55.04, "ebitda": 2,
             "pat": 3, "isEstimate": True, "ccy": "INR", "growthPct": 102},
        ]})
        row = out["items"][0]
        self.assertEqual(row["pat"], Decimal("3"))
        self.assertEqual(row["ccy"], "INR")
        self.assertIs(row["isEstimate"], True)
        self.assertEqual(row["growthPct"], 102.0)

    def test_a_growth_rate_above_one_hundred_percent_is_accepted(self):
        """A company in this database grew 300% in a year. Validating growth
        as a 0-100 share rejects the true figure and accepts only an
        understated one."""
        from fundos.profile.records import validate_structured_form

        out = validate_structured_form("financial_summary", {"items": [
            {"fiscalYear": "FY23", "growthPct": 300}]})
        self.assertEqual(out["items"][0]["growthPct"], 300.0)


class CkbSyncTests(TestCase):
    """The valuation engine reads CKB `revenue` as the company's revenue, not
    as its plan."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.core.management import call_command
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        from fundos.profile.services import get_or_create_profile

        self.world = make_world()
        self.tenant = self.world["a"]["tenant"]
        self.user = self.world["a"]["founder"]
        with tenant_context(self.tenant.id):
            self.profile = get_or_create_profile(self.world["a"]["company"],
                                                 user=self.user)
            self.profile.home_currency = "INR"
            self.profile.save(update_fields=["home_currency"])

    def _sync(self, items):
        from fundos.profile.services import _sync_financials_to_ckb
        with tenant_context(self.tenant.id):
            _sync_financials_to_ckb(self.profile, "financial_summary",
                                    {"items": items}, user=self.user)

    def _ckb_value(self, field_key):
        from fundos.core.models import CkbField
        with tenant_context(self.tenant.id):
            row = CkbField.all_objects.filter(field_key=field_key).first()
        return row

    def test_a_projection_never_becomes_the_valuation_input(self):
        """A live profile carried eleven fiscal rows to FY30, four flagged as
        estimates, and the sync selected the FY30 projection."""
        self._sync([
            {"fiscalYear": "FY25", "revenue": Decimal("16500000"),
             "ccy": "INR"},
            {"fiscalYear": "FY30", "revenue": Decimal("217500000"),
             "isEstimate": True, "ccy": "INR"},
        ])
        row = self._ckb_value("revenue")
        self.assertIsNotNone(row, "nothing was synced at all")
        self.assertEqual(float(row.value_num), 16_500_000.0,
                         "a forecast was written as revenue")

    def test_the_currency_travels_with_the_figure(self):
        """The CKB stores a (value, ccy, basis) triple precisely so a number
        cannot be read in the wrong denomination. This path wrote the value
        alone."""
        self._sync([{"fiscalYear": "FY25", "revenue": Decimal("16500000"),
                     "ccy": "INR"}])
        row = self._ckb_value("revenue")
        self.assertEqual(row.ccy, "INR",
                         "a money field was written with no denomination")

    def test_an_unstated_currency_falls_back_to_the_company_s_own(self):
        self._sync([{"fiscalYear": "FY25", "revenue": Decimal("16500000")}])
        self.assertEqual(self._ckb_value("revenue").ccy, "INR")

    def test_a_metric_whose_unit_states_a_scale_is_refused(self):
        """`ARR = 12` with unit `INR crore` is ₹120,000,000; taken as typed it
        is twelve rupees. The CKB feeds the valuation and nothing downstream
        can detect a magnitude wrong by seven orders."""
        from fundos.profile.services import _sync_financials_to_ckb

        with tenant_context(self.tenant.id):
            _sync_financials_to_ckb(
                self.profile, "company_metrics",
                {"items": [{"label": "ARR", "value": Decimal("12"),
                            "unit": "INR crore"}]}, user=self.user)
        self.assertIsNone(self._ckb_value("arr"),
                          "an ambiguous magnitude was written anyway")

    def test_a_metric_in_plain_units_still_syncs(self):
        from fundos.profile.services import _sync_financials_to_ckb

        with tenant_context(self.tenant.id):
            _sync_financials_to_ckb(
                self.profile, "company_metrics",
                {"items": [{"label": "ARR", "value": Decimal("12000000"),
                            "unit": "INR"}]}, user=self.user)
        row = self._ckb_value("arr")
        self.assertIsNotNone(row, "a well-specified metric was skipped")
        self.assertEqual(float(row.value_num), 12_000_000.0)
        self.assertEqual(row.ccy, "INR")

    def test_an_all_estimate_table_syncs_nothing(self):
        self._sync([{"fiscalYear": "FY30", "revenue": Decimal("217500000"),
                     "isEstimate": True}])
        self.assertIsNone(self._ckb_value("revenue"),
                          "a projection was the only thing available and was "
                          "written anyway")


# ---------------------------------------------------------------------------
# A supplied founder is a research lead, not a finished record
# ---------------------------------------------------------------------------
class FounderEnrichmentTests(TestCase):
    """Skipping any row whose name a human had claimed protected the human's
    data by discarding everything researched about them. On a live profile the
    two actual principals carried a name and a LinkedIn URL and empty `role`
    and `background`, while a part-time co-founder — not named at onboarding,
    so not suppressed — carried a full researched paragraph."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.core.management import call_command
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        from fundos.profile.services import get_or_create_profile

        self.world = make_world()
        self.tenant = self.world["a"]["tenant"]
        self.user = self.world["a"]["founder"]
        with tenant_context(self.tenant.id):
            self.profile = get_or_create_profile(self.world["a"]["company"],
                                                 user=self.user)

    def _write_ai(self, rows):
        from fundos.profile.section_writer import update_section_from_data
        update_section_from_data(self.profile, "founders", rows,
                                 user=self.user, by_ai=True)

    def test_blank_fields_on_a_human_row_are_filled_by_research(self):
        from fundos.profile.models import Founder

        with tenant_context(self.tenant.id):
            Founder.objects.create(
                tenant_id=self.tenant.id, profile=self.profile,
                name="Khushboo Aggarwal", source="founder",
                linkedin_url="https://www.linkedin.com/in/khushbooaggarwal/")
            self._write_ai([{
                "name": "Khushboo Aggarwal", "role": "Co-Founder & CEO",
                "background": "Founded Zyla in 2017 after IIT Delhi.",
                "is_founder": True}])
            rows = list(Founder.objects.filter(profile=self.profile))

        self.assertEqual(len(rows), 1, "the same person was listed twice")
        self.assertEqual(rows[0].designation, "Co-Founder & CEO")
        self.assertIn("Zyla", rows[0].experience)

    def test_a_value_the_human_supplied_is_never_overwritten(self):
        from fundos.profile.models import Founder

        with tenant_context(self.tenant.id):
            Founder.objects.create(
                tenant_id=self.tenant.id, profile=self.profile,
                name="Asha Rao", designation="CEO", source="founder")
            self._write_ai([{"name": "asha rao", "role": "Chief Executive",
                             "background": "Researched.", "is_founder": True}])
            row = Founder.objects.get(profile=self.profile)

        self.assertEqual(row.designation, "CEO", "the human's value must win")
        self.assertEqual(row.experience, "Researched.",
                         "a blank field should still have been filled")

    def test_a_field_research_filled_can_be_refreshed_by_a_later_run(self):
        """Without a per-field record of what the AI supplied, the first value
        written into a blank field is indistinguishable from something the
        founder typed, and the enrichment works exactly once."""
        from fundos.profile.models import Founder

        with tenant_context(self.tenant.id):
            Founder.objects.create(
                tenant_id=self.tenant.id, profile=self.profile,
                name="Asha Rao", source="founder")
            self._write_ai([{"name": "Asha Rao", "background": "First pass.",
                             "is_founder": True}])
            self._write_ai([{"name": "Asha Rao", "background": "Second pass.",
                             "is_founder": True}])
            row = Founder.objects.get(profile=self.profile)

        self.assertEqual(row.experience, "Second pass.")

    def test_a_human_row_is_enriched_in_place_not_duplicated_across_tables(self):
        """A person the founder filed as a Founder, whom the research reports
        as a non-founder executive, must not appear in both tables."""
        from fundos.profile.models import Founder, KeyPerson

        with tenant_context(self.tenant.id):
            Founder.objects.create(
                tenant_id=self.tenant.id, profile=self.profile,
                name="Ashish Garg", source="founder")
            self._write_ai([{"name": "Ashish Garg",
                             "role": "Chief Business Officer",
                             "background": "Ex-Lightrock.",
                             "is_founder": False}])
            founders = list(Founder.objects.filter(profile=self.profile))
            key_people = list(KeyPerson.objects.filter(profile=self.profile))

        self.assertEqual(len(founders), 1)
        self.assertEqual(len(key_people), 0,
                         "the same person was written into both tables")
        self.assertEqual(founders[0].designation, "Chief Business Officer")

    def test_a_person_only_the_research_found_is_still_added(self):
        from fundos.profile.models import Founder

        with tenant_context(self.tenant.id):
            Founder.objects.create(
                tenant_id=self.tenant.id, profile=self.profile,
                name="Asha Rao", source="founder")
            self._write_ai([{"name": "Asha Rao", "is_founder": True},
                            {"name": "Tanmay Patil", "role": "Co-Founder",
                             "is_founder": True}])
            names = set(Founder.objects.filter(profile=self.profile)
                        .values_list("name", flat=True))

        self.assertEqual(names, {"Asha Rao", "Tanmay Patil"})

    def test_an_explicit_null_is_still_read_as_a_founder(self):
        """Coercing a null boolean to False at the sanitize boundary would
        invert the rule below without touching the code that documents it."""
        from fundos.profile.models import Founder

        with tenant_context(self.tenant.id):
            self._write_ai([{"name": "Null Flagged", "role": "CEO",
                             "is_founder": None}])
            names = set(Founder.objects.filter(profile=self.profile)
                        .values_list("name", flat=True))

        self.assertIn("Null Flagged", names)

    def test_an_unflagged_person_is_still_read_as_a_founder(self):
        """A missing `is_founder` means "the model did not classify this
        person", and the documented rule is that they count as a founder.
        Collapsing that to False files them as a key person and empties the
        section a reader checks for the team."""
        from fundos.profile.models import Founder

        with tenant_context(self.tenant.id):
            self._write_ai([{"name": "Unflagged Person", "role": "CEO"}])
            names = set(Founder.objects.filter(profile=self.profile)
                        .values_list("name", flat=True))

        self.assertIn("Unflagged Person", names)


# ---------------------------------------------------------------------------
# Structured sections get the provenance rule the record tables always had
# ---------------------------------------------------------------------------
class StructuredSectionProvenanceTests(TestCase):
    """The "an AI write replaces only its own previous output" rule lived in
    `_replace_entity_rows` and covered the record tables only, so a full
    pipeline run silently overwrote a founder's hand-edited section JSON."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.core.management import call_command
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        from fundos.profile.services import get_or_create_profile

        self.world = make_world()
        self.tenant = self.world["a"]["tenant"]
        self.user = self.world["a"]["founder"]
        with tenant_context(self.tenant.id):
            self.profile = get_or_create_profile(self.world["a"]["company"],
                                                 user=self.user)

    def _write(self, data, *, by_ai):
        from fundos.profile.section_writer import update_section_from_data
        with tenant_context(self.tenant.id):
            return update_section_from_data(self.profile, "business_model",
                                            data, user=self.user, by_ai=by_ai)

    def test_generation_does_not_overwrite_a_human_edited_section(self):
        self._write({"pricing_model": "Per seat, INR 400/month"}, by_ai=False)
        section = self._write({"pricing_model": "Subscription",
                               "sales_model": "Enterprise field sales"},
                              by_ai=True)
        self.assertEqual(section.structured["pricing_model"],
                         "Per seat, INR 400/month",
                         "generation overwrote a human's own value")

    def test_generation_still_fills_the_fields_the_human_left_blank(self):
        self._write({"pricing_model": "Per seat, INR 400/month"}, by_ai=False)
        section = self._write({"pricing_model": "Subscription",
                               "sales_model": "Enterprise field sales"},
                              by_ai=True)
        self.assertEqual(section.structured["sales_model"],
                         "Enterprise field sales",
                         "a blank field is a gap to close, not data to keep")

    def test_a_person_clearing_a_populated_section_leaves_a_record(self):
        """A section reading empty is otherwise indistinguishable from one the
        research looked at and found nothing for, and those call for opposite
        responses."""
        self._write({"pricing_model": "Subscription"}, by_ai=True)
        section = self._write({}, by_ai=False)
        self.assertTrue(
            any("Cleared by a person" in str(n)
                for n in (section.missing_notes or [])),
            "a human emptied a populated section with no signal left behind")

    def test_an_untouched_section_is_written_normally_by_generation(self):
        section = self._write({"pricing_model": "Subscription"}, by_ai=True)
        self.assertEqual(section.structured["pricing_model"], "Subscription")


# ---------------------------------------------------------------------------
# Resource bounds
# ---------------------------------------------------------------------------
class SanitizerBoundsTests(TestCase):
    """A model can emit an arbitrarily large or deep structure, and this runs
    inside a worker holding a database connection."""

    def test_depth_is_bounded(self):
        from fundos.profile import sanitize

        deep = current = {}
        for _ in range(sanitize.MAX_DEPTH + 20):
            current["next"] = {}
            current = current["next"]
        out = sanitize.sanitize_json(deep)      # must return, not recurse away
        self.assertIsInstance(out, dict)

    def test_a_huge_string_is_truncated_rather_than_stored(self):
        from fundos.profile import sanitize

        out = sanitize.plain_text("x" * (sanitize.MAX_STRING_CHARS + 5_000))
        self.assertLessEqual(len(out), sanitize.MAX_STRING_CHARS + 1)

    def test_a_huge_array_is_capped(self):
        from fundos.profile import sanitize

        out = sanitize.sanitize_json(list(range(sanitize.MAX_LIST_ITEMS + 500)))
        self.assertLessEqual(len(out), sanitize.MAX_LIST_ITEMS)

    def test_a_non_dict_response_yields_an_empty_profile_not_a_crash(self):
        from fundos.profile import schema

        for raw in (None, [], "text", 42):
            profile = schema.normalize_profile(raw)
            self.assertIn("sections", profile)
            self.assertFalse(any(s["isComplete"]
                                 for s in profile["sections"].values()))


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
class ResearchConcurrencyTests(TestCase):

    def test_the_shipped_concurrency_is_five(self):
        from fundos.profile.pipeline import settings as pipeline_settings

        self.assertEqual(pipeline_settings.DEFAULT_RESEARCH_CONCURRENCY, 5)

    def test_the_configured_value_is_clamped_to_the_batch_count(self):
        from fundos.profile.pipeline import settings as pipeline_settings

        self.assertLessEqual(pipeline_settings.configured_research_concurrency(),
                             10)

    def test_sqlite_still_clamps_the_effective_value_to_one(self):
        """Concurrency here is a latency optimisation, never a correctness
        requirement — nine of ten batches were lost to "database table is
        locked" before the clamp."""
        from django.db import connection
        from fundos.profile.pipeline import settings as pipeline_settings

        if connection.vendor != "sqlite":
            self.skipTest("only meaningful on the SQLite test backend")
        self.assertEqual(pipeline_settings.research_concurrency(), 1)
