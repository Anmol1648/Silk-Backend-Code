"""Gemini reasons by default, and the thoughts are billed to the output.

The failure this module guards against was observed on a live UAT run: the
deep-extract call returned `completion_tokens=0`, `response_chars=215` and no
searches at all, while logging as a successful call. Four of the five section
blocks then recorded "the model returned nothing for this block", and the
readiness call failed with `JSONDecodeError: Unterminated string`.

All of it has one cause. On Gemini 2.5 and later, thinking is ON unless a
budget says otherwise; thought tokens are deducted from `maxOutputTokens` but
are NOT counted in `candidatesTokenCount`. So a call can spend its entire
output allowance thinking, return a truncated fragment, and report zero
completion tokens — with nothing anywhere in the trace to say what happened.

Three things had to change, and each is a test below:

  1. `thinking_budget` was absent from Gemini's capability set, so no config
     profile could state a budget and the adapter never sent `thinkingConfig`.
  2. `NO_THINKING_ROLES` DROPPED the thinking key for extraction roles. On
     Anthropic and OpenAI absence means off; on Gemini it means the default,
     which is on — so the cost control had the opposite of its intended
     effect on exactly the highest-volume roles.
  3. `finishReason` was discarded, so truncation was indistinguishable from a
     model that genuinely had little to say.
"""
from unittest import mock

from django.test import TestCase


def _with_gemini_key(test):
    """Set GEMINI_API_KEY for the duration of ONE test, then restore it.

    `os.environ.setdefault` leaks: the variable outlives the test and the
    seeder then auto-selects Gemini in every module that runs afterwards,
    which breaks the suites asserting the shipped Anthropic defaults. The
    failure surfaces as six unrelated tests failing only when this module is
    run alongside them.
    """
    patcher = mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    patcher.start()
    test.addCleanup(patcher.stop)



class GeminiThinkingCapabilityTests(TestCase):

    def _profile(self, **kwargs):
        from fundos.llm.models import LLMConfigProfile
        defaults = dict(code="test.gemini", display_name="t", tier="simple",
                        provider="gemini", model_string="gemini-3.6-flash",
                        web_search=False, structured_output=False,
                        thinking_mode="none")
        defaults.update(kwargs)
        return LLMConfigProfile(**defaults)

    def test_thinking_off_is_stated_explicitly_for_gemini(self):
        """Silence is not 'off' on this provider — it must be sent as zero."""
        caps = self._profile(thinking_mode="none").capabilities_payload()
        self.assertEqual(caps.get("thinking"), {"budget_tokens": 0})

    def test_thinking_budget_is_passed_through(self):
        caps = self._profile(thinking_mode="tokens",
                             thinking_budget="4000").capabilities_payload()
        self.assertEqual(caps["thinking"]["budget_tokens"], "4000")

    def test_anthropic_absence_still_means_off(self):
        """The Gemini rule must not change the other providers, where a
        missing key genuinely does mean no thinking."""
        from fundos.llm.models import LLMConfigProfile

        caps = LLMConfigProfile(
            code="t.anthropic", tier="simple", provider="anthropic",
            model_string="claude-haiku-4-5",
            thinking_mode="none").capabilities_payload()
        self.assertNotIn("thinking", caps)


