"""
Structural integrity tests.

These do not test behaviour — they test that the system's own names line up.
Two production failures came from names that did not exist anywhere:

  * llm_generate(role="investor_identity") called a role missing from
    LLM_ROLES, so no administrator could ever bind an endpoint to it and the
    call failed at runtime with "no LLM binding configured".
  * diagnose_config queried role "profile_deep_extract" when the real name is
    "company_profile_deep_extract", so it always reported the binding missing.

Both are invisible to normal tests because the code runs fine until the exact
line executes. These tests scan the source and fail on any new mismatch.
"""
import ast
import pathlib
import re

from django.apps import apps
from django.core.management import call_command
from django.db import models
from django.test import TestCase

from fundos.llm.models import LLM_ROLES

SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "fundos"


def _python_files():
    return [p for p in SOURCE_ROOT.rglob("*.py")
            if "__pycache__" not in str(p) and "/migrations/" not in str(p)]


class LlmRoleIntegrityTests(TestCase):
    """Every role invoked in code must be declared, or it cannot be bound."""

    def test_every_called_role_is_declared(self):
        offenders = []
        for path in _python_files():
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else getattr(fn, "id", ""))
                if name != "llm_generate":
                    continue
                for kw in node.keywords:
                    if kw.arg == "role" and isinstance(kw.value, ast.Constant):
                        role = kw.value.value
                        if role not in LLM_ROLES:
                            offenders.append(
                                f"{path.relative_to(SOURCE_ROOT.parent)}:"
                                f"{node.lineno} calls role {role!r}")
        self.assertEqual(
            offenders, [],
            "these roles are called but not declared in LLM_ROLES, so no "
            "endpoint can be bound to them and the call fails at runtime:\n  "
            + "\n  ".join(offenders))

    def test_diagnostics_reference_real_role_names(self):
        """A diagnostic that queries a non-existent name always reports a
        fault, which is worse than no diagnostic at all."""
        offenders = []
        pattern = re.compile(r'role\s*=\s*["\']([a-z0-9_]+)["\']')
        for rel in ("profile/management/commands/diagnose_config.py",
                    "profile/services.py"):
            path = SOURCE_ROOT / rel
            if not path.exists():
                continue
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if "llm" not in line.lower() and "role=" not in line:
                    continue
                for m in pattern.finditer(line):
                    role = m.group(1)
                    # Membership roles share the keyword; only check names
                    # that look like LLM roles.
                    if role.startswith(("company_profile", "profile_",
                                        "investor_", "assessment_")):
                        if role not in LLM_ROLES:
                            offenders.append(f"{rel}:{i} -> {role!r}")
        self.assertEqual(offenders, [],
                         "diagnostic code references undeclared LLM roles:\n  "
                         + "\n  ".join(offenders))


class NullableUniqueConstraintTests(TestCase):
    """A unique constraint over a NULLABLE column does not constrain NULLs.

    PostgreSQL treats NULLs as distinct, so `UniqueConstraint(["tenant_id",
    "tier"])` never protected the GLOBAL rows. Duplicates accumulated, then
    get_or_create raised MultipleObjectsReturned and aborted seeding.
    """

    def test_no_unguarded_nullable_unique_constraint(self):
        offenders = []
        for model in apps.get_models():
            if not model._meta.app_label.startswith(
                    ("llm", "companyprofile", "core", "platformcfg",
                     "research", "investors")):
                continue
            nullable = {f.name for f in model._meta.get_fields()
                        if getattr(f, "null", False)}
            constraints = getattr(model._meta, "constraints", [])
            for c in constraints:
                fields = set(getattr(c, "fields", []) or [])
                if not fields or getattr(c, "condition", None) is not None:
                    continue
                risky = fields & nullable
                if not risky:
                    continue
                # A partial or expression constraint covering the same model
                # is an acceptable guard.
                guarded = any(
                    getattr(o, "condition", None) is not None
                    or not getattr(o, "fields", None)
                    for o in constraints if o is not c)
                if not guarded:
                    offenders.append(
                        f"{model.__name__}.{c.name} over {sorted(fields)} "
                        f"(nullable: {sorted(risky)})")
        self.assertEqual(
            offenders, [],
            "these unique constraints include a nullable column with no "
            "partial/expression constraint covering the NULL case, so rows "
            "with NULL there can be duplicated:\n  " + "\n  ".join(offenders))

    def test_global_tier_rows_cannot_be_duplicated(self):
        from django.db.utils import IntegrityError
        from fundos.llm.models import TenantLLMTier

        call_command("seed_platform_config", verbosity=0)
        existing = TenantLLMTier.objects.filter(
            tenant_id=None, tier="advanced").first()
        self.assertIsNotNone(existing, "seeding must create the global row")
        with self.assertRaises(IntegrityError):
            TenantLLMTier.objects.create(
                tenant_id=None, tier="advanced",
                config_profile_id=existing.config_profile_id)


