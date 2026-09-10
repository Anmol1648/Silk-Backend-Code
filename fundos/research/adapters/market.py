"""Market / competitor / industry / public-funding adapters — read from the
internal market feed (market_deal ingestion, Doc 6 §12). Until the feed is
ratified (Doc 7 critical path) they run on seeded fixtures."""
from fundos.research.adapters.base import BaseResearchAdapter


class MarketAdapter(BaseResearchAdapter):
    source_type = "market"
    is_fixture = True          # pending feed ratification

    def fetch(self):
        sector = ((self.ckb.get("business") or {}).get("sector") or {}).get("value")
        return {"facts": {"market_note": f"Market context for sector "
                                          f"'{sector or 'unknown'}' pending "
                                          "feed ratification."},
                "raw": {}, "confidence": 0.3}


class CompetitorAdapter(BaseResearchAdapter):
    source_type = "competitor"
    is_fixture = True          # pending feed ratification

    def fetch(self):
        return {"facts": {"competitors": []}, "raw": {}, "confidence": 0.3}


class IndustryAdapter(BaseResearchAdapter):
    source_type = "industry"
    is_fixture = True          # pending feed ratification

    def fetch(self):
        return {"facts": {"industry_trends": []}, "raw": {}, "confidence": 0.3}


class PublicFundingAdapter(BaseResearchAdapter):
    source_type = "public_funding"
    is_fixture = True          # pending feed ratification

    def fetch(self):
        return {"facts": {"funding_history": []}, "raw": {}, "confidence": 0.4}
