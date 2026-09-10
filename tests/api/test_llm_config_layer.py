from django.core.exceptions import ValidationError
from django.test import TestCase


class ConfigProfileValidation(TestCase):
    """Phase 1: the admin-controllable capability layer enforces the provider
    capability matrix and hard constraints (Feature Requirement Doc §4)."""

    def setUp(self):
        from fundos.llm.models import LLMEndpoint
        self.gemini_ep = LLMEndpoint.objects.create(
            code="GEMINI", provider_kind="gemini",
            base_url="https://generativelanguage.googleapis.com",
            default_model="gemini-2.5-flash", api_key_env_var="GOOGLE_AI_API_KEY")
        self.claude_ep = LLMEndpoint.objects.create(
            code="ANTHROPIC", provider_kind="anthropic",
            base_url="https://api.anthropic.com",
            default_model="claude-sonnet-4-6", api_key_env_var="ANTHROPIC_API_KEY")
        self.openai_ep = LLMEndpoint.objects.create(
            code="OPENAI", provider_kind="openai_chat",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4o", api_key_env_var="OPENAI_API_KEY")

    def _profile(self, **kw):
        from fundos.llm.models import LLMConfigProfile
        base = dict(code="p", provider="gemini",
                    model_string="gemini-2.5-flash", endpoint=self.gemini_ep)
        base.update(kw)
        return LLMConfigProfile(**base)

    def test_gemini_search_plus_function_calling_rejected(self):
        p = self._profile(web_search=True, function_calling=True)
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_gemini_search_only_ok(self):
        p = self._profile(code="g_ok", web_search=True, web_fetch=True)
        p.full_clean()  # should not raise (web_fetch → url_context on gemini)

    def test_claude_dynamic_filter_requires_code_exec(self):
        p = self._profile(code="c1", provider="anthropic",
                          model_string="claude-sonnet-4-6",
                          endpoint=self.claude_ep,
                          web_search=True, dynamic_filter=True)
        with self.assertRaises(ValidationError):
            p.full_clean()
        p.code_execution = True
        p.full_clean()  # now valid

    def test_domain_allow_block_mutually_exclusive(self):
        p = self._profile(code="c2", provider="anthropic",
                          model_string="claude-sonnet-4-6",
                          endpoint=self.claude_ep, web_search=True,
                          allowed_domains=["a.com"], blocked_domains=["b.com"])
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_unsupported_capability_rejected(self):
        # maps_grounding is Gemini-only; rejected on OpenAI.
        p = self._profile(code="o1", provider="openai", model_string="gpt-4o",
                          endpoint=self.openai_ep, maps_grounding=True)
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_endpoint_provider_must_match(self):
        p = self._profile(code="mm", provider="openai", model_string="gpt-4o",
                          endpoint=self.gemini_ep)   # gemini endpoint, openai profile
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_sub_controls_require_web_search(self):
        p = self._profile(code="c3", provider="anthropic",
                          model_string="claude-sonnet-4-6",
                          endpoint=self.claude_ep, max_uses=5)
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_capabilities_payload_drops_unsupported(self):
        """v27.5 — split. The original asserted that an advanced-tier Gemini

        profile keeps BOTH web_search and structured_output. Measured 19 Aug
        2026 on gemini-3.5-flash, they are mutually exclusive: a request
        carrying a responseSchema searched in 0 of 4 configurations, the same
        request without one in 4 of 4. The advanced tier is defined by
        retrieval, so the schema is what gives way.

        The provider-capability filtering this test was written to cover is
        asserted on the simple tier below, where there is no search tool for a
        schema to suppress.
        """
        p = self._profile(code="g2", tier="advanced", web_search=True,
                          max_uses=3, structured_output=True,
                          output_schema={"type": "object"})
        p.full_clean()
        payload = p.capabilities_payload()
        self.assertIn("web_search", payload)
        self.assertNotIn("structured_output", payload)

    def test_capabilities_payload_keeps_schema_on_a_non_search_tier(self):
        """A tier with no search tool has nothing to lose to the schema, so

        the original filtering behaviour is unchanged here.
        """
        p = self._profile(code="g3", tier="simple", structured_output=True,
                          output_schema={"type": "object"})
        p.full_clean()
        payload = p.capabilities_payload()
        self.assertIn("structured_output", payload)
        self.assertEqual(payload["structured_output"]["schema"],
                         {"type": "object"})


class ModelCatalog(TestCase):
    def test_hide_model_keeps_it_queryable(self):
        from fundos.llm.models import LLMModelCatalog
        m = LLMModelCatalog.objects.create(provider="gemini",
                                           model_string="gemini-3.0-flash",
                                           display_name="Gemini 3 Flash")
        m.is_active = False
        m.save()
        # hidden from active list but not deleted
        self.assertFalse(LLMModelCatalog.objects.get(id=m.id).is_active)
        self.assertEqual(
            LLMModelCatalog.objects.filter(provider="gemini",
                                           is_active=True,
                                           model_string="gemini-3.0-flash").count(),
            0)

    def test_seed_populates_catalog(self):
        from django.core.management import call_command
        from fundos.llm.models import LLMModelCatalog
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        self.assertTrue(LLMModelCatalog.objects.filter(provider="gemini").exists())
        self.assertTrue(LLMModelCatalog.objects.filter(provider="anthropic").exists())
        self.assertTrue(LLMModelCatalog.objects.filter(provider="openai").exists())
