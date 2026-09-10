"""Readiness counts what the screen shows, and one object is one part.

Two defects, both of which made the score say something untrue about work a
founder had actually done.

The count came from `data.keys()`, which carries three keys the form never
renders: `total_funding_raised_display` beside `total_funding_raised_usd_mn`,
and the same for pre- and post-money. Those are not fields — they are the
currency and unit halves of a control the user sees as ONE input. So
`company_profile` counted 15 fields where the screen offers 12, and because no
control existed to confirm them, they could never be confirmed either: three
permanently unconfirmable fields dragging every profile down from 10/12 to
10/15.

And a list section was confirmed all-or-nothing — `len(data) if cf else 0` —
so one click marked all seven founders confirmed, including the six nobody had
read.
"""
from django.test import TestCase

from fundos.profile import schema as profile_schema
from fundos.profile.spec_serializer import (_confirmed_items,
                                            _readiness_breakdown)


def _section(key, data, confirmed=None):
    return {key: {"sectionKey": key, "data": data,
                  "confirmed_fields": confirmed or []}}


class TheCountIsWhatTheScreenShows(TestCase):

    def test_company_profile_counts_twelve_not_fifteen(self):
        data = {
            "description_of_business": "x", "website": "https://z.in",
            "country": "IN", "macro_sector": "Healthcare",
            "sub_sector": "Digital Health", "funding_status_name": "VC Funded",
            "revenue_size_name": "USD 1M to 5M", "currency_id": "INR",
            "total_funding_raised_usd_mn": 6.27,
            "last_funding_round_date": "2023-11-30",
            "latest_pre_money_usd_mn": None, "latest_post_money_usd_mn": None,
            # The three the form never renders on their own.
            "total_funding_raised_display": "USD:6.27:M",
            "latest_pre_money_display": "", "latest_post_money_display": "",
        }
        entry = _readiness_breakdown(_section("company_profile", data))[0]
        self.assertEqual(entry["fields"], 12)

    def test_a_display_companion_is_not_counted_as_populated(self):
        data = {"total_funding_raised_usd_mn": 6.27,
                "total_funding_raised_display": "USD:6.27:M"}
        entry = _readiness_breakdown(_section("company_profile", data))[0]
        # One value was supplied, not two.
        self.assertEqual(entry["populated"], 1)

    def test_the_unconfirmable_fields_no_longer_hold_the_score_down(self):
        data = {"description_of_business": "x", "website": "w",
                "country": "IN", "macro_sector": "m", "sub_sector": "s",
                "funding_status_name": "f", "revenue_size_name": "r",
                "currency_id": "INR", "total_funding_raised_usd_mn": 6.27,
                "last_funding_round_date": "2023-11-30",
                "latest_pre_money_usd_mn": None,
                "latest_post_money_usd_mn": None,
                "total_funding_raised_display": "USD:6.27:M",
                "latest_pre_money_display": "",
                "latest_post_money_display": ""}
        confirmed = ["description_of_business", "website", "country",
                     "macro_sector", "sub_sector", "funding_status_name",
                     "revenue_size_name", "currency_id",
                     "last_funding_round_date",
                     "total_funding_raised_usd_mn"]
        entry = _readiness_breakdown(
            _section("company_profile", data, confirmed))[0]
        self.assertEqual((entry["confirmed"], entry["fields"]), (10, 12))

    def test_the_count_matches_the_schema_the_form_is_built_from(self):
        spec = profile_schema.sections_by_key()
        for key in ("company_profile", "customers_markets", "business_model"):
            declared = len(spec[key]["fields"])
            noisy = dict.fromkeys(spec[key]["fields"], "v")
            noisy["some_internal_companion_display"] = "x"
            entry = _readiness_breakdown(_section(key, noisy))[0]
            self.assertEqual(entry["fields"], declared, key)

    def test_an_unknown_section_still_counts_its_own_keys(self):
        """No schema is not a reason to report zero and vanish."""
        entry = _readiness_breakdown(
            _section("not_in_the_schema", {"a": 1, "b": 2}))[0]
        self.assertEqual(entry["fields"], 2)


class OneObjectIsOnePart(TestCase):

    ITEMS = [{"id": "a", "name": "Khushboo"},
             {"id": "b", "name": "Aishwary"},
             {"id": "c", "name": "Tanmay"}]

    def test_confirming_one_does_not_confirm_the_others(self):
        self.assertEqual(_confirmed_items("founders", self.ITEMS, {"a"}), 1)

    def test_confirming_two_counts_two(self):
        self.assertEqual(
            _confirmed_items("founders", self.ITEMS, {"a", "c"}), 2)

    def test_nothing_confirmed_counts_zero(self):
        self.assertEqual(_confirmed_items("founders", self.ITEMS, set()), 0)

    def test_an_id_nobody_recognises_confirms_nobody(self):
        self.assertEqual(
            _confirmed_items("founders", self.ITEMS, {"not-an-id"}), 0)

    def test_the_legacy_whole_section_confirmation_still_counts(self):
        """Existing profiles stored the section key from when confirming was
        all-or-nothing; reading that as zero would un-confirm real work."""
        self.assertEqual(
            _confirmed_items("founders", self.ITEMS, {"founders"}), 3)

    def test_the_inner_fields_of_an_object_are_never_counted(self):
        entry = _readiness_breakdown(
            _section("founders", self.ITEMS, ["a"]))[0]
        # Three people, not nine name/role/id fields.
        self.assertEqual(entry["fields"], 3)
        self.assertEqual(entry["confirmed"], 1)