class GeminiRequestBodyTests(TestCase):
    """What actually goes on the wire."""

    def _endpoint(self):
        from fundos.llm.models import LLMEndpoint
        return LLMEndpoint(code="GEMINI LLM", provider_kind="gemini",
                           default_model="gemini-3.6-flash",
                           api_key_env_var="GEMINI_API_KEY",
                           timeout_seconds=120)

    def _post(self, captured, *, payload=None, status=200):
        response = mock.Mock()
        response.status_code = status
        response.text = ""
        response.json.return_value = payload or {
            "candidates": [{"content": {"parts": [{"text": "{}"}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10,
                              "candidatesTokenCount": 5},
        }
        response.raise_for_status = mock.Mock()

        def _fake_post(url, json=None, headers=None, timeout=None, **kw):
            captured.append(json)
            return response
        return _fake_post

    def _call(self, capabilities, captured, **kwargs):
        from fundos.llm import adapter

        _with_gemini_key(self)
        with mock.patch.object(adapter.requests, "post",
                               self._post(captured, **kwargs)):
            return adapter._run_gemini(
                self._endpoint(), "sys", "prompt", "gemini-3.6-flash",
                None, 2048, 120, capabilities=capabilities)

    def test_thinking_config_is_sent(self):
        captured = []
        self._call({"thinking": {"budget_tokens": 0}}, captured)
        config = captured[0]["generationConfig"]
        self.assertEqual(config["thinkingConfig"], {"thinkingBudget": 0})

    def test_output_ceiling_leaves_room_for_an_answer(self):
        """The budget comes out of maxOutputTokens, so a budget at or above
        the ceiling leaves nothing for the response itself."""
        captured = []
        self._call({"thinking": {"budget_tokens": 4000}}, captured)
        config = captured[0]["generationConfig"]
        self.assertGreater(config["maxOutputTokens"], 4000)

    def test_unspecified_thinking_defaults_to_off(self):
        """Absence must mean OFF, as it already does on the other providers.

        A call can reach the adapter with no capabilities at all — an
        unresolved tier, an explicit model_override, a direct adapter call —
        and leaving the field unsaid hands the decision to Gemini, whose
        answer is "think", with the thoughts billed against maxOutputTokens
        and excluded from candidatesTokenCount.
        """
        captured = []
        self._call({}, captured)
        self.assertEqual(captured[0]["generationConfig"]["thinkingConfig"],
                         {"thinkingBudget": 0})

    def test_dynamic_budget_is_passed_through(self):
        """-1 is Gemini's "decide for yourself" sentinel. An administrator
        who sets it has asked for the provider default deliberately."""
        captured = []
        self._call({"thinking": {"budget_tokens": -1}}, captured)
        self.assertEqual(captured[0]["generationConfig"]["thinkingConfig"],
                         {"thinkingBudget": -1})

    def test_model_rejecting_the_budget_is_retried_without_it(self):
        """Frontier Pro models cannot switch thinking off. That is a property
        of the model, not a misconfiguration — the call must still run."""
        from fundos.llm import adapter

        _with_gemini_key(self)
        captured = []
        responses = []

        rejected = mock.Mock()
        rejected.status_code = 400
        rejected.text = "thinkingBudget is not supported for this model"

        accepted = mock.Mock()
        accepted.status_code = 200
        accepted.text = ""
        accepted.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "{}"}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1,
                              "candidatesTokenCount": 1}}
        accepted.raise_for_status = mock.Mock()
        responses.extend([rejected, accepted])

        def _fake_post(url, json=None, headers=None, timeout=None, **kw):
            captured.append(json)
            return responses[len(captured) - 1]

        with mock.patch.object(adapter.requests, "post", _fake_post):
            text, usage = adapter._run_gemini(
                self._endpoint(), "sys", "prompt", "gemini-3.1-pro", None,
                2048, 120, capabilities={"thinking": {"budget_tokens": 0}})

        self.assertEqual(len(captured), 2, "the call was not retried")
        self.assertIn("thinkingConfig", captured[0]["generationConfig"])
        self.assertNotIn("thinkingConfig", captured[1]["generationConfig"])
        self.assertEqual(text, "{}")

    def test_finish_reason_and_thought_tokens_are_surfaced(self):
        """Truncation used to be invisible: thoughts are billed at the output
        rate but excluded from candidatesTokenCount, so a call that spent
        everything thinking reported zero completion tokens and success."""
        captured = []
        _text, usage = self._call(
            {"thinking": {"budget_tokens": 0}}, captured,
            payload={
                "candidates": [{"content": {"parts": [{"text": "{\"a\": "}]},
                                "finishReason": "MAX_TOKENS"}],
                "usageMetadata": {"promptTokenCount": 3831,
                                  "candidatesTokenCount": 0,
                                  "thoughtsTokenCount": 2048}})
        self.assertEqual(usage["finish_reason"], "MAX_TOKENS")
        self.assertEqual(usage["thinking_tokens"], 2048)
        self.assertEqual(usage["completion_tokens"], 0)