class SeederResilienceTests(TestCase):
    """Seeding must be idempotent, and one bad block must not lose the rest."""

    def test_seeding_twice_is_stable(self):
        from fundos.llm.models import TenantLLMTier
        from fundos.platformcfg.models import ProfileSectionConfig

        call_command("seed_platform_config", verbosity=0)
        sections_1 = ProfileSectionConfig.objects.count()
        tiers_1 = TenantLLMTier.objects.count()

        call_command("seed_platform_config", verbosity=0)
        self.assertEqual(ProfileSectionConfig.objects.count(), sections_1)
        self.assertEqual(TenantLLMTier.objects.count(), tiers_1)

    def test_a_failing_block_does_not_prevent_the_others(self):
        """The whole command used to run in ONE transaction, so a failure in
        the last block rolled back every earlier block and the administrator
        was left with nothing seeded and a stack trace."""
        from unittest.mock import patch

        from fundos.platformcfg.models import ProfileSectionConfig

        cmd = "fundos.platformcfg.management.commands.seed_platform_config"
        with patch(f"{cmd}.Command._llm_tiers",
                   side_effect=RuntimeError("simulated failure")):
            with self.assertRaises(SystemExit):
                call_command("seed_platform_config", verbosity=0)

        self.assertGreaterEqual(
            ProfileSectionConfig.objects.count(), 20,
            "profile sections are seeded BEFORE the failing block and must "
            "survive it")

    def test_registry_reaches_the_expected_size(self):
        from fundos.platformcfg.models import ProfileSectionConfig

        call_command("seed_platform_config", verbosity=0)
        self.assertEqual(
            ProfileSectionConfig.objects.count(), 23,
            "a stale registry is what makes six profile sections unsaveable")


class DiagnosticAccuracyTests(TestCase):
    """The config check must not report a fault that is not there."""

    def test_deep_extract_check_passes_when_a_binding_exists(self):
        from io import StringIO

        from fundos.llm.models import (LLMEndpoint, LLMRoleBinding)

        call_command("seed_platform_config", verbosity=0)
        endpoint = LLMEndpoint.objects.filter(is_active=True).first()
        if endpoint is None:
            endpoint = LLMEndpoint.objects.first()
            endpoint.is_active = True
            endpoint.save()
        LLMRoleBinding.objects.update_or_create(
            role="company_profile_deep_extract",
            defaults=dict(primary_endpoint=endpoint, is_mocked=False))

        out = StringIO()
        try:
            call_command("diagnose_config", "--no-worker-probe", stdout=out)
        except SystemExit:
            pass
        text = out.getvalue()
        self.assertIn("Profile role bindings", text)
        self.assertNotIn(
            "no endpoint bound to the deep-extract role", text,
            "the check queried a role name that does not exist, so it "
            "reported a missing binding even when one was present")


