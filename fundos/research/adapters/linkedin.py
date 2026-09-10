"""LinkedIn adapter — GATED for licensing/DPDP/ToS (BR-M1-003): disabled by
the research_source_flags row (legal_cleared=False) until cleared. The
scan method is intentionally NOT implemented until legal sign-off; when
cleared this adapter should call the licensed data provider, never scrape."""
from fundos.research.adapters.base import BaseResearchAdapter


class LinkedInAdapter(BaseResearchAdapter):
    source_type = "linkedin"

    def fetch(self):
        raise RuntimeError(
            "LinkedIn source is legally gated (BR-M1-003) — enable via "
            "research_source_flags only after DPDP/ToS clearance.")
