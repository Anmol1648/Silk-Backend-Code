"""Extension adapters (feature-flagged: enable_research_extensions) —
patents, news, awards, hiring/digital footprint."""
from fundos.research.adapters.base import BaseResearchAdapter


class PatentsAdapter(BaseResearchAdapter):
    source_type = "patents"
    is_fixture = True          # pending feed ratification

    def fetch(self):
        return {"facts": {"patents": []}, "raw": {}, "confidence": 0.3}


class NewsAdapter(BaseResearchAdapter):
    source_type = "news"
    is_fixture = True          # pending feed ratification

    def fetch(self):
        return {"facts": {"news": []}, "raw": {}, "confidence": 0.3}


class AwardsAdapter(BaseResearchAdapter):
    source_type = "awards"
    is_fixture = True          # pending feed ratification

    def fetch(self):
        return {"facts": {"awards": []}, "raw": {}, "confidence": 0.3}


class HiringAdapter(BaseResearchAdapter):
    source_type = "hiring"
    is_fixture = True          # pending feed ratification

    def fetch(self):
        return {"facts": {"open_roles": []}, "raw": {}, "confidence": 0.3}
