"""
[DET] Valuation engine (Doc 1 M2-§4 / Doc 5 Annexure).

Never a single number — an indicative RANGE with methodologies, weights,
visible outliers (BR-S2-033) and a football field. Pure — no Django imports.
"""
from dataclasses import dataclass, field
from statistics import median


@dataclass
class MethodResult:
    method: str
    low: float
    mid: float
    high: float
    weight: float
    applicable: bool = True
    is_outlier: bool = False
    outlier_reason: str = ""
    assumptions: list = field(default_factory=list)


@dataclass
class ValuationResult:
    range_low: float
    range_high: float
    confidence: str
    football_field: list          # [MethodResult]
    assumptions: list = field(default_factory=list)
    limitations: list = field(default_factory=list)


def _num(v, default=None):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def compute_valuation(ckb: dict, peer_stats: dict, cfg: dict,
                      usd_inr: float = 84.0) -> ValuationResult:
    vm = cfg["valuation_methods"]
    enabled = set(vm["enabled"])
    weights = vm["weights"]
    multiples = dict(vm.get("default_multiples", {}))
    # Peer-derived multiples override defaults when available (GC-20 feed).
    for key in ("ev_arr", "ev_revenue", "ev_ebitda"):
        if peer_stats.get(f"{key}_multiples"):
            multiples[key] = peer_stats[f"{key}_multiples"]

    arr_usd = (_num(ckb.get("arr"), 0) or 0) / usd_inr
    revenue_usd = (_num(ckb.get("revenue"), 0) or 0) / usd_inr or arr_usd
    ebitda_usd = (_num(ckb.get("ebitda"), 0) or 0) / usd_inr

    methods: list[MethodResult] = []

    def add_multiple_method(name, base_usd, key):
        m = multiples.get(key, {})
        applicable = bool(base_usd and base_usd > 0 and m)
        if applicable:
            methods.append(MethodResult(
                method=name, low=base_usd * m["low"], mid=base_usd * m["mid"],
                high=base_usd * m["high"], weight=weights.get(key, 0),
                assumptions=[f"{key} multiples "
                             f"{m['low']}–{m['high']}x (mid {m['mid']}x)"]))
        else:
            methods.append(MethodResult(
                method=name, low=0, mid=0, high=0, weight=0, applicable=False,
                assumptions=[f"insufficient data for {key}"]))

    if "ev_arr" in enabled:
        add_multiple_method("EV/ARR", arr_usd, "ev_arr")
    if "ev_revenue" in enabled:
        add_multiple_method("EV/Revenue", revenue_usd, "ev_revenue")
    if "ev_ebitda" in enabled:
        add_multiple_method("EV/EBITDA", ebitda_usd if ebitda_usd > 0 else 0,
                            "ev_ebitda")

    # Trading / transaction comparables from the peer universe stats.
    for key, name in (("trading", "Trading comparables"),
                      ("transaction", "Transaction comparables")):
        if key in enabled:
            stats = peer_stats.get(f"{key}_valuations_usd") or []
            if len(stats) >= 3:
                stats = sorted(stats)
                methods.append(MethodResult(
                    method=name, low=stats[0], mid=median(stats),
                    high=stats[-1], weight=weights.get(key, 0),
                    assumptions=[f"{len(stats)} peer data points"]))
            else:
                methods.append(MethodResult(
                    method=name, low=0, mid=0, high=0, weight=0,
                    applicable=False,
                    assumptions=["fewer than 3 peer data points"]))

    # Venture-stage benchmark from the raise band (post-money anchored).
    if "venture" in enabled:
        anchor = _num(peer_stats.get("stage_postmoney_median_usd"))
        if anchor:
            methods.append(MethodResult(
                method="Venture benchmarks", low=anchor * 0.7, mid=anchor,
                high=anchor * 1.3, weight=weights.get("venture", 0),
                assumptions=["stage post-money median from the market feed"]))
        else:
            methods.append(MethodResult(
                method="Venture benchmarks", low=0, mid=0, high=0, weight=0,
                applicable=False, assumptions=["market feed not yet available"]))

    # Scorecard / Berkus — early-stage qualitative anchors.
    if "berkus" in enabled:
        per_factor = vm.get("berkus_max_per_factor_usd", 500_000)
        factors = [bool(ckb.get("solution")), bool(ckb.get("founders")),
                   bool(ckb.get("cap_table")), _num(ckb.get("customers"), 0) > 0,
                   _num(ckb.get("arr"), 0) > 0]
        value = sum(per_factor for f in factors if f)
        methods.append(MethodResult(
            method="Berkus", low=value * 0.6, mid=value, high=value * 1.2,
            weight=weights.get("berkus", 0),
            applicable=value > 0,
            assumptions=[f"{sum(factors)}/5 factors present at "
                         f"${per_factor:,.0f} each"]))

    if "scorecard" in enabled:
        anchor = _num(peer_stats.get("stage_postmoney_median_usd"))
        if anchor:
            methods.append(MethodResult(
                method="Scorecard", low=anchor * 0.75, mid=anchor * 0.95,
                high=anchor * 1.15, weight=weights.get("scorecard", 0),
                assumptions=["scorecard adjustments vs stage median"]))

    if "dcf" in enabled:
        if revenue_usd > 0 and _num(ckb.get("growth_rate")):
            growth = min(_num(ckb.get("growth_rate")), 200) / 100.0
            terminal = revenue_usd * ((1 + growth) ** 5) * 3  # 3x terminal rev
            pv = terminal / (1.35 ** 5)                       # 35% discount
            methods.append(MethodResult(
                method="DCF", low=pv * 0.6, mid=pv, high=pv * 1.4,
                weight=weights.get("dcf", 0),
                assumptions=["5y horizon, 35% discount rate, 3x terminal "
                             "revenue multiple"]))
        else:
            methods.append(MethodResult(
                method="DCF", low=0, mid=0, high=0, weight=0, applicable=False,
                assumptions=["revenue/growth insufficient for DCF"]))

    applicable = [m for m in methods if m.applicable and m.mid > 0]

    # Outlier removal — visible with a reason (BR-S2-033), IQR policy.
    if len(applicable) >= 4:
        mids = sorted(m.mid for m in applicable)
        q1 = mids[len(mids) // 4]
        q3 = mids[(3 * len(mids)) // 4]
        iqr = (q3 - q1) or 1
        multiplier = vm["outlierPolicy"]["iqr_multiplier"]
        lo_bound, hi_bound = q1 - multiplier * iqr, q3 + multiplier * iqr
        for m in applicable:
            if m.mid < lo_bound or m.mid > hi_bound:
                m.is_outlier = True
                m.outlier_reason = (f"mid ${m.mid:,.0f} outside IQR "
                                    f"[{lo_bound:,.0f}, {hi_bound:,.0f}]")

    used = [m for m in applicable if not m.is_outlier and m.weight > 0]
    total_weight = sum(m.weight for m in used) or 1
    if used:
        range_low = sum(m.low * m.weight for m in used) / total_weight
        range_high = sum(m.high * m.weight for m in used) / total_weight
    else:
        range_low = range_high = 0.0

    confidence = ("high" if len(used) >= 4 else
                  "medium" if len(used) >= 2 else "low")

    return ValuationResult(
        range_low=round(range_low, -3), range_high=round(range_high, -3),
        confidence=confidence, football_field=methods,
        assumptions=[a for m in used for a in m.assumptions],
        limitations=(["Fewer than 3 applicable methods — treat the range as "
                      "directional only."] if len(used) < 3 else [])
        + ["Indicative only; FEMA may require an INR valuation by a "
           "registered valuer."],
    )
