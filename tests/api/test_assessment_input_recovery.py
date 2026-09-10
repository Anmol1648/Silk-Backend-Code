"""The cheap way back when step 1's parameter extraction fails.

`assessment_inputs` is one LLM call inside a long profile run. When it fails,
the profile still publishes, `ProfileAssessmentInput` stays empty, and the
scorecard renders blank. Re-running the whole profile to recover from one
refused call is 100 research questions and a 600,000-character synthesis; the
dossier is already stored, so the missing call can simply be made again.

This path was briefly removed when parameter extraction was folded into the
synthesis call. That refactor was reverted -- it took profile generation from
17 populated sections to 3 -- so the recovery is back, and these tests pin the
behaviour that made it worth having: it never raises, it refuses to act on a
profile that is not finished, and it only reports success when something was
actually written.
"""
from unittest import mock

from django.test import TestCase


class StoredDossierTests(TestCase):
    """Reading the dossier a previous run left in storage."""

    def test_a_missing_run_yields_empty_not_an_error(self):
        from fundos.assessment.tasks import _stored_dossier

        with mock.patch("fundos.profile.models.ProfileGenerationRun.objects"
                        ) as objects:
            objects.filter.return_value.exclude.return_value \
                .order_by.return_value.first.return_value = None
            self.assertEqual(_stored_dossier(mock.Mock()), "")

    def test_a_storage_failure_yields_empty_not_an_error(self):
        from fundos.assessment.tasks import _stored_dossier

        run = mock.Mock(dossier_uri="profiles/x/consolidated.md")
        storage = mock.Mock()
        storage.download_file.side_effect = RuntimeError("bucket is gone")
        with mock.patch("fundos.profile.models.ProfileGenerationRun.objects"
                        ) as objects, \
                mock.patch("fundos.docs.storage.get_storage",
                           return_value=storage):
            objects.filter.return_value.exclude.return_value \
                .order_by.return_value.first.return_value = run
            self.assertEqual(_stored_dossier(mock.Mock()), "")

    def test_it_never_raises(self):
        """A recovery path that can itself explode is not a recovery path."""
        from fundos.assessment.tasks import _stored_dossier

        with mock.patch("fundos.profile.models.ProfileGenerationRun.objects",
                        side_effect=Exception("boom")):
            self.assertEqual(_stored_dossier(mock.Mock()), "")


class RecoveryGateTests(TestCase):
    """When the sweep may run, and when it must not."""

    def _assessment(self):
        return mock.Mock(id="a1", company_id="c1")

    def test_no_profile_means_no_recovery(self):
        from fundos.assessment.tasks import _recover_profile_inputs

        with mock.patch("fundos.profile.models.CompanyProfile.objects"
                        ) as objects:
            objects.filter.return_value.order_by.return_value \
                .first.return_value = None
            self.assertIsNone(_recover_profile_inputs(self._assessment()))

    def test_an_unfinished_profile_is_not_re_extracted(self):
        """A draft has no dossier worth re-reading."""
        from fundos.assessment.tasks import _recover_profile_inputs

        with mock.patch("fundos.profile.models.CompanyProfile.objects"
                        ) as objects:
            objects.filter.return_value.order_by.return_value \
                .first.return_value = mock.Mock(status="draft")
            self.assertIsNone(_recover_profile_inputs(self._assessment()))

    def test_a_failed_retry_does_not_raise(self):
        from fundos.assessment.tasks import _recover_profile_inputs

        with mock.patch("fundos.profile.models.CompanyProfile.objects"
                        ) as objects, \
                mock.patch("fundos.profile.services.collect_sources",
                           side_effect=RuntimeError("no key")):
            objects.filter.return_value.order_by.return_value \
                .first.return_value = mock.Mock(status="generated")
            self.assertIsNone(_recover_profile_inputs(self._assessment()))

    def test_an_extraction_that_writes_nothing_is_not_treated_as_recovered(self):
        """Nothing written means nothing to seed, and the caller must know."""
        from fundos.assessment.tasks import _recover_profile_inputs

        with mock.patch("fundos.profile.models.CompanyProfile.objects"
                        ) as objects, \
                mock.patch("fundos.profile.services.collect_sources",
                           return_value=({}, None)), \
                mock.patch("fundos.assessment.tasks._stored_dossier",
                           return_value=""), \
                mock.patch("fundos.profile.assessment_extraction."
                           "extract_assessment_inputs",
                           return_value={"written": 0}):
            objects.filter.return_value.order_by.return_value \
                .first.return_value = mock.Mock(status="generated")
            self.assertIsNone(_recover_profile_inputs(self._assessment()))

    def test_a_successful_recovery_reseeds_the_bridge(self):
        from fundos.assessment.tasks import _recover_profile_inputs

        profile = mock.Mock(status="generated")
        with mock.patch("fundos.profile.models.CompanyProfile.objects"
                        ) as objects, \
                mock.patch("fundos.profile.services.collect_sources",
                           return_value=({}, None)), \
                mock.patch("fundos.assessment.tasks._stored_dossier",
                           return_value="DOSSIER"), \
                mock.patch("fundos.profile.assessment_extraction."
                           "extract_assessment_inputs",
                           return_value={"written": 12}), \
                mock.patch("fundos.assessment.profile_bridge."
                           "seed_from_profile",
                           return_value={"available": 12}) as seed:
            objects.filter.return_value.order_by.return_value \
                .first.return_value = profile
            result = _recover_profile_inputs(self._assessment())

        self.assertEqual(result, {"available": 12})
        seed.assert_called_once()

    def test_the_stored_dossier_is_handed_to_the_retry(self):
        """Without it the retry sees only per-file summaries and answers far
        fewer parameters than the run it is recovering."""
        from fundos.assessment.tasks import _recover_profile_inputs

        with mock.patch("fundos.profile.models.CompanyProfile.objects"
                        ) as objects, \
                mock.patch("fundos.profile.services.collect_sources",
                           return_value=({}, None)), \
                mock.patch("fundos.assessment.tasks._stored_dossier",
                           return_value="DOSSIER TEXT"), \
                mock.patch("fundos.profile.assessment_extraction."
                           "extract_assessment_inputs",
                           return_value={"written": 3}) as extract, \
                mock.patch("fundos.assessment.profile_bridge."
                           "seed_from_profile", return_value={"available": 3}):
            objects.filter.return_value.order_by.return_value \
                .first.return_value = mock.Mock(status="generated")
            _recover_profile_inputs(self._assessment())

        payloads = extract.call_args.kwargs["payloads"]
        self.assertEqual(payloads["dossier"], "DOSSIER TEXT")


class SynthesisStaysOnItsOwnJob(TestCase):
    """The reverted refactor must not creep back in unnoticed.

    Folding the sixty-one assessment parameters into the synthesis response
    took a live run from 17 populated sections to 3. Synthesis returns the
    profile and its summary, and nothing else.
    """

    def test_synthesis_returns_two_values(self):
        import inspect
        from fundos.profile.pipeline import synthesize

        source = inspect.getsource(synthesize.run)
        self.assertIn("return profile, summary", source)
        self.assertNotIn("assessment_inputs", source)

    def test_the_schema_block_is_not_augmented(self):
        import inspect
        from fundos.profile.pipeline import synthesize

        source = inspect.getsource(synthesize)
        self.assertNotIn("assessment_map", source)
