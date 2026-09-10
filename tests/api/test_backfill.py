from django.test import TestCase
from tests.conftest_helpers import make_world

class Backfill(TestCase):
    def setUp(self):
        from django.core.management import call_command
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo(); cfg.ai_mocked = True; cfg.save()
        self.w = make_world()

    def test_backfill_runs(self):
        from django.core.management import call_command
        from fundos.profile.services import get_or_create_profile
        from fundos.profile.models import ProfileSection
        p = get_or_create_profile(self.w["a"]["company"], user=self.w["a"]["founder"])
        call_command("backfill_profile_sections", company=str(p.company_id), verbosity=0)
        s = ProfileSection.objects.get(profile=p, section_key="products_services", is_active=True)
        self.assertTrue((s.structured or {}).get("items"), "backfill should populate structured items")
