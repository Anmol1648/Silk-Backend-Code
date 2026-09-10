import json
from unittest.mock import patch
from django.test import TestCase
from tests.conftest_helpers import auth_headers, make_world


class LLMNormalizer(TestCase):
    """Prove the adapter recovers content when a live provider returns valid
    JSON in a DIFFERENT shape than the code expects (LLM_ISSUE2)."""

    def setUp(self):
        from django.core.management import call_command
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        # LIVE mode (not mocked) so the endpoint dispatch path runs.
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo(); cfg.ai_mocked = False; cfg.save()
        self._bind_live_endpoint()
        self.w = make_world()
        self.company = self.w["a"]["company"]
        self.co = str(self.company.id)
        self.h = auth_headers(self.w["a"]["founder"])

    def _bind_live_endpoint(self):
        from fundos.llm.models import LLMEndpoint, LLMRoleBinding
        # seed_platform_config now seeds inactive GEMINI/OPENAI endpoints
        # alongside ANTHROPIC so an admin can switch provider without
        # hand-creating rows. get_or_create rather than create, so this
        # helper binds whichever row exists instead of colliding on `code`.
        ep, _ = LLMEndpoint.objects.get_or_create(
            code="OPENAI",
            defaults=dict(provider_kind="openai_chat",
                          base_url="https://api.openai.com/v1",
                          default_model="gpt-4o",
                          api_key_env_var="OPENAI_API_KEY",
                          is_active=True, priority=1))
        if not ep.is_active:
            ep.is_active = True
            ep.save(update_fields=["is_active"])
        for b in LLMRoleBinding.objects.all():
            b.primary_endpoint = ep; b.fallback_endpoint = None
            b.is_mocked = False; b.save()

    def _fake_dispatch(self, drift):
        """Return a dispatcher that emits off-shape JSON keyed by role's
        section, ignoring the real network."""
        def dispatch(endpoint, system, prompt, binding, model_override=None,
                     **kwargs):
            t = prompt  # section_label is substituted verbatim into the prompt
            if "Products & Services" in t:
                body = {"products": [{"name": "Widget", "category": "SaaS",
                                      "description": "A thing"}]}
            elif "Competitors" in t:
                body = {"competition": [{"name": "RivalCo"}]}
            elif "Founders" in t:
                body = {"records": [{"name": "Jane Doe", "designation": "CEO"}]}
            elif "Key People" in t:
                body = {"key_people": [{"name": "KP One",
                                        "designation": "CTO"}]}
            else:
                body = {"text": "This is investor-ready prose about the company "
                                "that the model returned under the wrong key."}
            return json.dumps(body), {"prompt_tokens": 10, "completion_tokens": 20}
        return dispatch

    def test_offshape_response_still_populates(self):
        """The normalizer recovers a list returned under an aliased key.

        Driven through per-SECTION regeneration rather than a full profile
        build. The fake dispatcher keys off a section label appearing in the
        prompt, which only the per-section roles put there; a full build now
        goes through the Company Master Data Pipeline, whose two prompts carry
        no section label, so every branch would fall through to the generic
        one and the test would pass or fail for reasons unrelated to its
        subject. Section regeneration is the surviving caller of these roles
        and is where the normalizer still matters.
        """
        import os
        os.environ["OPENAI_API_KEY"] = "sk-test-key"
        from fundos.profile.services import (get_or_create_profile,
                                             regenerate_section)
        profile = get_or_create_profile(self.company,
                                        user=self.w["a"]["founder"])
        sources = ({"website": {"text": "Acme Corp builds widgets for the "
                                        "Indian retail market." * 40}}, [])
        with patch("fundos.profile.services.collect_sources",
                   return_value=sources), \
             patch("fundos.llm.adapter._dispatch",
                   side_effect=self._fake_dispatch("drift")):
            for section in ("products_services", "competitors"):
                regenerate_section(profile, section,
                                   user=self.w["a"]["founder"], force=True)

        secs = self.client.get(f"/api/v1/companies/{self.co}/profile",
                               **self.h).json()["sections"]
        # products_services (company_profile_structured role): list recovered
        # from the "products" alias instead of "items".
        self.assertTrue(len(secs["products_services"]["data"]) >= 1,
                        "products_services must recover from aliased key")
        self.assertEqual(secs["products_services"]["data"][0]["name"], "Widget")
        # competitors (company_profile_records role): recovered from the
        # "competition" alias instead of "records".
        self.assertTrue(len(secs["competitors"]["data"]) >= 1,
                        "competitors must recover from aliased key")
        self.assertEqual(secs["competitors"]["data"][0]["name"], "RivalCo")

    def test_narrative_prose_under_wrong_key_recovers(self):
        # A narrative section whose model returns prose under "text" (not
        # "content") must still save that prose, not empty.
        import os
        os.environ["OPENAI_API_KEY"] = "sk-test-key"
        from fundos.llm.adapter import llm_generate
        with patch("fundos.llm.adapter._dispatch",
                   side_effect=self._fake_dispatch("drift")):
            out = llm_generate(
                role="company_profile_section",
                context={"section_key": "key_people"},
                section_context={"section_key": "key_people",
                                 "section_label": "Some Narrative"})
        self.assertTrue(out.get("content"),
                        "prose returned under 'text' must be recovered to content")
