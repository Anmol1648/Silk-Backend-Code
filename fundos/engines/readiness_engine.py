"""
[DET] Readiness engine (Doc 1 M1-§4 / Doc 5 Annexure A1).

Pure f(inputs, cfg) → dataclass. NO Django imports (Doc 8 §1/§5).
The numeric engine computes sub-scores; the surface is dimensional status;
the LLM only EXPLAINS these numbers.
"""
from dataclasses import dataclass, field


@dataclass
class DimensionResult:
    dimension: str
    status: str                 # ready | needs_attention | missing | not_applicable
    sub_score: float
    evidence: list = field(default_factory=list)
    gap: str = ""


@dataclass
class ReadinessResult:
    overall_score: float
    overall_band: str
    dimensions: list            # [DimensionResult]
    strengths: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)


def _band_points(value, band_map):
    """Look up points from a [{lt, pts}] band list (lt None = open-ended)."""
    if value is None:
        return 0
    for band in band_map:
        if band["lt"] is None or value < band["lt"]:
            return band["pts"]
    return 0


def compute_readiness(ckb: dict, materials_count: int, cfg: dict,
                      current_year: int = 2026) -> ReadinessResult:
    """`ckb` is a flat {field_key: value} dict of present values (None = absent).

    Score components (max 100): revenue 30 + growth 20 + age 10 + runway 10 +
    narrative completeness 30 (Plan-1 §11.1 shape, config-driven).
    """
    r = cfg["readiness"]

    revenue = _num(ckb.get("arr")) or _num(ckb.get("revenue"))
    growth = _num(ckb.get("growth_rate"))
    founding_year = _num(ckb.get("founding_year"))
    age = (current_year - founding_year) if founding_year else None
    runway = _num(ckb.get("runway_months"))

    dim_map = r["dimensionMap"]
    all_keys = [k for keys in dim_map.values() for k in keys]
    present = sum(1 for k in all_keys if ckb.get(k) not in (None, "", []))
    narrative_share = present / len(all_keys) if all_keys else 0

    score = (
        _band_points(revenue, r["revMap"])
        + _band_points(growth, r["grMap"])
        + _band_points(age, r["ageMap"])
        + _band_points(runway, r["raiMap"])
        + _band_points(narrative_share, r["narBands"])
    )
    score = min(round(score, 2), 100.0)

    band = "Not yet ready"
    for b in r["bands"]:
        if score >= b["gte"]:
            band = b["band"]
            break

    dimensions, strengths, risks, missing, recommendations = [], [], [], [], []
    ready_t = r.get("dimension_ready_threshold", 0.75)
    attn_t = r.get("dimension_attention_threshold", 0.35)

    for dim, keys in dim_map.items():
        have = [k for k in keys if ckb.get(k) not in (None, "", [])]
        share = len(have) / len(keys) if keys else 0
        absent = [k for k in keys if k not in have]
        if share >= ready_t:
            status, gap = "ready", ""
            strengths.append(f"{dim}: well documented ({', '.join(have)}).")
        elif share >= attn_t:
            status = "needs_attention"
            gap = f"Missing: {', '.join(absent)}"
            recommendations.append({
                "text": f"Complete the {dim.lower()} fields: {', '.join(absent)}.",
                "impact": "med", "dimension": dim})
        else:
            status = "missing"
            gap = f"Missing: {', '.join(absent)}"
            missing.extend(f"{dim}.{k}" for k in absent)
            recommendations.append({
                "text": f"Provide the {dim.lower()} information "
                        f"({', '.join(absent)}).",
                "impact": "high", "dimension": dim})
        dimensions.append(DimensionResult(
            dimension=dim, status=status, sub_score=round(share * 100, 2),
            evidence=have, gap=gap))

    if runway is not None and runway < 6:
        risks.append("Runway under 6 months — fundraise timing is critical.")
    if materials_count == 0:
        recommendations.append({
            "text": "Upload existing investor materials so they can be "
                    "analysed before regeneration is recommended.",
            "impact": "med", "dimension": "Data"})

    return ReadinessResult(overall_score=score, overall_band=band,
                           dimensions=dimensions, strengths=strengths,
                           risks=risks, missing=missing,
                           recommendations=recommendations)


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
