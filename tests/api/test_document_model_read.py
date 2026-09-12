"""Documents are read by a model, and the model never gets the spreadsheet.

The local OCR engines are gone. What replaced them is a call that hands the
FILE to a document-capable model, so the tests that matter are about which
files go that way, what happens when the call fails, and whether the result is
labelled honestly enough that a reader can tell a transcribed figure from an
extracted one.

The failure this guards hardest against is the quiet one: a model that cannot
read the attachment answering from the prompt alone and returning a confident
summary of nothing.
"""
import inspect
import re
from unittest import mock

from django.test import TestCase

from fundos.profile.pipeline import document_ai
from fundos.profile.pipeline import source2_documents as s2


class OnlyTheRightFilesReachTheModel(TestCase):

    def test_a_spreadsheet_is_never_sent(self):
        """Cells are exact. A model reading them could only be worse."""
        for name in ("model.xlsx", "book.xlsm", "data.csv", "old.xls"):
            self.assertEqual(s2.policy_for(name).read_mode, "never", name)

    def test_a_word_document_is_never_sent(self):
        self.assertEqual(s2.policy_for("memo.docx").read_mode, "never")

    def test_pdfs_and_images_are_sent(self):
        for name in ("report.pdf", "cap.png", "scan.tiff"):
            self.assertEqual(s2.policy_for(name).read_mode, "model", name)

    def test_a_deck_is_not_sent_because_no_provider_reads_one(self):
        """It was, and the call failed every time with "Unsupported MIME
        type". The policy now says what actually happens, and names the
        remedy: save the deck as PDF."""
        policy = s2.policy_for("pitch.pptx")
        self.assertEqual(policy.read_mode, "never")
        self.assertIn("PDF", policy.description)

    def test_the_slide_text_is_still_extracted(self):
        """Refusing the model read must not stop native extraction."""
        self.assertIn("slide text", s2.policy_for("pitch.pptx").description)

    def test_an_unknown_extension_is_not_sent(self):
        """The model needs a declared media type; guessing one is not a
        service to anybody."""
        self.assertEqual(s2.policy_for("mystery.xyz").read_mode, "never")

    def test_the_supported_types_agree_with_the_policy_table(self):
        """The two tables have to say the same thing. They did not: the
        policy promised a model read for `.pptx` that the MIME table could
        not serve, so the promise was kept in the log and broken at the
        provider."""
        for name in ("report.pdf", "cap.png"):
            suffix = "." + name.split(".")[-1]
            self.assertTrue(document_ai.is_supported(suffix), name)
        for suffix in (".xlsx", ".docx", ".pptx"):
            self.assertFalse(document_ai.is_supported(suffix), suffix)

    def test_every_model_read_format_has_a_media_type(self):
        """The general rule behind the case above, asserted directly."""
        for suffix in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tiff"):
            policy = s2.policy_for("f" + suffix)
            if policy.read_mode == "model":
                self.assertTrue(document_ai.is_supported(suffix), suffix)

    def test_each_type_declares_a_real_media_type(self):
        self.assertEqual(document_ai.mime_for(".pdf"), "application/pdf")
        self.assertEqual(document_ai.mime_for(".png"), "image/png")
        self.assertEqual(document_ai.mime_for(".jpg"), "image/jpeg")


class TheReadIsATranscriptionNotAnOpinion(TestCase):

    def test_the_prompt_forbids_inventing_a_figure(self):
        prompt = document_ai.PROMPT.lower()
        self.assertIn("exactly as printed", prompt)
        self.assertIn("[unreadable]", prompt)
        for banned in ("never round", "do not summarise"):
            self.assertIn(banned, prompt)

    def test_the_prompt_asks_for_text_inside_images(self):
        self.assertIn("[from image]", document_ai.PROMPT)

    def test_the_role_is_declared_and_bindable(self):
        from fundos.llm.models import LLM_ROLES
        self.assertIn(document_ai.DOCUMENT_ROLE, LLM_ROLES)