class APositionStillAddressesTheItemItNames(TestCase):
    """Clients sent "0".."N" before items carried ids, and still do.

    Read as unrecognised, those confirmations vanished: five founders
    confirmed, `confirmed_fields: ["0","1","2","3","4"]` stored on the
    section, and a breakdown that reported `confirmed: 0`. The work was done
    and the score never moved.
    """

    FOUNDERS = [
        {"id": "62a04985", "name": "Puneet Bhaskar", "role": "CEO"},
        {"id": "9d8cb4b7", "name": "Jiveshwar Sharma", "role": "CTO"},
        {"id": "9172b0ad", "name": "Rupendra Pratap Singh", "role": "COO"},
        {"id": "8c015ad2", "name": "Nikhil Kalra", "role": "Head of Product"},
        {"id": "fb9c8280", "name": "Sunish Kumar", "role": "Co-Founder"},
    ]

    def test_the_five_founders_confirmed_by_position_count_five(self):
        """The reported bug, exactly as it arrived."""
        entry = _readiness_breakdown(
            _section("founders", self.FOUNDERS,
                     ["0", "1", "2", "3", "4"]))[0]
        self.assertEqual(entry["fields"], 5)
        self.assertEqual(entry["populated"], 5)
        self.assertEqual(entry["confirmed"], 5)

    def test_one_position_confirms_one_item(self):
        self.assertEqual(
            _confirmed_items("founders", self.FOUNDERS, {"2"}), 1)

    def test_ids_and_positions_mix_without_double_counting(self):
        """"0" and "62a04985" are the same founder. Counting is per item, so
        that founder is one, not two."""
        self.assertEqual(
            _confirmed_items("founders", self.FOUNDERS,
                             {"0", "62a04985", "3"}), 2)

    def test_a_position_past_the_end_confirms_nobody(self):
        self.assertEqual(
            _confirmed_items("founders", self.FOUNDERS, {"9"}), 0)

    def test_an_id_is_still_the_address_that_works(self):
        self.assertEqual(
            _confirmed_items("founders", self.FOUNDERS,
                             {"9d8cb4b7", "fb9c8280"}), 2)

    def test_a_wrapper_section_takes_positions_too(self):
        """`document_center` wraps its list one key in; the same addresses
        have to reach it."""
        entry = _readiness_breakdown(_section(
            "document_center",
            {"documents": [{"id": "d1", "filename": "deck.pdf"},
                           {"id": "d2", "filename": "model.xlsx"}]},
            ["0", "1"]))[0]
        self.assertEqual(entry["fields"], 2)
        self.assertEqual(entry["confirmed"], 2)


class AnObjectSectionIsConfirmedByFieldName(TestCase):
    """Object sections were never broken by the position bug — they are keyed
    by declared field name — but nothing pinned that down."""

    def test_named_fields_are_counted(self):
        entry = _readiness_breakdown(_section(
            "company_profile",
            {"description_of_business": "A B2B tyre marketplace.",
             "website": "https://tyreplex.com"},
            ["description_of_business", "website"]))[0]
        self.assertEqual(entry["confirmed"], 2)

    def test_a_field_the_schema_does_not_declare_is_not_counted(self):
        entry = _readiness_breakdown(_section(
            "company_profile",
            {"description_of_business": "A B2B tyre marketplace."},
            ["description_of_business", "invented_field"]))[0]
        self.assertEqual(entry["confirmed"], 1)

    def test_a_position_is_not_an_address_on_an_object_section(self):
        """An object section has named fields; "0" names none of them, and
        must not be read as "the first one"."""
        entry = _readiness_breakdown(_section(
            "company_profile",
            {"description_of_business": "A B2B tyre marketplace."},
            ["0"]))[0]
        self.assertEqual(entry["confirmed"], 0)


