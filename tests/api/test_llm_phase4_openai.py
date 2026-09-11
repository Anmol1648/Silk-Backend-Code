from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from django.test import TestCase


class OpenAICapabilityTranslation(TestCase):
    """Phase 4: capability payload → real OpenAI params."""

    def setUp(self):
        # Restored afterwards: os.environ is process-wide, and a key left
        # behind here decides what a seeding test in another file resolves to.
        from tests.conftest_helpers import set_env
        set_env(self, OPENAI_API_KEY="test")
        from fundos.llm.models import LLMEndpoint
        self.ep = LLMEndpoint.objects.create(
            code="OPENAI", provider_kind="openai_chat",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4o", api_key_env_var="OPENAI_API_KEY")

    def _profile(self, **kw):
        from fundos.llm.models import LLMConfigProfile
        base = dict(code="cp.oai", provider="openai",
                    model_string="gpt-4o", endpoint=self.ep)
        base.update(kw)
        p = LLMConfigProfile(**base); p.full_clean(); p.save()
        return p

    def _fake_client(self, chat_capture, resp_capture, *, responses_output=None):
        chat_resp = MagicMock()
        chat_resp.choices = [SimpleNamespace(
            message=SimpleNamespace(content='{"content":"chat"}'))]
        chat_resp.usage = SimpleNamespace(prompt_tokens=3, completion_tokens=4)

        class FakeChatCompletions:
            def create(self, **kwargs):
                chat_capture.update(kwargs); return chat_resp

        class FakeResponses:
            def create(self, **kwargs):
                resp_capture.update(kwargs)
                return responses_output

        class FakeClient:
            def __init__(self, *a, **k):
                self.chat = SimpleNamespace(completions=FakeChatCompletions())
                self.responses = FakeResponses()
        return FakeClient

    def _run(self, profile, *, responses_output=None):
        from fundos.llm import adapter
        caps = profile.capabilities_payload()
        chat_cap, resp_cap = {}, {}
        FakeClient = self._fake_client(chat_cap, resp_cap,
                                       responses_output=responses_output)
        fake_openai = MagicMock(); fake_openai.OpenAI = FakeClient
        with patch.dict("sys.modules", {"openai": fake_openai}):
            text, usage = adapter._run_openai_chat(
                self.ep, "sys", "prompt", "gpt-4o", None, 2048, 60,
                capabilities=caps)
        return chat_cap, resp_cap, text, usage

    def test_structured_output_json_schema(self):
        schema = {"type": "object",
                  "properties": {"content": {"type": "string"}}}
        p = self._profile(structured_output=True, output_schema=schema)
        chat, resp, text, _ = self._run(p)
        rf = chat.get("response_format", {})
        self.assertEqual(rf.get("type"), "json_schema")
        self.assertEqual(rf["json_schema"]["schema"], schema)
        self.assertEqual(resp, {})   # chat path, not responses

    def test_reasoning_effort(self):
        p = self._profile(code="oai.think", thinking_mode="effort",
                          thinking_budget="high")
        chat, _, _, _ = self._run(p)
        self.assertEqual(chat.get("reasoning_effort"), "high")

    def test_web_search_routes_to_responses_api(self):
        # Build a fake Responses output with output_text + a url citation.
        ann = SimpleNamespace(url="https://src.com", title="Src")
        content = SimpleNamespace(text="grounded answer", annotations=[ann])
        item = SimpleNamespace(content=[content])
        out = SimpleNamespace(output=[item], output_text="grounded answer",
                              usage=SimpleNamespace(input_tokens=5,
                                                    output_tokens=6))
        p = self._profile(code="oai.search", tier="advanced", web_search=True)
        chat, resp, text, usage = self._run(p, responses_output=out)
        # went through responses API, not chat completions
        self.assertEqual(chat, {})
        self.assertIn("tools", resp)
        self.assertEqual(resp["tools"], [{"type": "web_search"}])
        self.assertEqual(text, "grounded answer")
        self.assertEqual(usage["grounding_sources"],
                         [{"uri": "https://src.com", "title": "Src"}])

    def test_plain_call_unchanged(self):
        p = self._profile(code="oai.plain")
        chat, resp, text, _ = self._run(p)
        self.assertNotIn("response_format", chat)
        self.assertNotIn("reasoning_effort", chat)
        self.assertEqual(resp, {})
        self.assertEqual(text, '{"content":"chat"}')

    def test_maps_grounding_rejected_for_openai(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self._profile(code="oai.bad", maps_grounding=True).full_clean()