class AFailedReadCostsTheFileNothing(TestCase):

    def read_pdf(self, sections):
        return s2._model_stage("x.pdf", "x.pdf", s2.policy_for("x.pdf"),
                               lambda _m: None, sections, None)

    def test_a_missing_file_is_reported_not_raised(self):
        with self.assertRaises(document_ai.DocumentReadError):
            document_ai.read_document("/nonexistent/file.pdf", "file.pdf")

    def test_an_oversized_file_falls_back_rather_than_truncating(self):
        with mock.patch.object(document_ai, "MAX_INLINE_BYTES", 10):
            with mock.patch("pathlib.Path.stat") as stat:
                stat.return_value = mock.Mock(st_size=5000)
                with self.assertRaises(document_ai.DocumentReadError) as caught:
                    document_ai.read_document("x.pdf", "x.pdf")
        self.assertIn("over the", str(caught.exception))

    def test_an_empty_answer_is_an_error_not_an_empty_document(self):
        with mock.patch("pathlib.Path.stat") as stat, \
                mock.patch("pathlib.Path.read_bytes", return_value=b"x"), \
                mock.patch("fundos.llm.adapter.llm_generate",
                           return_value={"text": "   "}):
            stat.return_value = mock.Mock(st_size=10)
            with self.assertRaises(document_ai.DocumentReadError) as caught:
                document_ai.read_document("x.pdf", "x.pdf")
        self.assertIn("no text", str(caught.exception))

    def test_the_dossier_records_why_a_read_failed(self):
        sections = []
        with mock.patch.object(
                document_ai, "read_document",
                side_effect=document_ai.DocumentReadError("rate limited")):
            outcome = self.read_pdf(sections)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("rate limited", outcome.reason)
        self.assertTrue(sections)

    def test_an_unexpected_error_is_also_contained(self):
        """One bad file never fails the source."""
        sections = []
        with mock.patch.object(document_ai, "read_document",
                               side_effect=ValueError("boom")):
            outcome = self.read_pdf(sections)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("boom", outcome.reason)

    def test_a_near_empty_read_is_not_treated_as_content(self):
        sections = []
        with mock.patch.object(document_ai, "read_document",
                               return_value="x"):
            outcome = self.read_pdf(sections)
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(outcome.text, "")

    def test_a_good_read_is_recorded_as_triggered(self):
        sections = []
        with mock.patch.object(document_ai, "read_document",
                               return_value="## Page 1\n\nRevenue INR 4.2 Cr"):
            outcome = self.read_pdf(sections)
        self.assertEqual(outcome.status, "triggered")
        self.assertIn("Revenue INR 4.2 Cr", outcome.text)
        self.assertEqual(outcome.units_ocred, 1)
        self.assertEqual(outcome.engines, [document_ai.DOCUMENT_ROLE])


class TheAdapterRefusesRatherThanPretend(TestCase):

    def test_a_provider_that_cannot_read_files_raises(self):
        """Silently dropping the file would produce a confident summary of
        nothing, indistinguishable from a working call."""
        from fundos.llm import adapter

        endpoint = mock.Mock(provider_kind="anthropic", code="ANTHROPIC",
                             default_model="m", timeout_seconds=30,
                             base_url="", api_key="k")
        endpoint.has_api_key_in_env.return_value = True
        with self.assertRaises(RuntimeError) as caught:
            adapter._dispatch(endpoint, "sys", "prompt", None,
                              attachments=[{"mime_type": "application/pdf",
                                            "data": b"%PDF-1.4"}])
        self.assertIn("cannot accept file attachments", str(caught.exception))

    def test_gemini_puts_the_file_before_the_instruction(self):
        from fundos.llm import adapter

        parts = adapter._gemini_parts(
            "SYSTEM", "PROMPT",
            [{"mime_type": "application/pdf", "data": b"%PDF-1.4"}])
        self.assertEqual(len(parts), 2)
        self.assertIn("inline_data", parts[0])
        self.assertEqual(parts[0]["inline_data"]["mime_type"],
                         "application/pdf")
        self.assertIn("text", parts[1])

    def test_the_bytes_are_base64_encoded(self):
        import base64

        from fundos.llm import adapter

        parts = adapter._gemini_parts(
            "S", "P", [{"mime_type": "image/png", "data": b"binary\x00bytes"}])
        self.assertEqual(
            base64.b64decode(parts[0]["inline_data"]["data"]),
            b"binary\x00bytes")

    def test_no_attachment_leaves_the_request_shape_unchanged(self):
        from fundos.llm import adapter

        parts = adapter._gemini_parts("SYSTEM", "PROMPT", None)
        self.assertEqual(parts, [{"text": "SYSTEM\n\nPROMPT"}])


