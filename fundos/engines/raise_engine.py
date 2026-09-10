"""
[DET] Ideal fund-raise engine (Doc 1 M2-§3 / Doc 5 Annexure A2).

Principle: recommend the SMALLEST amount to reach the next value-inflection
milestone with acceptable runway and ownership — never maximise. Pure — no
Django imports. Dilution is always a RANGE ([AI-GUARD]).
"""
from dataclasses import dataclass, field


@dataclass
class RaiseScenarioResult:
    label: str
    raise_usd: float
    dilution_pct: float
    runway_months: int
    assessment: str


@dataclass
class RaiseResult:
    recommended_usd: float
    low_usd: float
    high_usd: float
    confidence: str                       # high | medium | low
    dilution_low_pct: float
    dilution_high_pct: float
    recommended_timeline: str
    funding_band: str
    dimension_scores: dict = field(default_factory=dict)
    reasoning: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    scenarios: list = field(default_factory=list)   # ≥3 (BR guarantee)


def _num(v, default=None):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def compute_raise(ckb: dict, objectives: dict, peer_stats: dict,
                  cfg: dict, usd_inr: float = 84.0) -> RaiseResult:
    """`ckb` flat field dict (INR monetary values), `objectives` from Stage-2
    step 1, `peer_stats` e.g. {"median_raise_usd": ...} from the peer
    universe (may be empty until the market feed is ratified)."""
    r = cfg["raise"]

    burn_inr = _num(ckb.get("burn"), 0) or 0          # monthly
    runway_choice = (objectives.get("runway") or "18m")
    runway_target = r["runwayMult"].get(runway_choice, 18)

    burn_usd = burn_inr / usd_inr if burn_inr else 0
    base_need = burn_usd * runway_target
    buffer = r["plannedAmountMap"]["buffer_pct"] / 100.0
    recommended = base_need * (1 + buffer)

    # Peer anchor: pull toward the peer median without maximising.
    median_peer = _num(peer_stats.get("median_raise_usd"))
    if median_peer and recommended:
        recommended = min(max(recommended, 0.6 * median_peer),
                          1.2 * median_peer) if recommended > 0 else median_peer
    elif median_peer and not recommended:
        recommended = median_peer
    recommended = round(recommended or 500_000, -3)

    spread = r["plannedAmountMap"]["range_spread_pct"] / 100.0
    low = round(recommended * (1 - spread), -3)
    high = round(recommended * (1 + spread), -3)

    # Funding band + typical dilution range.
    band_label, dil_low, dil_high = "growth", 10, 20
    for band in r["bands"]:
        if band["high"] is None or recommended <= band["high"]:
            band_label = band["label"]
            dil_low, dil_high = band["dil"]
            break
    max_dil = _num(objectives.get("maxDilutionPct"))
    if max_dil:
        dil_high = min(dil_high, max_dil)
        dil_low = min(dil_low, dil_high)

    # Six independent dimensions (0–100), config-weighted.
    dims = {
        "business_maturity": _maturity_score(ckb),
        "commercial_traction": _traction_score(ckb),
        "capital_efficiency": _efficiency_score(ckb),
        "founder_strength": 60.0 if ckb.get("founders") else 30.0,
        "market_opportunity": 65.0 if ckb.get("sector") else 40.0,
        "investor_appetite": _num(peer_stats.get("appetite_score"), 50.0),
    }
    weights = r["dimensionWeights"]
    weighted = sum(dims[k] * weights.get(k, 0) for k in dims) / \
        (sum(weights.values()) or 1)
    confidence = "high" if weighted >= 65 else "medium" if weighted >= 45 else "low"

    reasoning = [
        f"Target runway {runway_target} months at current burn implies "
        f"~${base_need:,.0f} before buffer.",
        f"A {int(buffer * 100)}% execution buffer is applied "
        "(raise enough, not as much as possible).",
    ]
    if median_peer:
        reasoning.append(
            f"Group-A peers raised a median of ~${median_peer:,.0f} at this stage.")
    risks = []
    if burn_usd == 0:
        risks.append("Burn not captured in the CKB — the amount is a band-level "
                     "estimate; capture Financials to tighten it.")
        confidence = "low"

    timeline = objectives.get("timeline") or "6m"

    scenarios = [
        RaiseScenarioResult(
            label="Conservative", raise_usd=low,
            dilution_pct=round(dil_low, 1),
            runway_months=int(runway_target * (1 - spread)),
            assessment="Smaller round, less dilution, shorter runway — "
                       "suits strong near-term milestones."),
        RaiseScenarioResult(
            label="Recommended", raise_usd=recommended,
            dilution_pct=round((dil_low + dil_high) / 2, 1),
            runway_months=runway_target,
            assessment="Reaches the next value-inflection milestone with "
                       "buffer at acceptable ownership cost."),
        RaiseScenarioResult(
            label="Aggressive", raise_usd=high,
            dilution_pct=round(dil_high, 1),
            runway_months=int(runway_target * (1 + spread)),
            assessment="Longer runway and optionality at higher dilution — "
                       "requires stronger investor appetite."),
    ]

    return RaiseResult(
        recommended_usd=recommended, low_usd=low, high_usd=high,
        confidence=confidence, dilution_low_pct=round(dil_low, 1),
        dilution_high_pct=round(dil_high, 1), recommended_timeline=timeline,
        funding_band=band_label, dimension_scores={k: round(v, 1) for k, v in dims.items()},
        reasoning=reasoning, risks=risks, scenarios=scenarios)


def _maturity_score(ckb):
    score = 30.0
    if _num(ckb.get("arr"), 0) > 0:
        score += 25
    if ckb.get("incorporation"):
        score += 15
    if _num(ckb.get("employees"), 0) >= 10:
        score += 15
    return min(score, 100.0)


def _traction_score(ckb):
    score = 20.0
    if _num(ckb.get("customers"), 0) > 0:
        score += 20
    if _num(ckb.get("growth_rate"), 0) >= 50:
        score += 30
    elif _num(ckb.get("growth_rate"), 0) >= 20:
        score += 15
    if _num(ckb.get("churn"), 100) <= 3:
        score += 15
    return min(score, 100.0)


def _efficiency_score(ckb):
    score = 40.0
    ltv, cac = _num(ckb.get("ltv")), _num(ckb.get("cac"))
    if ltv and cac and cac > 0:
        ratio = ltv / cac
        score += 30 if ratio >= 3 else 15 if ratio >= 1.5 else 0
    gm = _num(ckb.get("gross_margin"))
    if gm and gm >= 60:
        score += 20
    return min(score, 100.0)