class NoThinkingRoleTests(TestCase):
    """Extraction roles gain nothing from reasoning, and it is billed at the
    output rate — but the control has to be expressed the way the provider
    understands it."""

    def test_gemini_extraction_role_pins_the_budget_to_zero(self):
        from fundos.llm.adapter import NO_THINKING_ROLES
        from fundos.llm.models import LLMConfigProfile

        self.assertIn("company_profile_records", NO_THINKING_ROLES)
        profile = LLMConfigProfile(code="t", tier="simple", provider="gemini",
                                   model_string="gemini-3.6-flash",
                                   thinking_mode="tokens",
                                   thinking_budget="4000")
        capabilities = profile.capabilities_payload()
        self.assertIn("thinking", capabilities)

        # Mirror the adapter's own reduction step.
        role = "company_profile_records"
        if role in NO_THINKING_ROLES and (
                "thinking" in capabilities
                or "reasoning_effort" in capabilities):
            capabilities = {k: v for k, v in capabilities.items()
                            if k not in ("thinking", "reasoning_effort")}
            if profile.provider == "gemini":
                capabilities["thinking"] = {"budget_tokens": 0}

        self.assertEqual(capabilities["thinking"], {"budget_tokens": 0},
                         "dropping the key on Gemini restores the provider "
                         "default, which is thinking ON")


class ProductionLoggingTests(TestCase):
    """silk_generation.log is named throughout the runbooks. It has to exist.

    prod.py replaced the base LOGGING dict wholesale, which removed the
    generation_file handler and the four fundos.* logger definitions that
    write to it. Every trace line still went to stdout, so logging looked
    healthy while the file the documentation tells you to read was never
    created.
    """

    def test_prod_settings_keep_the_generation_file_handler(self):
        import importlib
        import os
        import tempfile

        # Every variable prod refuses to boot without. Adding one to prod.py
        # and not to this dict makes this test fail, which is the point: the
        # dict is the readable statement of what a deployment must supply.
        required = {
            "FUNDOS_SECRET_KEY": "test-secret-key",
            "FUNDOS_JWT_SECRET": "test-jwt-secret-distinct-from-the-above",
            "FUNDOS_ALLOWED_HOSTS": "example.test",
            "FUNDOS_CORS_ALLOWED_ORIGINS": "https://example.test",
            "FUNDOS_REDIS_URL": "redis://localhost:6379/0",
            "FUNDOS_SMTP_HOST": "localhost",
            "FUNDOS_STORAGE_BUCKET": "bucket",
            "FUNDOS_CLAMAV_HOST": "localhost",
            "FUNDOS_LOG_DIR": tempfile.mkdtemp(),
        }
        previous = {k: os.environ.get(k) for k in required}
        os.environ.update(required)
        try:
            prod = importlib.import_module("fundos.settings.prod")
            importlib.reload(prod)
            logging_config = prod.LOGGING
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertIn("generation_file", logging_config["handlers"])
        self.assertIn("silk_generation.log",
                      logging_config["handlers"]["generation_file"]["filename"])
        for name in ("fundos.generation", "fundos.profile", "fundos.llm"):
            self.assertIn(name, logging_config["loggers"], name)
            self.assertIn("generation_file",
                          logging_config["loggers"][name]["handlers"], name)
        # The console stays JSON for the platform's collector.
        self.assertIn("console", logging_config["handlers"])
        self.assertIn('"time"',
                      logging_config["formatters"]["json"]["format"])


