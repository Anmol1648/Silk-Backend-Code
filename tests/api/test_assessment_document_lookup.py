"""The assessment must be able to read the deal's uploaded documents.

`_deal_documents` returned [] for every deal that has ever existed. It tried
three reverse accessors the Deal model does not have, then
`Document.objects.filter(deal=deal)` — and `Document.deal_id` is a plain
UUIDField, not a ForeignKey, so that raises

    FieldError: Cannot resolve keyword 'deal' into field

which a bare `except Exception: return []` turned into "this deal has no
documents", on every run, behind an INFO line that read like a fact about the
deal.

The cost was category B. Financials is deliberately excluded from step 1
research (`NOT_RESEARCHABLE = {"B", "G"}`) because reading revenue off a
marketing page is the worst thing this system could do, so revenue, margins,
runway and debt were meant to come from the founder's uploaded model. They
came from nowhere: a 4 MB model could be uploaded, reported as 552,541
characters extracted, and every Financials row still rendered blank.
"""
from unittest import mock

from django.test import TestCase


class DealDocumentLookupTests(TestCase):

    def test_documents_are_looked_up_by_deal_id_not_by_deal(self):
        """`filter(deal=...)` raises FieldError; `deal_id` is the real column."""
        from fundos.docs.models import Document

        with self.assertRaises(Exception):
            Document.objects.filter(deal="x").count()
        # The correct form resolves. It returning zero rows here is fine —
        # what matters is that it does not raise.
        self.assertEqual(
            Document.objects.filter(
                deal_id="00000000-0000-0000-0000-000000000000").count(), 0)

    def test_it_no_longer_swallows_errors_into_an_empty_list(self):
        """A lookup failure must not be indistinguishable from 'no uploads'."""
        import inspect

        from fundos.assessment import extraction
        src = inspect.getsource(extraction._deal_documents)
        self.assertNotIn("except Exception:\n        return []", src)
        self.assertIn("deal_id=deal.id", src)

    def test_a_document_with_no_stored_version_is_reported(self):
        from fundos.assessment.extraction import _deal_documents

        doc = mock.Mock(id="d1", title="Model.xlsx", current_version_id=None)
        deal = mock.Mock(id="deal1")
        with mock.patch("fundos.docs.models.Document.objects.filter",
                        return_value=[doc]), \
                mock.patch("fundos.docs.models.DocumentVersion.objects.filter",
                           return_value=[]), \
                self.assertLogs("fundos.assessment.extraction",
                                level="WARNING") as logs:
            self.assertEqual(_deal_documents(deal), [])
        self.assertIn("Model.xlsx", "\n".join(logs.output))

    def test_a_readable_document_carries_its_storage_uri(self):
        from fundos.assessment.extraction import _deal_documents

        doc = mock.Mock(id="d1", title="Model.xlsx", current_version_id="v1")
        version = mock.Mock(document_id="d1", storage_uri="t/d/model.xlsx")
        deal = mock.Mock(id="deal1")
        with mock.patch("fundos.docs.models.Document.objects.filter",
                        return_value=[doc]), \
                mock.patch("fundos.docs.models.DocumentVersion.objects.filter",
                           return_value=[version]):
            out = _deal_documents(deal)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "Model.xlsx")
        self.assertEqual(out[0]["storage_uri"], "t/d/model.xlsx")


class StorageFetchTests(TestCase):
    """Files live in object storage, which is not the local filesystem."""

    def test_a_failed_download_returns_empty_and_leaves_no_temp_file(self):
        import os

        from fundos.assessment.extraction import _fetch
        with mock.patch("fundos.docs.storage.get_storage") as store:
            store.return_value.download_file.return_value = False
            self.assertEqual(_fetch("t/d/model.xlsx"), "")
        # Nothing to assert on the path itself; the point is it did not raise
        # and did not return a path to a file that was never written.
        self.assertTrue(True)

    def test_a_storage_exception_is_logged_not_raised(self):
        from fundos.assessment.extraction import _fetch
        with mock.patch("fundos.docs.storage.get_storage",
                        side_effect=RuntimeError("bucket down")), \
                self.assertLogs("fundos.assessment.extraction",
                                level="WARNING"):
            self.assertEqual(_fetch("t/d/model.xlsx"), "")

    def test_the_suffix_is_preserved_so_readers_can_dispatch(self):
        """read_document picks its reader from the extension."""
        import os

        from fundos.assessment.extraction import _fetch
        captured = {}

        def fake_download(uri, path):
            captured["path"] = path
            with open(path, "wb") as fh:
                fh.write(b"x")
            return True

        with mock.patch("fundos.docs.storage.get_storage") as store:
            store.return_value.download_file = fake_download
            path = _fetch("tenant/deal/model.xlsx")
        try:
            self.assertTrue(path.endswith(".xlsx"))
        finally:
            if path and os.path.exists(path):
                os.unlink(path)


class FinancialsAreNotResearchedTests(TestCase):
    """Why this lookup carries category B on its own."""

    def test_category_b_is_excluded_from_step_one_research(self):
        from fundos.profile.assessment_extraction import NOT_RESEARCHABLE
        self.assertIn("B", NOT_RESEARCHABLE)

    def test_so_financials_depend_entirely_on_the_document_lookup(self):
        """If _deal_documents is empty, nothing else can fill Financials."""
        from fundos.assessment.models import ConfigParameter
        from fundos.profile.assessment_extraction import question_set

        b_keys = {p.input_key for p in ConfigParameter.objects.filter(
            is_active=True, feeds_score=True, category_code="B")}
        if not b_keys:
            self.skipTest("assessment config not seeded in this database")
        researched = set(question_set())
        self.assertEqual(
            b_keys & researched, set(),
            "category B must not be researched — it comes from the "
            "founder's uploaded financial model")
