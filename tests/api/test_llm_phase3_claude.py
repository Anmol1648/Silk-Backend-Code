from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from django.test import TestCase


class _Block(SimpleNamespace):
    pass


def _make_anthropic_response(blocks):
    resp = MagicMock()
    resp.content = blocks
    resp.usage = SimpleNamespace(input_tokens=10, output_tokens=20)
    return resp


class ClaudeCapabilityTranslation(TestCase):
    """Phase 3: capability payload → real Anthropic params."""

    def setUp(self):
        # Restored afterwards — see set_env: a key left in the environment
        # changes what every later test in the process sees.
        from tests.conftest_helpers import set_env
        set_env(self, ANTHROPIC_API_KEY="test")
        from fundos.llm.models import LLMEndpoint
        self.ep = LLMEndpoint.objects.create(
            code="ANTHROPIC", provider_kind="anthropic",
            base_url="https://api.anthropic.com",
            default_model="claude-sonnet-4-6",
            api_key_env_var="ANTHROPIC_API_KEY")

    def _profile(self, **kw):
        from fundos.llm.models import LLMConfigProfile
        base = dict(code="cp.claude", provider="anthropic",
                    model_string="claude-sonnet-4-6", endpoint=self.ep)
        base.update(kw)
        p = LLMConfigProfile(**base); p.full_clean(); p.save()
        return p

    def _run(self, profile, blocks):
        from fundos.llm import adapter
        caps = profile.capabilities_payload()
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _make_anthropic_response(blocks)

        class FakeClient:
            def __init__(self, *a, **k): self.messages = FakeMessages()

        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic = FakeClient
        fake_anthropic.APITimeoutError = Exception
        fake_anthropic.APIStatusError = Exception
        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            text, usage = adapter._run_anthropic(
                self.ep, "sys", "prompt", "claude-sonnet-4-6", None, 2048, 60,
                capabilities=caps)
        return captured, text, usage

    def test_web_search_tool_with_subcontrols(self):
        p = self._profile(tier="advanced", web_search=True, max_uses=5,
                          allowed_domains=["crunchbase.com"],
                          user_location={"country": "IN"})
        cap, text, usage = self._run(p, [_Block(type="text", text="hi")])
        tools = cap.get("tools", [])
        ws = [t for t in tools if t.get("type") == "web_search_20250305"]
        self.assertTrue(ws, "web_search tool must be present")
        self.assertEqual(ws[0]["max_uses"], 5)
        self.assertEqual(ws[0]["allowed_domains"], ["crunchbase.com"])
        self.assertEqual(ws[0]["user_location"], {"country": "IN"})

    def test_web_fetch_tool(self):
        p = self._profile(code="c.fetch", web_fetch=True)
        cap, _, _ = self._run(p, [_Block(type="text", text="hi")])
        self.assertTrue(any(t.get("type") == "web_fetch_20250910"
                            for t in cap.get("tools", [])))

    def test_thinking_budget(self):
        p = self._profile(code="c.think", thinking_mode="tokens",
                          thinking_budget="4000")
        cap, _, _ = self._run(p, [_Block(type="text", text="hi")])
        self.assertEqual(cap["thinking"],
                         {"type": "enabled", "budget_tokens": 4000})
        # max_tokens bumped above budget
        self.assertGreater(cap["max_tokens"], 4000)

    def test_dynamic_filter_requires_code_exec_tool(self):
        p = self._profile(code="c.df", web_search=True, dynamic_filter=True,
                          code_execution=True)
        cap, _, _ = self._run(p, [_Block(type="text", text="hi")])
        self.assertTrue(any(t.get("type") == "code_execution_20250522"
                            for t in cap.get("tools", [])))

    def test_structured_output_forced_tool(self):
        schema = {"type": "object",
                  "properties": {"content": {"type": "string"}}}
        p = self._profile(code="c.so", structured_output=True,
                          output_schema=schema)
        # model returns the structured tool_use input
        blocks = [_Block(type="tool_use", name="emit_structured_output",
                         input={"content": "structured!"})]
        cap, text, _ = self._run(p, blocks)
        forced = [t for t in cap.get("tools", [])
                  if t.get("name") == "emit_structured_output"]
        self.assertTrue(forced)
        self.assertEqual(cap["tool_choice"],
                         {"type": "tool", "name": "emit_structured_output"})
        # returned text is the JSON of the tool input
        import json
        self.assertEqual(json.loads(text), {"content": "structured!"})

    def test_search_sources_extracted(self):
        p = self._profile(code="c.src", web_search=True)
        blocks = [
            _Block(type="web_search_tool_result",
                   content=[{"url": "https://x.com", "title": "X"}]),
            _Block(type="text", text="answer"),
        ]
        cap, text, usage = self._run(p, blocks)
        self.assertEqual(text, "answer")
        self.assertEqual(usage["grounding_sources"],
                         [{"uri": "https://x.com", "title": "X"}])

    def test_plain_call_unchanged(self):
        p = self._profile(code="c.plain")
        cap, text, _ = self._run(p, [_Block(type="text", text="plain")])
        self.assertNotIn("tools", cap)
        self.assertNotIn("thinking", cap)
        self.assertEqual(text, "plain")