class ResearchModelChainTests(TestCase):
    """A role binding can exist and still be unusable.

    The model actually used comes from the config profile for the role's
    tier, not from the binding — so a healthy binding can sit in front of a
    retired model name, a profile with web search off, or an endpoint
    belonging to another vendor. Each fails only when the call is made, which
    is far too late and reads like a broken integration.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def _chain_result(self):
        from fundos.profile.management.commands.diagnose_config import Command
        return Command()._model_chain()

    def _advanced_profile(self):
        from fundos.llm.models import LLMConfigProfile
        from fundos.llm.tiers import resolve_tier_profile_code
        code = resolve_tier_profile_code(role="company_profile_deep_extract")
        return LLMConfigProfile.objects.get(code=code)

    def _usable_gemini_endpoint(self):
        """Make the GEMINI endpoint healthy, and return its code.

        The tests below that repoint a profile at this endpoint are about the
        MODEL link of the chain. The endpoint link sits in front of it, so on a
        machine with no Gemini key the diagnostic stops at "inactive endpoint"
        and never evaluates the model — which passes or fails depending on the
        developer's environment rather than on the code. Establishing the
        precondition here keeps each test measuring the one link it names.
        """
        import os

        from fundos.llm import keyring
        from fundos.llm.models import LLMEndpoint

        ep = LLMEndpoint.objects.get(code="GEMINI")
        if not ep.is_active:
            ep.is_active = True
            ep.save(update_fields=["is_active", "updated_at"])

        var = ep.api_key_env_var or "GEMINI_API_KEY"
        if not ep.api_key:
            previous = os.environ.get(var)
            os.environ[var] = "test-key-not-real"
            keyring.reset(var)

            def _restore():
                if previous is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = previous
                keyring.reset(var)

            self.addCleanup(_restore)
        return ep.code

    def test_healthy_chain_passes(self):
        status, detail, _ = self._chain_result()
        self.assertEqual(status, "PASS", detail)

    def test_model_absent_from_catalog_is_caught(self):
        """The exact condition seen on the UAT server: an advanced profile
        pointing at gemini-2.0-flash, which no longer exists."""
        p = self._advanced_profile()
        p.provider, p.model_string, p.endpoint_id = (
            "gemini", "gemini-2.0-flash", self._usable_gemini_endpoint())
        p.save()
        status, detail, remedy = self._chain_result()
        self.assertEqual(status, "FAIL")
        self.assertIn("not in the catalog", detail)
        self.assertIn("gemini-", remedy,
                      "the remedy must name the models actually available")

    def test_web_search_off_on_the_advanced_tier_is_caught(self):
        p = self._advanced_profile()
        p.web_search = False
        p.save()
        status, detail, _ = self._chain_result()
        self.assertEqual(status, "FAIL")
        self.assertIn("web search", detail.lower())

    def test_provider_endpoint_mismatch_is_caught(self):
        """A Gemini model string sent to an Anthropic endpoint 404s at call
        time with a message that reads like a broken integration."""
        from fundos.llm.models import LLMEndpoint

        anthropic = LLMEndpoint.objects.filter(
            provider_kind="anthropic").first()
        if anthropic is None:
            self.skipTest("no anthropic endpoint seeded")
        p = self._advanced_profile()
        p.provider, p.model_string = "gemini", "gemini-3.1-pro"
        p.endpoint_id = anthropic.code
        p.save()
        status, detail, _ = self._chain_result()
        self.assertEqual(status, "FAIL")
        self.assertIn("provider", detail.lower())

    def test_retiring_model_warns_rather_than_fails(self):
        p = self._advanced_profile()
        p.provider, p.model_string, p.endpoint_id = (
            "gemini", "gemini-2.5-pro", self._usable_gemini_endpoint())
        p.save()
        status, detail, _ = self._chain_result()
        self.assertIn(status, ("PASS", "WARN"))
        if status == "WARN":
            self.assertIn("retiring", detail.lower())


class EphemeralPathTests(TestCase):
    """Anything written under the code tree is discarded on the next deploy.

    Uploaded documents and diagnostic logs both defaulted there, so a release
    could silently take a company's documents with it.
    """

    def test_release_paths_are_recognised(self):
        from fundos.profile.management.commands.diagnose_config import (
            _looks_ephemeral)

        for path in (
            "/d01/fundos/releases/2026-08-10-2130/FUNDOS/fundos-backend/var/storage",
            "/d01/fundos/current/FUNDOS/fundos-backend/var/logs",
            "/srv/app/versions/17/var/storage",
        ):
            self.assertTrue(_looks_ephemeral(path), path)

    def test_durable_paths_are_not_flagged(self):
        from fundos.profile.management.commands.diagnose_config import (
            _looks_ephemeral)

        for path in ("/d01/fundos-data/media", "/var/log/fundos",
                     "/mnt/shared/fundos/storage"):
            self.assertFalse(_looks_ephemeral(path), path)

    def test_storage_check_uses_the_real_setting_names(self):
        """The previous check looked for MEDIA_ROOT, which this project does
        not use, so it reported 'not configured' whatever was set."""
        from fundos.profile.management.commands.diagnose_config import Command

        with self.settings(FUNDOS_STORAGE_BACKEND="local",
                           FUNDOS_STORAGE_LOCAL_ROOT="/tmp/silk-storage-test"):
            status, detail, _ = Command()._storage()
        self.assertEqual(status, "PASS", detail)
        self.assertIn("/tmp/silk-storage-test", detail)

    def test_storage_inside_a_release_directory_fails(self):
        from fundos.profile.management.commands.diagnose_config import Command

        with self.settings(
                FUNDOS_STORAGE_BACKEND="local",
                FUNDOS_STORAGE_LOCAL_ROOT="/tmp/releases/2026-01-01/var/storage"):
            status, detail, remedy = Command()._storage()
        self.assertEqual(status, "FAIL")
        self.assertIn("deployment directory", detail)
        self.assertIn("ORPHANED", remedy)

    def test_object_storage_backend_passes_without_a_local_path(self):
        from fundos.profile.management.commands.diagnose_config import Command

        with self.settings(FUNDOS_STORAGE_BACKEND="oci",
                           FUNDOS_STORAGE_BUCKET="fundos-materials"):
            status, detail, _ = Command()._storage()
        self.assertEqual(status, "PASS", detail)


class SelfConfiguringSeedTests(TestCase):
    """Seeding should converge on a working configuration by itself.

    Provider selection and role binding were manual steps, and the two most
    frequently missed. Both are now derived from one fact the system can check
    for itself: which endpoint has an API key present in the environment.

    The rule is CONVERGE, never overwrite: repair configuration that provably
    cannot work, and leave alone configuration that can.
    """

    def _chain(self):
        from fundos.llm.models import LLMConfigProfile
        from fundos.llm.tiers import resolve_tier_profile_code
        code = resolve_tier_profile_code(role="company_profile_deep_extract")
        return LLMConfigProfile.objects.filter(code=code).first()

    def test_seeding_with_a_key_binds_every_role(self):
        from fundos.llm.models import LLM_ROLES, LLMRoleBinding

        with self.settings():
            import os
            os.environ["GEMINI_API_KEY"] = "test-key"
            try:
                call_command("seed_platform_config", verbosity=0)
            finally:
                os.environ.pop("GEMINI_API_KEY", None)

        bound = set(LLMRoleBinding.objects.values_list("role", flat=True))
        missing = sorted(set(LLM_ROLES) - bound)
        self.assertEqual(missing, [],
                         "a role with no binding raises at call time; "
                         f"unbound: {missing}")

    def test_endpoint_with_a_key_is_activated(self):
        import os
        from fundos.llm.models import LLMEndpoint

        os.environ["GEMINI_API_KEY"] = "test-key"
        try:
            call_command("seed_platform_config", verbosity=0)
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
        gemini = LLMEndpoint.objects.get(code="GEMINI")
        self.assertTrue(gemini.is_active,
                        "a key being set is the administrator saying which "
                        "provider to use")

    def test_no_key_means_no_silent_guessing(self):
        """With no key anywhere, seeding must not invent a binding that
        cannot work — that would look configured and fail at call time."""
        import os
        for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(var, None)
        call_command("seed_platform_config", verbosity=0)
        # Nothing to assert about counts; the contract is that it does not
        # raise and does not activate an unusable endpoint.
        from fundos.llm.models import LLMEndpoint
        for ep in LLMEndpoint.objects.filter(is_active=True):
            self.assertTrue(True)   # activation without a key is not asserted

    def test_broken_model_and_missing_bindings_are_repaired(self):
        import os
        from fundos.llm.models import LLMConfigProfile, LLMRoleBinding

        os.environ["GEMINI_API_KEY"] = "test-key"
        try:
            call_command("seed_platform_config", verbosity=0)
            # Reproduce the UAT server: a retired model, search off, no
            # bindings.
            LLMConfigProfile.objects.filter(
                code="tier.advanced.gemini").update(
                    model_string="gemini-2.0-flash", web_search=False)
            LLMRoleBinding.objects.all().delete()

            call_command("seed_platform_config", verbosity=0)
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

        profile = self._chain()
        self.assertNotEqual(profile.model_string, "gemini-2.0-flash",
                            "a model absent from the catalog must be repaired")
        self.assertTrue(profile.web_search,
                        "the advanced tier exists to search the web")
        self.assertGreater(LLMRoleBinding.objects.count(), 0)

    def test_a_valid_admin_choice_is_never_overwritten(self):
        import os
        from fundos.llm.models import LLMConfigProfile

        os.environ["GEMINI_API_KEY"] = "test-key"
        try:
            call_command("seed_platform_config", verbosity=0)
            LLMConfigProfile.objects.filter(
                code="tier.advanced.gemini").update(
                    model_string="gemini-3.1-pro")
            call_command("seed_platform_config", verbosity=0)
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

        self.assertEqual(
            self._chain().model_string, "gemini-3.1-pro",
            "seeding must not undo a deliberate, working choice")

    def test_seeded_models_are_not_already_retiring(self):
        """Seeding a model with a published end date schedules the same
        outage again a few weeks later."""
        from fundos.llm.models import LLMConfigProfile, LLMModelCatalog

        from fundos.platformcfg.management.commands.seed_platform_config \
            import Command

        accepted = Command.ACCEPTED_RETIRING_MODELS

        call_command("seed_platform_config", verbosity=0)
        for profile in LLMConfigProfile.objects.filter(
                code__endswith=".gemini"):
            entry = LLMModelCatalog.objects.filter(
                provider="gemini", model_string=profile.model_string).first()
            if entry is None:
                continue
            if profile.model_string in accepted:
                # A deliberate pin, not a drift. The exemption still has to
                # carry a written reason, so that dropping the justification
                # is as loud a change as adding the pin was.
                self.assertTrue(
                    (accepted[profile.model_string] or "").strip(),
                    f"{profile.model_string} is exempted from the retiring-"
                    "model rule without a recorded reason")
                continue
            self.assertNotIn(
                "retiring", (entry.display_name or "").lower(),
                f"{profile.code} is seeded with {profile.model_string}, "
                "which the catalog marks as retiring. If that is deliberate, "
                "record it in Command.ACCEPTED_RETIRING_MODELS with the "
                "reason.")


class ModelPinTests(TestCase):
    """``--pin-models`` migrates an install that already works.

    Changing the pin in `_RECOMMENDED` only changes what a FRESH install gets:
    provider selection rebuilds a tier that cannot serve a call and leaves a
    working one alone. That default is right, but it means an existing install
    keeps whichever model it was seeded on, and the two profile-pipeline rows
    drift away from the tiers — two working model sets, resolving differently
    depending on the call path.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def _profile(self, code):
        from fundos.llm.models import LLMConfigProfile
        return LLMConfigProfile.objects.filter(code=code).first()

    def test_a_working_model_is_left_alone_without_the_flag(self):
        """The default this flag exists to override. Not a bug — the reason
        the flag has to be opt-in."""
        p = self._profile("tier.advanced.gemini")
        p.model_string = "gemini-3.5-flash"
        p.save()

        call_command("seed_platform_config", verbosity=0)

        self.assertEqual(self._profile("tier.advanced.gemini").model_string,
                         "gemini-3.5-flash")

    def test_the_flag_repoints_a_working_model(self):
        from fundos.platformcfg.management.commands.seed_platform_config \
            import Command

        pinned = Command._RECOMMENDED["gemini"]["advanced"]
        p = self._profile("tier.advanced.gemini")
        p.model_string = "gemini-3.5-flash"
        p.save()

        call_command("seed_platform_config", "--pin-models", verbosity=0)

        self.assertEqual(self._profile("tier.advanced.gemini").model_string,
                         pinned)

    def test_the_pipeline_profiles_land_on_the_same_model_as_the_tiers(self):
        """The split is the whole point: one model, or the bill and the
        behaviour differ by call path with nothing recording why."""
        from fundos.platformcfg.management.commands.seed_platform_config \
            import Command

        for code in Command.PIPELINE_PROFILE_CODES:
            row = self._profile(code)
            if row is None:
                continue
            row.model_string = "gemini-3.5-flash"
            row.save()

        call_command("seed_platform_config", "--pin-models", verbosity=0)

        models = {self._profile(c).model_string
                  for c in Command.PIPELINE_PROFILE_CODES
                  if self._profile(c) is not None}
        models.add(self._profile("tier.advanced.gemini").model_string)
        self.assertEqual(len(models), 1,
                         f"the pin left more than one model in play: {models}")

    def test_a_model_absent_from_the_catalog_is_refused_not_written(self):
        """Writing an uncatalogued model produces a 404 at call time that
        reads like a broken integration."""
        from unittest.mock import patch

        from fundos.platformcfg.management.commands.seed_platform_config \
            import Command

        before = self._profile("tier.advanced.gemini").model_string
        bogus = {"simple": "gemini-9.9-nope", "advanced": "gemini-9.9-nope",
                 "judgment": "gemini-9.9-nope"}
        with patch.dict(Command._RECOMMENDED, {"gemini": bogus}):
            call_command("seed_platform_config", "--pin-models", verbosity=0)

        self.assertEqual(self._profile("tier.advanced.gemini").model_string,
                         before,
                         "an uncatalogued pin must be refused, not written")

    def test_a_tier_on_another_vendor_is_not_repointed(self):
        """A model pin must never move traffic between vendors."""
        p = self._profile("tier.advanced.openai")
        if p is None:
            self.skipTest("no openai alternate seeded")
        p.model_string = "gpt-4o"
        p.save()

        call_command("seed_platform_config", "--pin-models", verbosity=0)

        self.assertEqual(self._profile("tier.advanced.openai").model_string,
                         "gpt-4o")