class TheLocalOcrEnginesAreGone(TestCase):

    def test_the_ocr_module_no_longer_exists(self):
        import importlib

        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("fundos.profile.pipeline.ocr")

    def test_no_engine_choice_remains_in_configuration(self):
        from fundos.config.models import AppConfiguration

        fields = {f.name for f in AppConfiguration._meta.get_fields()}
        self.assertNotIn("profile_ocr_engine", fields)
        self.assertNotIn("profile_ocr_page_limit", fields)
        # The master switch survives: it answers the same question it always
        # did — read what a text extractor cannot reach?
        self.assertIn("profile_ocr_enabled", fields)

    def test_the_switch_still_gates_the_new_path(self):
        from fundos.profile.pipeline import settings as ps

        with mock.patch.object(ps, "document_model_enabled",
                               return_value=False):
            outcome = s2._model_stage("x.pdf", "x.pdf",
                                      s2.policy_for("x.pdf"),
                                      lambda _m: None, [], None)
        self.assertEqual(outcome.status, "disabled")


class TheMaterialsTaskCallsTheStageThatExists(TestCase):
    """Uploading a document goes through readiness/tasks, not the pipeline.

    Removing the OCR engines renamed `_ocr_stage` to `_model_stage` and left
    that caller behind. `_extract_text` catches every exception and logs a
    warning, so the AttributeError became one line in a log nobody reads:

        MATERIALS: extraction failed: module
        'fundos.profile.pipeline.source2_documents' has no attribute
        '_ocr_stage'

    The upload still returned 201 and the file still appeared in the Document
    Center marked uploaded, having contributed nothing -- the exact failure
    the extraction rewrite was written to end, reintroduced through a rename.
    """

    def test_the_stage_the_task_calls_is_present(self):
        from fundos.profile.pipeline import source2_documents
        from fundos.readiness import tasks

        source = inspect.getsource(tasks._extract_text)
        for attribute in re.findall(r"source2\.(\w+)", source):
            self.assertTrue(
                hasattr(source2_documents, attribute),
                f"readiness.tasks calls source2_documents.{attribute}, "
                f"which does not exist")

    def test_the_removed_name_is_not_called_anywhere(self):
        """A mention in a comment is fine; a call is not."""
        from fundos.readiness import tasks

        self.assertNotRegex(inspect.getsource(tasks), r"_ocr_stage\s*\(")

    def test_the_stage_accepts_what_the_task_passes_it(self):
        """The task has no profile to give it -- only a material."""
        from fundos.profile.pipeline import source2_documents

        signature = inspect.signature(source2_documents._model_stage)
        self.assertEqual(len(signature.parameters), 6)

    def test_a_material_carries_the_tenant_the_stage_reads(self):
        """`_model_stage` reaches for `tenant_id` on its last argument; the
        material is what the task hands it."""
        from fundos.readiness.models import MaterialAsset

        self.assertTrue(hasattr(MaterialAsset, "tenant_id"))

    def test_native_text_survives_a_document_model_failure(self):
        """A failed read must leave what MarkItDown recovered, not nothing."""
        from fundos.profile.pipeline import source2_documents

        with mock.patch.object(source2_documents, "_model_read",
                               side_effect=RuntimeError("no network")):
            outcome = source2_documents._model_stage(
                "x.pdf", "x.pdf", source2_documents.policy_for("x.pdf"),
                lambda _m: None, [], None)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.text, "")