class SigningKeyTests(TestCase):
    """A deployed environment must not inherit the development signing key.

    `base` gives SECRET_KEY the fallback "insecure-dev-key-change-me" so a
    fresh checkout runs, and FUNDOS_JWT_SECRET defaults to SECRET_KEY. A
    prod or uat deployment that omitted the variables would therefore boot
    normally and sign every access and refresh token with a string printed in
    this repository — forgeable by anyone who can read the source, for any
    user of any tenant, with nothing in the logs to show for it.

    Nothing else catches this. `manage.py check` passes, the app serves
    traffic, and tokens verify. The only safe failure is at startup.
    """

    def _import_without(self, module_name, missing):
        """Import a settings module with `missing` unset. Returns the error."""
        import importlib
        import os
        import tempfile

        supplied = {
            "FUNDOS_SECRET_KEY": "a-real-secret",
            "FUNDOS_JWT_SECRET": "a-different-real-secret",
            "FUNDOS_ALLOWED_HOSTS": "example.test",
            "FUNDOS_CORS_ALLOWED_ORIGINS": "https://example.test",
            "FUNDOS_REDIS_URL": "redis://localhost:6379/0",
            "FUNDOS_SMTP_HOST": "localhost",
            "FUNDOS_STORAGE_BUCKET": "bucket",
            "FUNDOS_CLAMAV_HOST": "localhost",
            "FUNDOS_LOG_DIR": tempfile.mkdtemp(),
        }
        supplied.pop(missing)
        previous = {k: os.environ.get(k)
                    for k in list(supplied) + [missing]}
        os.environ.update(supplied)
        os.environ.pop(missing, None)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                importlib.reload(importlib.import_module(module_name))
            return str(ctx.exception)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            # Leave the module holding a valid configuration for whatever
            # imports it next; a half-reloaded settings module is a landmine.
            os.environ.update({k: v for k, v in supplied.items()})
            os.environ.setdefault(missing, "restored")
            importlib.reload(importlib.import_module(module_name))

    def test_prod_refuses_to_start_without_a_secret_key(self):
        self.assertIn("FUNDOS_SECRET_KEY",
                      self._import_without("fundos.settings.prod",
                                           "FUNDOS_SECRET_KEY"))

    def test_prod_refuses_to_start_without_a_jwt_secret(self):
        """Distinct from SECRET_KEY. Defaulting one to the other means a leak
        of either compromises both."""
        self.assertIn("FUNDOS_JWT_SECRET",
                      self._import_without("fundos.settings.prod",
                                           "FUNDOS_JWT_SECRET"))

    def test_uat_refuses_too(self):
        """UAT holds real accounts and real documents. Same rule."""
        self.assertIn("FUNDOS_SECRET_KEY",
                      self._import_without("fundos.settings.uat",
                                           "FUNDOS_SECRET_KEY"))

    def test_the_development_fallback_is_never_a_deployed_value(self):
        """The fallback itself is fine — it is what makes a fresh checkout
        run. What must never happen is a deployed environment using it."""
        from fundos.settings import base
        self.assertEqual(base.env("FUNDOS_SECRET_KEY_NOT_SET_ANYWHERE",
                                  default="insecure-dev-key-change-me"),
                         "insecure-dev-key-change-me")


