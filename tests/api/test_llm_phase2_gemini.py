import json
from unittest.mock import patch, MagicMock
from django.test import TestCase


def _fake_gemini_response(text="{\"content\": \"ok\"}", with_grounding=False):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    cand = {"content": {"parts": [{"text": text}]}}
    if with_grounding:
        cand["groundingMetadata"] = {"groundingChunks": [
            {"web": {"uri": "https://example.com", "title": "Example"}}]}
    resp.json = lambda: {"candidates": [cand],
                         "usageMetadata": {"promptTokenCount": 5,
                                           "candidatesTokenCount": 7}}
    return resp


class GeminiCapabilityTranslation(TestCase):
    """Phase 2: capability payload → real Gemini API params."""

    def setUp(self):
        from fundos.llm.models import (LLMConfigProfile, LLMEndpoint,
                                       LLMRoleBinding)
        # Restored afterwards — see set_env.
        from tests.conftest_helpers import set_env
        set_env(self, GOOGLE_AI_API_KEY="test")
        self.ep = LLMEndpoint.objects.create(
            code="GEMINI", provider_kind="gemini",
            base_url="https://generativelanguage.googleapis.com",
            default_model="gemini-2.5-flash",
            api_key_env_var="GOOGLE_AI_API_KEY")

    def _run_with_profile(self, **profile_kw):
        from fundos.llm import adapter
        from fundos.llm.models import LLMConfigProfile
        base = dict(code="cp_extract.test", provider="gemini",
                    model_string="gemini-2.5-flash", endpoint=self.ep)
        base.update(profile_kw)
        p = LLMConfigProfile(**base); p.full_clean(); p.save()
        caps = p.capabilities_payload()
        captured = {}
        def fake_post(url, json=None, headers=None, timeout=None, **kw):
            captured["url"] = url; captured["body"] = json
            return _fake_gemini_response(with_grounding=True)
        with patch("fundos.llm.adapter.requests.post", side_effect=fake_post):
            text, usage = adapter._run_gemini(
                self.ep, "sys", "prompt", "gemini-2.5-flash", None, 2048, 60,
                capabilities=caps)
        return captured, text, usage

    def test_web_search_becomes_google_search_tool(self):
        cap, text, usage = self._run_with_profile(tier="advanced", web_search=True)
        tools = cap["body"].get("tools", [])
        self.assertIn({"google_search": {}}, tools)
        # grounding sources surfaced for observability
        self.assertTrue(usage.get("grounding_sources"))

    def test_url_context_and_search_off_for_basic_info(self):
        # Basic-Info style: URL context ON, search OFF (§5.4 exception).
        cap, _, _ = self._run_with_profile(web_fetch=True)
        tools = cap["body"].get("tools", [])
        self.assertIn({"url_context": {}}, tools)
        self.assertNotIn({"google_search": {}}, tools)

    def test_structured_output_sets_response_schema(self):
        schema = {"type": "object", "properties": {"content": {"type": "string"}}}
        cap, _, _ = self._run_with_profile(
            structured_output=True, output_schema=schema)
        gc = cap["body"]["generationConfig"]
        self.assertEqual(gc.get("responseMimeType"), "application/json")
        self.assertEqual(gc.get("responseSchema"), schema)

    def test_no_capabilities_sends_plain_request(self):
        from fundos.llm import adapter
        captured = {}
        def fake_post(url, json=None, headers=None, timeout=None, **kw):
            captured["body"] = json
            return _fake_gemini_response()
        with patch("fundos.llm.adapter.requests.post", side_effect=fake_post):
            adapter._run_gemini(self.ep, "s", "p", "gemini-2.5-flash",
                                None, 2048, 60, capabilities={})
        self.assertNotIn("tools", captured["body"])


class ProfileConsumesConfigProfile(TestCase):
    """A per-section config profile drives that section's generation."""

    def setUp(self):
        from django.core.management import call_command
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)

    def test_section_profile_code_convention(self):
        from fundos.profile.services import _section_config_profile
        self.assertEqual(_section_config_profile("founders"),
                         "cp_extract.founders")

    def test_missing_profile_is_backward_compatible(self):
        # With no cp_extract.* profiles configured, generation still runs
        # (mocked) and populates — i.e. opt-in, non-breaking.
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo(); cfg.ai_mocked = True; cfg.save()
        from tests.conftest_helpers import make_world
        w = make_world()
        from fundos.profile.services import (generate_profile,
                                             get_or_create_profile)
        profile = get_or_create_profile(w["a"]["company"], user=w["a"]["founder"])
        res = generate_profile(profile, user=w["a"]["founder"])
        self.assertTrue(res["generated"])
