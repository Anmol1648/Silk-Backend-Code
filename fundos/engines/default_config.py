"""
Default `scoring_config` v1 (Doc 2 M0.6 JSONB keys / Doc 5 Annexure).
Pure data — no Django imports. Seeded into the ScoringConfig table by
`seed_initial_data`; engines receive it as `cfg`.
"""

DEFAULT_SCORING_CONFIG_V1 = {
    "readiness": {
        # revenue (INR annual) → points
        "revMap": [
            {"lt": 1_000_000, "pts": 5},
            {"lt": 10_000_000, "pts": 12},
            {"lt": 50_000_000, "pts": 18},
            {"lt": 200_000_000, "pts": 24},
            {"lt": None, "pts": 30},
        ],
        # growth rate % → points
        "grMap": [
            {"lt": 0, "pts": 0}, {"lt": 20, "pts": 5}, {"lt": 50, "pts": 10},
            {"lt": 100, "pts": 15}, {"lt": None, "pts": 20},
        ],
        # company age (years) → points
        "ageMap": [
            {"lt": 1, "pts": 4}, {"lt": 3, "pts": 8}, {"lt": 6, "pts": 10},
            {"lt": None, "pts": 8},
        ],
        # runway (months) → points
        "raiMap": [
            {"lt": 3, "pts": 2}, {"lt": 6, "pts": 5}, {"lt": 12, "pts": 8},
            {"lt": None, "pts": 10},
        ],
        # narrative completeness bands (share of mandatory CKB present)
        "narBands": [
            {"lt": 0.4, "pts": 8}, {"lt": 0.7, "pts": 18},
            {"lt": 0.9, "pts": 25}, {"lt": None, "pts": 30},
        ],
        # dimension → the CKB field keys that evidence it
        "dimensionMap": {
            "Story": ["description", "vision", "problem", "solution"],
            "Financials": ["arr", "burn", "runway_months", "gross_margin"],
            "Market": ["sector", "geography", "segments"],
            "Team": ["founders", "leadership", "employees"],
            "Governance": ["cap_table", "board", "incorporation"],
            "Data": ["customers", "churn", "cac", "ltv"],
        },
        "bands": [
            {"gte": 70, "band": "Ready"},
            {"gte": 40, "band": "Needs preparation"},
            {"gte": 0, "band": "Not yet ready"},
        ],
        "dimension_ready_threshold": 0.75,
        "dimension_attention_threshold": 0.35,
    },
    "similarity_weights": {
        # Doc 1 M2-§2 default weights (admin-tunable), sum 120 → normalised
        "sector_subsector": 20, "business_model": 15, "revenue_stage": 15,
        "customer_profile": 10, "growth_rate": 10, "geography": 10,
        "funding_stage": 10, "team_size": 5, "founder_background": 5,
        "technology_market": 10,
    },
    "raise": {
        # runway_months target → multiple of monthly burn
        "runwayMult": {"12m": 12, "18m": 18, "24m": 24, "36m": 36},
        "plannedAmountMap": {"buffer_pct": 20, "range_spread_pct": 25},
        "dimensionWeights": {
            "business_maturity": 15, "commercial_traction": 25,
            "capital_efficiency": 15, "founder_strength": 10,
            "market_opportunity": 20, "investor_appetite": 15,
        },
        # funding bands (USD) → label + typical dilution range
        "bands": [
            {"high": 250_000, "label": "angel", "dil": [8, 15]},
            {"high": 1_000_000, "label": "pre_seed", "dil": [10, 18]},
            {"high": 3_000_000, "label": "seed", "dil": [12, 22]},
            {"high": 8_000_000, "label": "pre_series_a", "dil": [15, 25]},
            {"high": 20_000_000, "label": "series_a", "dil": [15, 25]},
            {"high": None, "label": "growth", "dil": [10, 20]},
        ],
    },
    "valuation_methods": {
        "enabled": ["ev_arr", "ev_revenue", "ev_ebitda", "trading",
                    "transaction", "venture", "scorecard", "berkus", "dcf"],
        "weights": {"ev_arr": 0.25, "ev_revenue": 0.20, "trading": 0.15,
                    "transaction": 0.15, "venture": 0.10, "scorecard": 0.05,
                    "berkus": 0.05, "dcf": 0.05, "ev_ebitda": 0.0},
        "outlierPolicy": {"iqr_multiplier": 1.5},
        # fallback sector multiples when the market feed is not yet ratified
        "default_multiples": {
            "ev_arr": {"low": 5, "mid": 8, "high": 12},
            "ev_revenue": {"low": 3, "mid": 5, "high": 8},
            "ev_ebitda": {"low": 8, "mid": 12, "high": 18},
        },
        "berkus_max_per_factor_usd": 500_000,
    },
    "dilution": {
        "esop_topup_default_pct": 10,
        "safe_discount_default_pct": 20,
    },
    "crossdoc_tolerances": {
        "arr": 0.02, "revenue": 0.02, "employees": 0.0,
        "use_of_funds": 0.05, "burn": 0.05,
    },
}