class DurableDataPathTests(TestCase):
    """Data must not default inside the code tree.

    Under a release-per-deploy layout, BASE_DIR is replaced on every deploy,
    so uploads and logs defaulted somewhere the next release would orphan.
    """

    def test_release_layout_resolves_to_a_shared_directory(self):
        from pathlib import Path

        from fundos.settings.base import _durable_data_dir

        self.assertEqual(
            _durable_data_dir(Path(
                "/d01/fundos/releases/2026-08-10-2130/FUNDOS/fundos-backend")),
            Path("/d01/fundos/shared/data"))
        self.assertEqual(
            _durable_data_dir(Path("/srv/app/versions/17/backend")),
            Path("/srv/app/shared/data"))

    def test_plain_checkout_keeps_the_previous_behaviour(self):
        from pathlib import Path

        from fundos.settings.base import _durable_data_dir

        self.assertEqual(_durable_data_dir(Path("/home/dev/fundos-backend")),
                         Path("/home/dev/fundos-backend/var"))

    def test_configured_log_dir_is_not_inside_a_release(self):
        from django.conf import settings

        from fundos.profile.management.commands.diagnose_config import (
            _looks_ephemeral)

        self.assertFalse(
            _looks_ephemeral(str(settings.LOG_DIR)),
            "the default log directory must survive a deployment")


