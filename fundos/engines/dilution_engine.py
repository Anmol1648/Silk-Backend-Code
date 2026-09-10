"""
[DET] Dilution simulator (Doc 1 M2-§5). Pure — no Django imports.

Computes ownership outcomes as RANGES, never single points (Enhanced LLD
v4.0 §11). SAFE/Note scenarios model conversion at the range boundaries and
surface the resulting dilution band (BR-M2-051).
"""
from dataclasses import dataclass, field


@dataclass
class DilutionResult:
    instrument: str
    raise_usd: float
    valuation_low_usd: float
    valuation_high_usd: float
    pre_money: dict = field(default_factory=dict)
    post_money: dict = field(default_factory=dict)
    ownership_bands: dict = field(default_factory=dict)
    conversion_assumptions: dict = field(default_factory=dict)


def simulate_dilution(raise_usd: float, valuation_low_usd: float,
                      valuation_high_usd: float, instrument: str,
                      cap_table: list | None = None,
                      cfg: dict | None = None) -> DilutionResult:
    """cap_table: [{holder, pct, kind: founder|investor|esop}] — from CKB.
    Valuations are PRE-money. Ownership bands: {group: [lo_pct, hi_pct]}."""
    cfg = cfg or {}
    cap_table = cap_table or [{"holder": "Founders", "pct": 100.0,
                               "kind": "founder"}]
    conv = {}

    def shares(kind):
        return sum(h.get("pct", 0) for h in cap_table
                   if h.get("kind") == kind)

    founder_pct = shares("founder") or 100.0
    existing_inv_pct = shares("investor")
    esop_pct = shares("esop")

    if instrument in ("safe", "convertible_note", "ccd"):
        # Conversion at range boundaries with the configured discount.
        discount = (cfg.get("dilution", {}) or {}).get(
            "safe_discount_default_pct", 20) / 100.0
        eff_low = valuation_low_usd * (1 - discount)
        eff_high = valuation_high_usd * (1 - discount)
        conv = {"discount_pct": discount * 100,
                "note": "Converts at the next priced round; boundaries model "
                        "conversion at the valuation range edges."}
    else:
        eff_low, eff_high = valuation_low_usd, valuation_high_usd

    def new_investor_pct(pre_money):
        post = pre_money + raise_usd
        return 100.0 * raise_usd / post if post > 0 else 0.0

    ni_high = new_investor_pct(eff_low)     # low valuation → high dilution
    ni_low = new_investor_pct(eff_high)

    def diluted(pct, ni):
        return round(pct * (1 - ni / 100.0), 2)

    ownership_bands = {
        "founder": sorted([diluted(founder_pct, ni_high),
                           diluted(founder_pct, ni_low)]),
        "newInvestor": sorted([round(ni_low, 2), round(ni_high, 2)]),
        "esop": sorted([diluted(esop_pct, ni_high), diluted(esop_pct, ni_low)]),
        "existing": sorted([diluted(existing_inv_pct, ni_high),
                            diluted(existing_inv_pct, ni_low)]),
    }

    return DilutionResult(
        instrument=instrument, raise_usd=raise_usd,
        valuation_low_usd=valuation_low_usd,
        valuation_high_usd=valuation_high_usd,
        pre_money={"founder": founder_pct, "existing": existing_inv_pct,
                   "esop": esop_pct},
        post_money={"atLowValuation": {"newInvestor": round(ni_high, 2)},
                    "atHighValuation": {"newInvestor": round(ni_low, 2)}},
        ownership_bands=ownership_bands,
        conversion_assumptions=conv,
    )


def dilution_matrix(raise_points: list, valuation_points: list,
                    cap_table: list | None = None,
                    esop_topup_pct: float = 0.0) -> list:
    """Valuation-vs-dilution grid (Doc 1 M2-§4 §7.9)."""
    grid = []
    cap_table = cap_table or [{"holder": "Founders", "pct": 100.0,
                               "kind": "founder"}]
    founder_pct = sum(h["pct"] for h in cap_table if h.get("kind") == "founder") or 100
    existing_pct = sum(h["pct"] for h in cap_table if h.get("kind") == "investor")
    for r in raise_points:
        for v in valuation_points:
            post = v + r
            dil = 100.0 * r / post if post else 0
            grid.append({
                "raise": r, "valuation": v,
                "dilutionPct": round(dil, 2),
                "founderOwnershipPct": round(founder_pct * (1 - dil / 100)
                                             * (1 - esop_topup_pct / 100), 2),
                "investorOwnershipPct": round(dil, 2),
                "esopImpactPct": round(esop_topup_pct, 2),
                "existingInvestorDilutionPct": round(
                    existing_pct * dil / 100, 2),
            })
    return grid


# ---------------------------------------------------------------------------
# [DET] Investor-category derivation (GC-03 / Doc 5 Annexure A3)
# ---------------------------------------------------------------------------

def derive_investor_categories(round_type: str, raise_usd: float,
                               category_map: list,
                               growth_rate: float | None = None) -> dict:
    """category_map rows: {round_type, band_low_usd, band_high_usd,
    primary[], secondary[], optional[]}. Growth/revenue act as tie-breakers."""
    matches = [row for row in category_map
               if str(row.get("round_type", "")).lower() == str(round_type or "").lower()
               and float(row.get("band_low_usd", 0)) <= raise_usd
               <= float(row.get("band_high_usd") or 1e15)]
    if not matches:
        matches = [row for row in category_map
                   if float(row.get("band_low_usd", 0)) <= raise_usd
                   <= float(row.get("band_high_usd") or 1e15)]
    if not matches:
        return {"primary": ["Angels", "Micro VCs"],
                "secondary": ["Accelerators"], "optional": ["Family Offices"]}
    row = matches[0]
    result = {"primary": list(row.get("primary", [])),
              "secondary": list(row.get("secondary", [])),
              "optional": list(row.get("optional", []))}
    # Tie-breaker: exceptional growth promotes Growth funds to secondary.
    if growth_rate and growth_rate >= 150 and "Growth" not in result["primary"]:
        if "Growth" in result["optional"]:
            result["optional"].remove("Growth")
        if "Growth" not in result["secondary"]:
            result["secondary"].append("Growth")
    return result


# ---------------------------------------------------------------------------
# [DET] Cross-document numeric validation (Doc 1 M3-§8 FS-S3-061)
# ---------------------------------------------------------------------------

def crossdoc_inconsistencies(values_by_document: dict, tolerances: dict) -> list:
    """values_by_document: {field_key: [{document, value}]}. Returns
    inconsistency rows where values differ beyond the field tolerance."""
    findings = []
    for field_key, entries in values_by_document.items():
        numeric = []
        for e in entries:
            try:
                numeric.append((e["document"], float(e["value"])))
            except (TypeError, ValueError, KeyError):
                continue
        if len(numeric) < 2:
            continue
        values = [v for _, v in numeric]
        base = max(abs(v) for v in values) or 1
        spread = (max(values) - min(values)) / base
        tol = tolerances.get(field_key, 0.0)
        if spread > tol:
            findings.append({
                "field_key": field_key,
                "values": [{"document": d, "value": v} for d, v in numeric],
                "recommended_correction": (
                    f"Align {field_key} across documents — values differ by "
                    f"{spread * 100:.1f}% (tolerance {tol * 100:.1f}%)."),
            })
    return findings
