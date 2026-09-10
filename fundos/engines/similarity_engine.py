"""
[DET] Peer similarity engine (Doc 1 M2-§2 / Doc 5 Annexure A4).

Pure — no Django imports. Weighted, EXPLAINABLE score (BR-S2-010): the
breakdown records each dimension's contribution.
"""
from dataclasses import dataclass, field


@dataclass
class SimilarityResult:
    score: float                       # 0–100
    breakdown: dict = field(default_factory=dict)
    rationale: str = ""


def _txt(v):
    return str(v or "").strip().lower()


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ratio_closeness(a, b):
    """1.0 when equal, decaying with the ratio (order-of-magnitude aware)."""
    a, b = _num(a), _num(b)
    if not a or not b or a <= 0 or b <= 0:
        return 0.0
    ratio = min(a, b) / max(a, b)
    return ratio


def compute_similarity(company: dict, peer: dict, weights: dict,
                       sector_adjacency: dict | None = None) -> SimilarityResult:
    """`company`/`peer` dicts carry: sector, subsector, business_model,
    revenue, customer_profile, growth_rate, geography, funding_stage,
    team_size, founder_background, technology."""
    sector_adjacency = sector_adjacency or {}
    components = {}

    # Sector & sub-sector (exact 1.0 / adjacent 0.6 / same sector 0.8)
    cs, ps = _txt(company.get("sector")), _txt(peer.get("sector"))
    css, pss = _txt(company.get("subsector")), _txt(peer.get("subsector"))
    if cs and cs == ps:
        components["sector_subsector"] = 1.0 if (css and css == pss) else 0.8
    elif ps and ps in [_txt(x) for x in sector_adjacency.get(cs, [])]:
        components["sector_subsector"] = 0.6
    else:
        components["sector_subsector"] = 0.0

    components["business_model"] = 1.0 if _txt(company.get("business_model")) and \
        _txt(company.get("business_model")) == _txt(peer.get("business_model")) else 0.0

    components["revenue_stage"] = _ratio_closeness(
        company.get("revenue"), peer.get("revenue"))

    components["customer_profile"] = 1.0 if _txt(company.get("customer_profile")) and \
        _txt(company.get("customer_profile")) == _txt(peer.get("customer_profile")) else 0.0

    cg, pg = _num(company.get("growth_rate")), _num(peer.get("growth_rate"))
    if cg is not None and pg is not None:
        components["growth_rate"] = max(0.0, 1.0 - abs(cg - pg) / max(abs(cg), abs(pg), 50))
    else:
        components["growth_rate"] = 0.0

    components["geography"] = 1.0 if _txt(company.get("geography")) and \
        _txt(company.get("geography")) == _txt(peer.get("geography")) else 0.0

    components["funding_stage"] = 1.0 if _txt(company.get("funding_stage")) and \
        _txt(company.get("funding_stage")) == _txt(peer.get("funding_stage")) else 0.0

    components["team_size"] = _ratio_closeness(
        company.get("team_size"), peer.get("team_size"))

    components["founder_background"] = 1.0 if _txt(company.get("founder_background")) and \
        _txt(company.get("founder_background")) == _txt(peer.get("founder_background")) else 0.5

    components["technology_market"] = 1.0 if _txt(company.get("technology")) and \
        _txt(company.get("technology")) == _txt(peer.get("technology")) else 0.4

    total_weight = sum(weights.values()) or 1
    score = 0.0
    breakdown = {}
    for key, weight in weights.items():
        contribution = components.get(key, 0.0) * weight
        score += contribution
        breakdown[key] = {"weight": weight,
                          "match": round(components.get(key, 0.0), 3),
                          "contribution": round(contribution * 100 / total_weight, 2)}
    score = round(score * 100 / total_weight, 2)

    top = sorted(breakdown.items(), key=lambda kv: -kv[1]["contribution"])[:3]
    rationale = "Closest on " + ", ".join(k.replace("_", " ") for k, _ in top) + "."
    return SimilarityResult(score=score, breakdown=breakdown, rationale=rationale)