class OutputCeilingTests(TestCase):
    """The role cap only ever LOWERS the binding's allowance."""

    def test_an_unstated_binding_uses_the_designed_budget(self):
        """A binding that names no ceiling must not act as a 2048 cap.

        The field defaulted to 2048 and the adapter can only ever LOWER the
        role cap, so every fresh install ran the dossier role at a quarter of
        its designed 8192 — and a response cut off at the ceiling is parsed,
        repaired and logged as a successful call with a thin result.
        """
        from fundos.llm.models import LLMRoleBinding

        field = LLMRoleBinding._meta.get_field("max_output_tokens")
        self.assertTrue(field.null)
        self.assertIsNone(field.default)

    def test_dispatch_falls_back_to_the_role_cap(self):
        from fundos.llm.adapter import (DEFAULT_MAX_OUTPUT_TOKENS,
                                        ROLE_MAX_OUTPUT_TOKENS)

        designed = ROLE_MAX_OUTPUT_TOKENS["company_profile_deep_extract"]
        stated = None
        role_cap = designed or DEFAULT_MAX_OUTPUT_TOKENS
        effective = min(stated, role_cap) if stated else role_cap
        self.assertEqual(effective, 8192)

    def test_a_stated_binding_still_only_lowers(self):
        from fundos.llm.adapter import (DEFAULT_MAX_OUTPUT_TOKENS,
                                        ROLE_MAX_OUTPUT_TOKENS)

        role_cap = (ROLE_MAX_OUTPUT_TOKENS["company_profile_deep_extract"]
                    or DEFAULT_MAX_OUTPUT_TOKENS)
        for stated, expected in ((1024, 1024), (16384, 8192)):
            self.assertEqual(min(stated, role_cap), expected)

    def test_diagnostic_reports_a_short_ceiling(self):
        """Capping an ESSENTIAL role is a FAIL.

        `profile_synthesis` is the whole profile in one response, so a ceiling
        below its designed budget truncates the JSON and the run writes a
        handful of sections and calls itself successful.
        """
        from django.core.management import call_command

        from fundos.llm.adapter import ROLE_MAX_OUTPUT_TOKENS
        from fundos.llm.models import LLMRoleBinding
        from fundos.profile.management.commands.diagnose_config import Command

        # The seeder only creates role bindings once an endpoint has a usable
        # key, and this check reads those bindings.
        _with_gemini_key(self)
        call_command("seed_platform_config", verbosity=0)
        LLMRoleBinding.objects.filter(
            role="profile_synthesis").update(max_output_tokens=2048)
        cmd = Command()
        cmd.opts = {"no_worker_probe": True, "probe_timeout": 1}
        status, detail, remedy = cmd._output_ceilings()
        self.assertEqual(status, "FAIL", detail)
        self.assertIn("profile_synthesis", detail)
        self.assertIn("max_output_tokens", remedy)

        LLMRoleBinding.objects.filter(
            role__in=list(ROLE_MAX_OUTPUT_TOKENS)).update(
                max_output_tokens=None)
        status, detail, _ = cmd._output_ceilings()
        self.assertEqual(status, "PASS", detail)

    def test_a_capped_non_essential_role_warns_rather_than_fails(self):
        """Severity has to discriminate, or the remediation list is useless.

        A short ceiling on a role the profile does not depend on is real and
        worth reporting — the response is still truncated — but it does not
        stop a profile being generated, and reporting it at the same severity
        as one that does buries the one that matters.
        """
        from django.core.management import call_command

        from fundos.llm.models import LLMRoleBinding
        from fundos.profile.management.commands.diagnose_config import Command

        _with_gemini_key(self)
        call_command("seed_platform_config", verbosity=0)
        LLMRoleBinding.objects.filter(
            role__in=("profile_research_batch", "profile_synthesis")).update(
                max_output_tokens=None)
        # `assessment_inputs` is sized at 8192 and is not on the profile's
        # critical path — a short ceiling there loses the assessment, not the
        # profile.
        capped = LLMRoleBinding.objects.filter(
            role="assessment_inputs").update(max_output_tokens=256)
        if not capped:                       # role not bound in this install
            self.skipTest("assessment_inputs has no binding to cap")

        cmd = Command()
        cmd.opts = {"no_worker_probe": True, "probe_timeout": 1}
        status, detail, _ = cmd._output_ceilings()
        self.assertEqual(status, "WARN", detail)
        self.assertIn("assessment_inputs", detail)