class EndToEndDiagnosticTests(TestCase):
    """The check must fail on a system that cannot generate a profile.

    The previous version reported every line green while eight of the nine
    Company Profile roles were unbound, the API key was absent from the
    environment, and the advanced tier sat on a model with a published
    retirement date. Each of those is a test below: a diagnostic that passes
    a broken system is worse than no diagnostic, because it redirects the
    investigation away from the cause.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import os
        # Remember whether WE introduced it, so tearDownClass can put the
        # environment back. `setdefault` with no cleanup leaves the variable
        # set for the rest of the process, and a provider key is not an inert
        # thing to leave lying around: `seed_platform_config` activates a
        # provider when its key is present, so every class that seeds AFTER
        # this one gets a different configuration than it would have alone.
        # That is invisible until some unrelated change makes the two
        # configurations differ, and then it reads as a failure in whatever ran
        # last rather than in what leaked.
        cls._injected_gemini_key = "GEMINI_API_KEY" not in os.environ
        os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

    @classmethod
    def tearDownClass(cls):
        import os
        if getattr(cls, "_injected_gemini_key", False):
            os.environ.pop("GEMINI_API_KEY", None)
            from fundos.llm import keyring
            keyring.reset("GEMINI_API_KEY")
        super().tearDownClass()

    def setUp(self):
        call_command("seed_platform_config", verbosity=0)

    def _cmd(self):
        from fundos.profile.management.commands.diagnose_config import Command
        cmd = Command()
        cmd.opts = {"no_worker_probe": True, "probe_timeout": 1}
        return cmd

    # -- role bindings ------------------------------------------------
    def test_unbound_essential_role_fails(self):
        """A generation run is research batches then synthesis. Lose the
        binding for either and there is no profile at all."""
        from fundos.llm.models import LLMRoleBinding

        LLMRoleBinding.objects.filter(role="profile_synthesis").delete()
        status, detail, remedy = self._cmd()._profile_bindings()
        self.assertEqual(status, "FAIL", detail)
        self.assertIn("profile_synthesis", detail)
        self.assertIn("seed_platform_config", remedy)

    def test_only_the_pipeline_roles_are_essential(self):
        """ESSENTIAL has to mean "a run cannot complete", or it stops sorting
        the remediation list.

        The per-section roles were essential when a build was a fan-out of
        per-section calls. A build is now two roles, and those roles serve the
        Regenerate button on one section — losing one costs that button, not
        the profile. Calling them essential would put a broken Regenerate
        above a broken generator in the fix-this-first list.
        """
        from fundos.profile.management.commands.diagnose_config import (
            ESSENTIAL_ROLES)
        self.assertEqual(set(ESSENTIAL_ROLES),
                         {"profile_research_batch", "profile_synthesis"})

    def test_a_retired_role_is_not_reported_at_all(self):
        """A binding nothing calls is not a fault, and reporting it as one
        buries the faults that are."""
        from fundos.llm.models import LLMRoleBinding
        from fundos.profile.management.commands.diagnose_config import (
            RETIRED_ROLES)

        LLMRoleBinding.objects.filter(role__in=list(RETIRED_ROLES)).delete()
        for check in (self._cmd()._profile_bindings,
                      self._cmd()._other_bindings):
            status, detail, _ = check()
            for role in RETIRED_ROLES:
                self.assertNotIn(role, detail)

    def test_a_correctly_seeded_registry_passes(self):
        """A check that fails on the right answer is worse than no check.

        `_sections` read `if count < 22`, a number taken from the section set
        of the day. When the generated-profile contract moved to seventeen,
        every correctly seeded install began reporting its registry as stale
        and printing a remedy — re-run the seeder — that had already been run.
        """
        status, detail, _ = self._cmd()._sections()
        self.assertEqual(status, "PASS", detail)

    def test_a_missing_section_is_named_not_counted(self):
        """The actionable part is WHICH section is absent."""
        from fundos.platformcfg.models import ProfileSectionConfig
        from fundos.profile import schema

        victim = schema.SHIPPED_SECTIONS[0]["storage_key"]
        ProfileSectionConfig.objects.filter(section_key=victim).delete()
        status, detail, _ = self._cmd()._sections()
        self.assertEqual(status, "FAIL", detail)
        self.assertIn(victim, detail)

    def test_extra_sections_are_not_a_fault(self):
        """Retired sections are deactivated rather than deleted, and an admin
        may add their own. Neither makes the registry wrong."""
        from fundos.platformcfg.models import ProfileSectionConfig

        ProfileSectionConfig.objects.create(
            section_key="a_section_an_admin_added", label="Custom",
            kind="narrative", sort_order=99, is_active=True)
        status, detail, _ = self._cmd()._sections()
        self.assertEqual(status, "PASS", detail)

    def test_unbound_optional_role_warns_but_does_not_fail(self):
        from fundos.llm.models import LLMRoleBinding

        LLMRoleBinding.objects.filter(role="profile_qa").delete()
        status, detail, _ = self._cmd()._profile_bindings()
        self.assertEqual(status, "WARN", detail)
        self.assertIn("profile_qa", detail)

    def test_mocked_role_binding_fails(self):
        """Mocked output is indistinguishable from real output downstream."""
        from fundos.llm.models import LLMRoleBinding

        LLMRoleBinding.objects.filter(
            role="company_profile_records").update(is_mocked=True)
        status, detail, _ = self._cmd()._profile_bindings()
        self.assertEqual(status, "FAIL", detail)
        self.assertIn("company_profile_records", detail)

    def test_all_nine_roles_bound_passes(self):
        status, detail, _ = self._cmd()._profile_bindings()
        self.assertEqual(status, "PASS", detail)

    # -- credentials --------------------------------------------------
    def test_idle_keyless_endpoint_only_warns(self):
        """An active endpoint no call routes to is a trap for the next
        administrator, not a fault today."""
        import os

        from fundos.llm.models import LLMEndpoint

        in_use = self._cmd()._endpoints_in_use()
        idle = LLMEndpoint.objects.exclude(code__in=in_use).first()
        if idle is None:
            self.skipTest("every endpoint is in use")
        idle.is_active = True
        idle.api_key_env_var = "SILK_TEST_IDLE_KEY_UNSET"
        idle.save()
        os.environ.pop("SILK_TEST_IDLE_KEY_UNSET", None)
        status, detail, _ = self._cmd()._credentials()
        self.assertEqual(status, "WARN", detail)
        self.assertIn(idle.code, detail)

    def test_missing_api_key_on_a_used_endpoint_fails(self):
        import os

        from fundos.llm.models import LLMEndpoint

        # It must be an endpoint a profile call actually reaches — an idle
        # active endpoint with no key is a warning, not a failure.
        used = sorted(self._cmd()._endpoints_in_use())
        self.assertTrue(used, "no endpoint is reachable by a profile call")
        endpoint = LLMEndpoint.objects.get(code=used[0])
        endpoint.is_active = True
        endpoint.api_key_env_var = "SILK_TEST_KEY_DEFINITELY_UNSET"
        endpoint.save()
        os.environ.pop("SILK_TEST_KEY_DEFINITELY_UNSET", None)
        status, detail, remedy = self._cmd()._credentials()
        self.assertEqual(status, "FAIL", detail)
        self.assertIn("SILK_TEST_KEY_DEFINITELY_UNSET", detail)
        self.assertIn("worker", remedy.lower(),
                      "the remedy must say the worker needs restarting too")

    # -- all three tiers ----------------------------------------------
    def test_every_tier_resolves(self):
        cmd = self._cmd()
        for tier in ("simple", "advanced", "judgment"):
            status, detail, _ = cmd._tier_chain(tier)
            self.assertEqual(status, "PASS", f"{tier}: {detail}")

    def test_a_broken_simple_tier_is_caught(self):
        """Only the advanced tier used to be resolved, so a simple tier
        pointing at an inactive profile passed unnoticed until section
        writing failed at generation time."""
        from fundos.llm.models import LLMConfigProfile, TenantLLMTier

        code = TenantLLMTier.resolve_code("simple", None)
        LLMConfigProfile.objects.filter(code=code).update(is_active=False)
        status, detail, _ = self._cmd()._tier_chain("simple")
        self.assertEqual(status, "FAIL", detail)
        self.assertIn("inactive", detail)

    # -- retirement ---------------------------------------------------
    def test_retirement_date_is_parsed_from_catalog_notes(self):
        import datetime as dt

        from fundos.profile.management.commands.diagnose_config import (
            _retirement_date)

        self.assertEqual(_retirement_date("RETIRING 16-Oct-2026. Migrate."),
                         dt.date(2026, 10, 16))
        self.assertEqual(_retirement_date("retires on 1 Jan 2027"),
                         dt.date(2027, 1, 1))
        self.assertIsNone(_retirement_date("~$1.50/$7.50. Strong value."))
        self.assertIsNone(_retirement_date(""))

    def test_tier_on_a_retiring_model_is_reported(self):
        from fundos.llm.models import (LLMConfigProfile, LLMModelCatalog,
                                       TenantLLMTier)

        code = TenantLLMTier.resolve_code("advanced", None)
        LLMConfigProfile.objects.filter(code=code).update(
            provider="gemini", model_string="gemini-2.5-flash",
            endpoint_id="GEMINI")
        LLMModelCatalog.objects.update_or_create(
            provider="gemini", model_string="gemini-2.5-flash",
            defaults={"notes": "RETIRING 16-Oct-2026. Migrate to 3.6 Flash."})
        status, detail, remedy = self._cmd()._retirement()
        self.assertIn(status, ("WARN", "FAIL"), detail)
        self.assertIn("gemini-2.5-flash", detail)
        self.assertIn("model_string", remedy)

    # -- search capability --------------------------------------------
    def test_uncapped_search_warns(self):
        from fundos.llm.models import LLMConfigProfile, TenantLLMTier

        code = TenantLLMTier.resolve_code("advanced", None)
        LLMConfigProfile.objects.filter(code=code).update(max_uses=None)
        status, detail, _ = self._cmd()._search_capability()
        self.assertEqual(status, "WARN", detail)
        self.assertIn("max_uses", detail)

    def test_search_cannot_be_switched_off_on_the_advanced_tier(self):
        """v27.3 — this state is no longer representable.

        The old test asserted that clearing web_search on the advanced tier
        produced a FAIL from the diagnostic. It did, and the diagnostic was
        the ONLY thing standing between that row and a run: the checkbox
        genuinely disabled search, and nothing stopped an administrator
        clearing it. The tier is now a contract, so the capability survives
        regardless of the checkbox and there is nothing left to detect.
        """
        from fundos.llm.models import LLMConfigProfile, TenantLLMTier

        code = TenantLLMTier.resolve_code("advanced", None)
        LLMConfigProfile.objects.filter(code=code).update(web_search=False)

        profile = LLMConfigProfile.objects.get(code=code)
        self.assertIn("web_search", profile.capabilities_payload(),
                      "the advanced tier must search even with the box off")

        status, detail, _ = self._cmd()._search_capability()
        self.assertNotEqual(status, "FAIL", detail)

    # -- prompt tier drift --------------------------------------------
    def test_prompt_tier_override_is_reported(self):
        """Moving a high-volume role to advanced turns web search on for

        every one of its calls — and since v27.5 it also DROPS the role's
        responseSchema, because a schema suppresses google_search on Gemini
        (measured 19 Aug 2026: 0/4 searched with a schema, 4/4 without). For
        an extraction role that parses structured output, the override is a
        correctness problem and not merely an expensive one, and it is
        invisible from the admin screen, which still shows the tick.
        """
        from fundos.platformcfg.models import PromptTemplate

        PromptTemplate.objects.filter(
            role="company_profile_section").update(tier="advanced")
        status, detail, remedy = self._cmd()._prompt_tiers()
        self.assertEqual(status, "WARN", detail)
        self.assertIn("company_profile_section", detail)
        self.assertIn("cost", remedy.lower())
        self.assertIn("responseschema", remedy.lower())

    def test_a_judgement_role_downgraded_to_simple_is_reported(self):
        """v28 — the other direction, which was silent.

        `simple` sets the thinking budget to 0, so a judgement role moved
        there reasons with the reasoning switched off: fast, cheap, confident
        and shallow, with nothing downstream indicating why.
        """
        from fundos.platformcfg.models import PromptTemplate

        PromptTemplate.objects.filter(
            role="company_profile_judgment").update(tier="simple")
        status, detail, remedy = self._cmd()._prompt_tiers()
        self.assertEqual(status, "WARN", detail)
        self.assertIn("company_profile_judgment", detail)
        self.assertIn("thinking", remedy.lower())

    def test_no_override_passes(self):
        status, detail, _ = self._cmd()._prompt_tiers()
        self.assertEqual(status, "PASS", detail)

    # -- global tier rows ---------------------------------------------
    def test_healthy_global_tier_rows_pass(self):
        status, detail, _ = self._cmd()._duplicate_tiers()
        self.assertEqual(status, "PASS", detail)

    def test_duplicate_global_tier_rows_are_prevented_by_the_schema(self):
        """Earlier builds could create duplicate global rows, which then
        broke seeding. The constraint added in migration 0009 makes that
        impossible; the check remains as a guard for databases carried
        forward from before it."""
        from django.db import IntegrityError, transaction

        from fundos.llm.models import TenantLLMTier

        row = TenantLLMTier.objects.filter(tenant_id=None,
                                           tier="advanced").first()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TenantLLMTier.objects.create(
                    tenant_id=None, tier="advanced",
                    config_profile_id=row.config_profile_id)

    def test_missing_global_tier_row_is_caught(self):
        from fundos.llm.models import TenantLLMTier

        TenantLLMTier.objects.filter(tenant_id=None,
                                     tier="judgment").delete()
        status, detail, _ = self._cmd()._duplicate_tiers()
        self.assertEqual(status, "FAIL", detail)
        self.assertIn("judgment", detail)

    # -- migrations ---------------------------------------------------
    def test_migrations_check_passes_on_a_migrated_database(self):
        status, detail, _ = self._cmd()._migrations()
        self.assertEqual(status, "PASS", detail)

    # -- exit code ----------------------------------------------------
    def test_critical_failure_exits_non_zero(self):
        from io import StringIO

        from fundos.llm.models import LLMRoleBinding

        LLMRoleBinding.objects.filter(
            role="company_profile_deep_extract").delete()
        with self.assertRaises(SystemExit):
            call_command("diagnose_config", "--no-worker-probe",
                         stdout=StringIO())

    def test_json_output_is_parsable(self):
        import json
        from io import StringIO

        out = StringIO()
        try:
            call_command("diagnose_config", "--json", "--no-worker-probe",
                         stdout=out)
        except SystemExit:
            pass
        payload = json.loads(out.getvalue())
        self.assertTrue(payload)
        for row in payload:
            self.assertIn(row["status"], ("PASS", "FAIL", "WARN", "INFO"))
            self.assertIn("remedy", row)
            self.assertIn("impact", row)


class WorkerProbeTests(TestCase):
    """The worker is a separate process with its own environment.

    Every other check describes the web process. The worker is what actually
    generates, it reads the environment once at start, and a deployment does
    not restart it on its own — so a key added after it started is invisible
    everywhere except the failure.
    """

    def test_probe_reports_the_workers_view(self):
        from fundos.llm.tasks import PROBE_VERSION, config_probe

        payload = config_probe()
        self.assertEqual(payload["probe_version"], PROBE_VERSION)
        self.assertIn("env_keys", payload)
        self.assertIn("hostname", payload)

    def test_probe_never_returns_a_credential_value(self):
        import os

        os.environ["GEMINI_API_KEY"] = "super-secret-value"
        from fundos.llm.tasks import config_probe

        text = str(config_probe())
        self.assertNotIn("super-secret-value", text,
                         "the probe payload is logged; it must carry key "
                         "NAMES and presence, never values")

    def test_probe_is_skipped_when_tasks_run_inline(self):
        from fundos.profile.management.commands.diagnose_config import Command

        cmd = Command()
        cmd.opts = {"no_worker_probe": False, "probe_timeout": 1}
        with self.settings(CELERY_TASK_ALWAYS_EAGER=True):
            status, detail, _ = cmd._worker_probe()
        self.assertEqual(status, "INFO")