class EveryPersonCarriesTheirOwnAddress(TestCase):
    """A Confirm has to stick to the person it was given to."""

    def test_the_row_template_declares_an_id(self):
        from fundos.profile.spec_serializer import _LIST_ROW_TEMPLATES

        self.assertIn("id", _LIST_ROW_TEMPLATES["founders"])

    def test_a_new_list_item_is_given_a_durable_id_on_write(self):
        from fundos.profile.section_writer import _ensure_item_ids

        items = [{"name": "A"}, {"name": "B", "id": "kept"}]
        _ensure_item_ids(items)
        self.assertTrue(items[0]["id"])
        self.assertEqual(items[1]["id"], "kept")
        self.assertNotEqual(items[0]["id"], items[1]["id"])

    def test_ids_are_not_positions(self):
        """Insert someone at the top and every later id must be unchanged —
        the failure this exists to prevent is a confirmation quietly moving
        to a different founder."""
        from fundos.profile.section_writer import _ensure_item_ids

        items = [{"name": "A"}, {"name": "B"}]
        _ensure_item_ids(items)
        before = [i["id"] for i in items]

        items.insert(0, {"name": "New"})
        _ensure_item_ids(items)
        self.assertEqual([i["id"] for i in items[1:]], before)

    def test_a_non_list_payload_is_left_alone(self):
        from fundos.profile.section_writer import _ensure_item_ids

        payload = {"website": "x"}
        self.assertIs(_ensure_item_ids(payload), payload)
        self.assertNotIn("id", payload)


class EveryArrayOfObjectsIsConfirmableItemByItem(TestCase):
    """Not just founders. Wherever `data` is a list of objects, each object
    is one part with one Confirm, so each object needs its own address."""

    LIST_SECTIONS = ("founders", "products_services", "customers_markets",
                     "competitive_advantages", "revenue_model",
                     "company_metrics", "funding_history", "competitors",
                     "news")

    def test_every_list_template_declares_an_id(self):
        from fundos.profile.spec_serializer import _LIST_ROW_TEMPLATES

        for key in self.LIST_SECTIONS:
            self.assertIn("id", _LIST_ROW_TEMPLATES[key],
                          f"{key} rows cannot be confirmed individually")

    def test_the_schema_agrees_these_are_arrays(self):
        spec = profile_schema.sections_by_key()
        for key in self.LIST_SECTIONS:
            self.assertEqual(spec[key]["kind"], "array", key)

    def test_a_stored_id_is_read_back_not_recomputed(self):
        from fundos.profile.spec_serializer import _item_id

        self.assertEqual(_item_id({"id": "abc", "name": "x"}), "abc")
        self.assertEqual(_item_id({"name": "x"}), "")
        self.assertEqual(_item_id(None), "")
        self.assertEqual(_item_id("a plain string"), "")

    def test_nested_items_are_given_ids_on_write(self):
        """products_services and friends store rows under `items`; looking
        only for a bare list left every one of them unaddressable."""
        from fundos.profile.section_writer import _ensure_item_ids

        payload = {"items": [{"name": "Online Tyre Retail"},
                             {"name": "Motozee Stores"}]}
        _ensure_item_ids(payload)
        ids = [i["id"] for i in payload["items"]]
        self.assertTrue(all(ids))
        self.assertEqual(len(set(ids)), 2)

    def test_a_bare_list_still_gets_ids(self):
        from fundos.profile.section_writer import _ensure_item_ids

        payload = [{"name": "A"}]
        _ensure_item_ids(payload)
        self.assertTrue(payload[0]["id"])

    def test_confirming_one_product_confirms_only_that_product(self):
        products = [{"id": "p1", "name": "Online Tyre Retail"},
                    {"id": "p2", "name": "Motozee Stores"},
                    {"id": "p3", "name": "Dealer Financing"}]
        entry = _readiness_breakdown(
            _section("products_services", products, ["p2"]))[0]
        self.assertEqual((entry["fields"], entry["confirmed"]), (3, 1))


class AWrapperSectionCountsWhatItWraps(TestCase):
    """`document_center` declares one field whose value IS the file list."""

    DOCS = [{"id": "d1", "filename": "teaser.pptx", "status": "Processed"},
            {"id": "d2", "filename": "model.xlsx", "status": "Processed"},
            {"id": "d3", "filename": "scan.pdf", "status": "Uploaded"}]

    def test_it_counts_the_documents_not_the_wrapper(self):
        entry = _readiness_breakdown(
            _section("document_center", {"documents": self.DOCS}))[0]
        self.assertEqual(entry["fields"], 3)
        self.assertEqual(entry["populated"], 3)

    def test_one_confirmation_does_not_cover_the_whole_pack(self):
        entry = _readiness_breakdown(
            _section("document_center", {"documents": self.DOCS}, ["d2"]))[0]
        self.assertEqual(entry["confirmed"], 1)

    def test_an_empty_pack_still_counts_as_one_slot(self):
        entry = _readiness_breakdown(
            _section("document_center", {"documents": []}))[0]
        self.assertEqual((entry["fields"], entry["populated"]), (1, 0))

    def test_an_ordinary_object_section_is_untouched(self):
        """The rule must not swallow a normal multi-field section."""
        spec = profile_schema.sections_by_key()
        data = dict.fromkeys(spec["business_model"]["fields"], "v")
        entry = _readiness_breakdown(_section("business_model", data))[0]
        self.assertEqual(entry["fields"], len(spec["business_model"]["fields"]))