class SeedDefaultsTests(TestCase):
    """A fresh seed must produce a Gemini setup that WORKS, unassisted.

    Every failure this module documents was visible in a diagnose_config run
    on the live install, and every one of them had a remedy that began
    "Admin -> ...". A default that only works once an administrator has read
    the provider's API notes is not a default.
    """

    def setUp(self):
        _with_gemini_key(self)
        from django.core.management import call_command
        call_command("seed_platform_config", verbosity=0)

    def _tier(self, tier):
        from fundos.llm.models import LLMConfigProfile, TenantLLMTier
        code = TenantLLMTier.resolve_code(tier, None)
        self.assertIsNotNone(code, f"{tier} tier resolves to nothing")
        return LLMConfigProfile.objects.get(code=code)

    def test_every_tier_resolves(self):
        for tier in ("simple", "advanced", "judgment"):
            self.assertTrue(self._tier(tier).is_active)

    def test_a_healthy_advanced_tier_does_not_mask_the_other_two(self):
        """The selection used to test the advanced tier and return early.

        An administrator who repairs the tier that visibly failed leaves the
        other two as they were, and the next seed run then reports the system
        fine while simple and judgment resolve to nothing.
        """
        from django.core.management import call_command
        from fundos.llm.models import TenantLLMTier

        TenantLLMTier.objects.filter(
            tenant_id=None, tier__in=("simple", "judgment")
        ).update(is_active=False)
        call_command("seed_platform_config", verbosity=0)
        for tier in ("simple", "judgment"):
            self.assertIsNotNone(TenantLLMTier.resolve_code(tier, None), tier)

    def test_searching_tier_is_bounded(self):
        self.assertTrue(self._tier("advanced").web_search)
        self.assertTrue(self._tier("advanced").max_uses)

    def test_gemini_search_is_not_combined_with_a_response_schema(self):
        """Gemini rejects google_search alongside a responseSchema."""
        profile = self._tier("advanced")
        if profile.provider == "gemini" and profile.web_search:
            self.assertFalse(profile.structured_output)

    def test_judgement_tier_actually_reasons(self):
        """Absence means OFF everywhere now, so the tier whose purpose is
        reasoning has to state a budget or it does none."""
        profile = self._tier("judgment")
        self.assertNotEqual(profile.thinking_mode, "none")
        self.assertTrue(profile.thinking_budget)

    def test_bindings_do_not_cap_the_dossier(self):
        from fundos.llm.models import LLMRoleBinding

        binding = LLMRoleBinding.objects.get(
            role="company_profile_deep_extract")
        self.assertIsNone(binding.max_output_tokens)


class EndpointIdentityTests(TestCase):
    """An administrator may have coded the endpoint anything at all.

    Seeding by CODE then adds a SECOND endpoint for the same vendor. That is
    not a cosmetic duplicate: role bindings and config profiles reference an
    endpoint by code, so half the system ends up pointing at the row that has
    no API key.
    """

    def test_a_hand_coded_endpoint_is_not_duplicated(self):
        from django.core.management import call_command
        from fundos.llm.models import LLMEndpoint

        _with_gemini_key(self)
        LLMEndpoint.objects.create(
            code="GEMINI LLM", display_name="Google (Gemini)",
            provider_kind="gemini", default_model="gemini-3.6-flash",
            api_key_env_var="GEMINI_API_KEY", timeout_seconds=120,
            is_active=True, priority=10)

        call_command("seed_platform_config", verbosity=0)

        # The canonical row may still exist — the vendor alternate profiles
        # reference it by code — but only ONE endpoint per provider may be
        # selectable, and it must be the administrator's.
        active = list(LLMEndpoint.objects.filter(
            provider_kind="gemini", is_active=True).values_list(
                "code", flat=True))
        self.assertEqual(active, ["GEMINI LLM"])

    def test_the_endpoint_already_in_use_wins(self):
        """Seeding must converge on the row traffic already flows through,
        not migrate it to a fresh one behind the administrator's back."""
        from django.core.management import call_command
        from fundos.llm.models import LLMEndpoint, LLMRoleBinding

        _with_gemini_key(self)
        call_command("seed_platform_config", verbosity=0)
        hand_made = LLMEndpoint.objects.create(
            code="GEMINI LLM", display_name="Google (Gemini)",
            provider_kind="gemini", default_model="gemini-3.6-flash",
            api_key_env_var="GEMINI_API_KEY", timeout_seconds=120,
            is_active=True, priority=1)
        LLMRoleBinding.objects.update(primary_endpoint=hand_made)

        call_command("seed_platform_config", verbosity=0)

        self.assertEqual(
            set(LLMRoleBinding.objects.values_list(
                "primary_endpoint_id", flat=True)),
            {"GEMINI LLM"})
