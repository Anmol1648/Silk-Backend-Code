"""
Per-role canned fixtures (Doc 6 §3.3) — returned when ai_mocked() or the
role binding's is_mocked flag is on, and in dev/tests. Shapes match
fundos/llm/schemas.py exactly.
"""


def get_mock_response(role: str, context: dict | None = None) -> dict:
    context = context or {}
    company = (context.get("company") or {}).get("name", "the company")
    factory = _FACTORIES.get(role, _generic)
    return factory(company, context)


def _generic(company, context):
    return {"text": f"[MOCK] Insight for {company}.", "insights": [
        {"text": f"[MOCK] {company} shows a pattern worth noting.",
         "kind": "inference", "evidence": []}]}


def _research_synthesis(company, context):
    return {"fields": [
        {"fieldKey": "description",
         "value": f"{company} is a technology company (mock research).",
         "factOrInference": "fact", "evidence": ["website"], "confidence": 0.9},
        {"fieldKey": "sector", "value": "SaaS",
         "factOrInference": "inference", "evidence": ["website", "linkedin"],
         "confidence": 0.7},
        {"fieldKey": "founding_year", "value": 2021,
         "factOrInference": "fact", "evidence": ["public_funding"],
         "confidence": 0.95},
    ]}


def _material_critique(company, context):
    return {
        "detected_type": "deck",
        "summary": "[MOCK] A 14-slide seed pitch deck covering problem, "
                   "solution, traction and ask.",
        "extracted_metrics": [{"fieldKey": "arr", "value": 12000000,
                               "ccy": "INR", "confidence": 0.8}],
        "inconsistencies": [],
        "missing_information": ["team slide", "use of funds"],
        "recommendations": [
            {"target_section": "traction", "issue": "No cohort data",
             "suggested_change": "Add a retention cohort chart",
             "estimated_effort_min": 45},
        ],
    }


def _readiness_summary(company, context):
    """Tester Issues 9 & 13: the old static text ("story and team are
    strong") contradicted the real dimension statuses computed from the
    founder's actual data. Even in mocked mode the narrative is now derived
    from the deterministic result passed in context, so summary and
    dimension cards can never disagree."""
    det = context.get("deterministic_result") or {}
    dims = det.get("dimensions") or []
    band = det.get("overall_band") or "in progress"

    ready = [d["dimension"] for d in dims if d.get("status") == "ready"]
    attention = [d["dimension"] for d in dims
                 if d.get("status") == "needs_attention"]
    missing = [d["dimension"] for d in dims if d.get("status") == "missing"]

    parts = [f"[MOCK] {company} is assessed as '{band}'."]
    if ready:
        parts.append("Well documented: " + ", ".join(ready) + ".")
    if attention:
        parts.append("Needs attention: " + ", ".join(attention) + ".")
    if missing:
        parts.append("Not yet evidenced: " + ", ".join(missing) + ".")
    if not dims:
        parts.append("No dimension data was available for this assessment.")

    _EXPL = {
        "ready": "Supporting information is present and internally "
                 "consistent for this dimension.",
        "needs_attention": "Partial information exists — complete the "
                           "flagged fields or upload supporting documents.",
        "missing": "No supporting information found yet — add the relevant "
                   "knowledge-base facts or materials.",
        "not_applicable": "Not applicable for this company profile.",
    }
    return {
        "summary": " ".join(parts),
        "dimension_explanations": {
            d["dimension"]: f"{d['dimension']}: " + _EXPL.get(
                d.get("status"), "Assessed from the available data.")
            for d in dims
        },
    }


def _peer_insight(company, context):
    return {"insights": [
        {"text": f"[MOCK] {company}'s acquisition pattern resembles Peer A's "
                 "first two years.", "kind": "inference",
         "evidence": ["peer_milestones"]},
        {"text": "[MOCK] Peer A raised its Series A 18 months after seed.",
         "kind": "fact", "evidence": ["market_deal:ma-1"]},
    ]}


def _raise_narrative(company, context):
    return {
        "narrative": f"[MOCK] Based on runway needs and peer benchmarks, "
                     f"{company} should raise enough to reach the next "
                     "value-inflection milestone — not as much as possible.",
        "facts": ["Current runway per CKB"],
        "inferences": ["Milestone timing inferred from peer journeys"],
        "assumptions": ["Burn stays within 10% of current levels"],
    }


def _valuation_negotiation(company, context):
    return {
        "supporting_arguments": [
            {"text": "[MOCK] Growth rate above peer median supports the "
                     "upper half of the range.", "evidence": ["peers"]}],
        "investor_objections": [
            {"text": "[MOCK] Revenue concentration in top-2 customers."}],
        "suggested_responses": [
            {"text": "[MOCK] Show the signed pipeline diversifying "
                     "concentration within two quarters."}],
        "positioning_statement": f"[MOCK] {company} resembles Peer A roughly "
                                 "18 months before its Series A.",
    }


def _instrument_explainer(company, context):
    return {"instruments": [
        {"instrument": "equity", "pros": ["Clean cap table"],
         "cons": ["Requires agreed valuation now"],
         "mechanics_explainer": "[MOCK] Priced round mechanics…",
         "market_context": "Common at Series A.", "applicability_note": ""},
        {"instrument": "safe", "pros": ["Fast, low legal cost"],
         "cons": ["Dilution uncertainty until conversion"],
         "mechanics_explainer": "[MOCK] Converts at next priced round…",
         "market_context": "Common at pre-seed/seed.", "applicability_note": ""},
        {"instrument": "ccd", "pros": ["FEMA-friendly for Indian entities"],
         "cons": ["Coupon/holding-period terms to negotiate"],
         "mechanics_explainer": "[MOCK] Compulsorily convertible debenture…",
         "market_context": "Common in India.", "applicability_note": ""},
    ]}


def _strategy_blueprint(company, context):
    return {
        "executive_summary": f"[MOCK] {company} should pursue the Recommended "
                             "path: a focused raise at the peer-supported "
                             "valuation range.",
        "why_this_strategy": "[MOCK] It balances runway, dilution and "
                             "milestone risk given readiness.",
        "risks": [{"risk": "Market cooling", "likelihood": "medium",
                   "impact": "high", "mitigation": "Start earlier; widen "
                   "investor universe."}],
        "success_factors": ["Complete the financial model",
                            "Secure two enterprise customers"],
        "next_milestones": ["Financial model approved",
                            "IM drafted and reviewed"],
        "current_position": {"summary": "[MOCK] Seed-stage, early traction."},
    }


def _investment_story(company, context):
    components = ["vision", "problem", "current_solutions", "solution",
                  "why_now", "market", "business_model", "advantage",
                  "traction", "growth", "investment", "exit"]
    return {
        "components": [{"component": c,
                        "content": f"[MOCK] {c.replace('_', ' ').title()} "
                                   f"narrative for {company}.",
                        "evidence": ["ckb"]} for c in components],
        "positioning_options": [
            {"text": f"[MOCK] {company}: the system of record for X.",
             "rationale": "Category framing."},
            {"text": f"[MOCK] {company}: AI-native workflow for Y.",
             "rationale": "Technology framing."},
            {"text": f"[MOCK] {company}: the fastest path to Z.",
             "rationale": "Outcome framing."},
        ],
        "thesis_primary": f"[MOCK] {company} converts a large manual spend "
                          "pool into software revenue with proven retention.",
        "message_blocks": [
            {"blockType": "elevator", "content": f"[MOCK] 30-second pitch for {company}."},
            {"blockType": "intro30", "content": "[MOCK] Intro."},
            {"blockType": "overview2m", "content": "[MOCK] 2-minute overview."},
            {"blockType": "overview5m", "content": "[MOCK] 5-minute overview."},
            {"blockType": "ask", "content": "[MOCK] The investment ask."},
        ],
    }


def _teaser(company, context):
    sections = ["overview", "problem", "solution", "market", "business_model",
                "traction", "team", "investment_opportunity", "fund_raise",
                "contact"]
    return {
        "headline": f"[MOCK] {company} — investing in the future of X",
        "thesis": "[MOCK] Compounding retention with efficient acquisition.",
        "tagline": "[MOCK] Built for the next decade.",
        "sections": [{"sectionKey": s, "title": s.replace("_", " ").title(),
                      "body": f"[MOCK] {s} content.", "isMandatory": s in
                      ("overview", "problem", "solution", "fund_raise")}
                     for s in sections],
        "suggestions": [{"sectionKey": "awards", "reason": "[MOCK] Add awards."}],
    }


def _pitch_story(company, context):
    keys = ["vision", "problem", "solution", "market", "product",
            "business_model", "traction", "financials", "growth", "ask"]
    return {"narrative_outline": [
        {"key": k, "title": k.replace("_", " ").title(), "order": i + 1}
        for i, k in enumerate(keys)]}


def _pitch_slides(company, context):
    outline = (context.get("narrative_outline")
               or _pitch_story(company, context)["narrative_outline"])
    return {"slides": [
        {"slideType": item["key"], "headline": f"[MOCK] {item['title']}",
         "narrative": f"[MOCK] One-message narrative for {item['title']}.",
         "keyMetrics": [], "chartSpec": None, "icons": [],
         "speakerNotes": "[MOCK] Speaker notes.",
         "investorTakeaway": f"[MOCK] Takeaway for {item['title']}."}
        for item in outline]}


def _fin_model_assist(company, context):
    return {"assumptions": [
        {"group": "revenue", "key": "new_customers_per_month", "value_num": 6,
         "unit": "count", "source": "ai_default_peer"},
        {"group": "revenue", "key": "arpa_monthly", "value_num": 150000,
         "unit": "INR", "source": "ai_default_peer"},
        {"group": "revenue", "key": "monthly_churn_pct", "value_num": 1.5,
         "unit": "%", "source": "ai_default_peer"},
        {"group": "hiring", "key": "monthly_hiring_cost_growth_pct",
         "value_num": 3, "unit": "%", "source": "ai_default_peer"},
        {"group": "opex", "key": "opex_pct_of_revenue", "value_num": 35,
         "unit": "%", "source": "ai_default_peer"},
    ], "explanation": "[MOCK] Defaults benchmarked from Group-A peers."}


def _fin_review(company, context):
    return {"findings": [
        {"area": "growth_assumptions", "severity": "medium",
         "text": "[MOCK] Month-24 growth exceeds the peer 75th percentile.",
         "action": "investigate"}]}


def _im_section(company, context):
    section_keys = context.get("section_keys") or [
        "executive_summary", "investment_highlights", "company_overview",
        "founder_story", "market", "industry", "product_technology",
        "business_model", "traction", "sales_marketing", "competition",
        "financial_performance", "financial_forecast", "use_of_funds",
        "valuation_summary", "risk_factors"]
    return {"sections": [
        {"sectionKey": k, "title": k.replace("_", " ").title(),
         "body": f"[MOCK] {k} narrative for {company}.", "isMandatory": True}
        for k in section_keys]}


def _im_consistency(company, context):
    return {"flags": [
        {"fieldKey": "arr", "message": "[MOCK] ARR in the IM matches the "
         "financial model.", "severity": "info"}]}


def _package_review(company, context):
    return {
        "category_notes": {
            "story": "[MOCK] Coherent and differentiated.",
            "commercial": "[MOCK] Traction well evidenced.",
            "financial": "[MOCK] Model balanced; assumptions defensible.",
            "presentation": "[MOCK] Consistent visual hierarchy.",
            "investment": "[MOCK] Clear ask aligned with strategy.",
            "completeness": "[MOCK] All mandatory documents present.",
        },
        "recommendations": [
            {"priority": "high", "category": "financial",
             "reason": "[MOCK] Add sensitivity to the hiring plan.",
             "business_impact": "Improves diligence resilience.",
             "affected_documents": ["model"],
             "estimated_improvement": "quality +1 band"}],
        "risk_summary": ["[MOCK] Customer concentration"],
        "outstanding_actions": ["[MOCK] Resolve open comments on the IM"],
    }


def _objection_sim(company, context):
    return {"objections": [
        {"investor_type": "seed_vc",
         "question": "[MOCK] Why now — what changed in the market?",
         "reason": "Timing risk", "supporting_data": {},
         "suggested_response": "[MOCK] Reference the regulatory change and "
                               "cohort adoption curve.",
         "evidence": ["ckb.why_now"], "confidence": "medium",
         "priority": "high"}]}


def _company_profile_section(company, context):
    """Whole-section profile prose (Company Profile §5.4).

    Previously this role had no fixture and fell through to _generic, whose
    {text, insights} shape carries no `content`/`summary`, so mocked profile
    sections came back empty and were flagged needs_input. That masked the
    section editors in a mocked QA environment. Return real prose keyed to
    the section so the page renders meaningfully under the mock provider.
    """
    label = (context.get("section_label")
             or (context.get("section_key") or "section").replace("_", " ").title())
    return {
        "content": f"[MOCK] {label} for {company}: a concise, investor-ready "
                   f"summary generated from the company's sources.",
        "needsInput": False,
        "missing": [],
    }


def _company_profile_structured(company, context):
    """Structured §7 data for the four narrative-but-structured sections
    (products_services, customers_markets, competitive_advantages,
    business_model). Returns the exact shape the serializer maps into
    sections.data, plus a `content` summary so nothing is lost when thin.
    """
    key = context.get("section_key") or ""
    if key == "products_services":
        return {
            "items": [
                {"name": f"[MOCK] {company} Platform", "category": "Software",
                 "description": "Core product offering (mock)."},
            ],
            "content": f"[MOCK] {company} offers a primary software platform.",
            "needsInput": False, "missing": [],
        }
    if key == "customers_markets":
        return {
            "items": [
                {"market": "[MOCK] SMB", "customer_type": "B2B",
                 "geography": "India"},
            ],
            "content": f"[MOCK] {company} primarily serves B2B SMB customers.",
            "needsInput": False, "missing": [],
        }
    if key == "competitive_advantages":
        return {
            "items": [
                {"title": "[MOCK] Product depth",
                 "description": "Illustrative differentiation from mock."},
            ],
            "content": f"[MOCK] {company}'s edge is product depth.",
            "needsInput": False, "missing": [],
        }
    if key == "business_model":
        return {
            "object": {
                "customer_type": "[MOCK] B2B",
                "value_proposition": "Illustrative value proposition (mock).",
                "delivery_model": "SaaS",
                "pricing_model": "Subscription",
                "sales_model": "Inside sales",
                "distribution_channels": ["Direct", "Partners"],
            },
            "content": f"[MOCK] {company} runs a B2B SaaS subscription model.",
            "needsInput": False, "missing": [],
        }
    # Unknown section — safe empty shape.
    return {"items": [], "content": "", "needsInput": True, "missing": []}


def _company_profile_field(company, context):
    """Single-field regeneration for a structured section.

    Returns just the one field's value; the service merges it back without
    disturbing the rest of the record.
    """
    field_key = context.get("field_key") or "value"
    return {
        "fieldKey": field_key,
        "value": f"[MOCK] {field_key} for {company}",
    }


def _company_profile_records(company, context):
    """Structured records / form-items for a profile section.

    Returns rows shaped for the section under generation, so a mocked
    environment demonstrates the auto-populate behaviour end to end. The
    'source' marker on each row is 'ai_research'; fields the (real) model
    can't determine should be omitted so they surface as needs-input.
    """
    key = context.get("section_key") or ""
    demo = {
        "founders": [
            {"name": f"[MOCK] Founder, {company}",
             "designation": "CEO & Co-Founder",
             "experience": "Illustrative founder background from mock provider.",
             "linkedinUrl": "", "biography": ""},
        ],
        "key_people": [
            {"name": f"[MOCK] Head of Product, {company}",
             "designation": "Head of Product", "linkedinUrl": "", "biography": ""},
        ],
        "competitors": [
            {"name": "[MOCK] Competitor A", "website": "", "geography": "",
             "description": "Illustrative competitor from mock provider."},
        ],
        "funding_history": [
            {"roundName": "[MOCK] Seed", "amount": None, "ccy": "",
             "investors": [], "notes": "Mock — verify against your records."},
        ],
        "recent_news": [
            {"headline": f"[MOCK] {company} in the news", "url": "",
             "publisher": "", "summary": "Illustrative item from mock provider."},
        ],
        "revenue_model": {"kind": "streams", "items": [
            {"label": "[MOCK] Subscriptions", "pct": 70},
            {"label": "[MOCK] Services", "pct": 30}]},
        "company_metrics": {"kind": "metrics", "items": [
            {"label": "ARR", "value": None, "unit": "USD"}]},
        "financial_summary": {"kind": "fiscal_years", "items": [
            {"fiscalYear": "FY23", "revenue": 24, "grossMarginPct": 70,
             "ebitda": -2, "ccy": "USD"},
            {"fiscalYear": "FY24", "revenue": 35, "grossMarginPct": 78,
             "ebitda": 4, "ccy": "USD"}],
            "observations": [
                "[MOCK] Path to profitability achieved in late FY24.",
                "[MOCK] Strong gross margins driven by software mix."]},
    }
    payload = demo.get(key)
    if isinstance(payload, dict):        # structured form
        return {"structured": payload, "missing": [], "needsInput": False}
    return {"records": payload or [], "missing": [], "needsInput": not payload}


def _assessment_inputs(company, context):
    """A deliberately PARTIAL answer, echoing only what was asked.

    Returning everything would make mocked runs look like full coverage and
    hide the suppression gate — the behaviour most worth exercising in dev,
    because a real run never answers everything. Keys are echoed from the
    request rather than hardcoded, so the mock cannot drift from the workbook
    contract the way the old hand-written list did.
    """
    asked = [p.get("inputKey") for p in (context.get("parameters") or [])]
    canned = {
        "TEAM_FDR_EXP": ("12", "Years"), "TEAM_COFDR_YRS": ("6", "Years"),
        "TEAM_INST_INV": ("2", "Count"), "TEAM_ADVISORS": ("3", "Count"),
        "BQ_CO_AGE": ("7", "Years"), "BQ_REC_REV": ("68", "%"),
        "BQ_TOP5_CLIENT": ("38", "%"), "BQ_NRR": ("112", "%"),
        "SEC_TAM": ("14.5", "US$ Bn"), "SEC_TAM_CAGR": ("18", "%"),
        "SEC_TAM_SUB": ("3.2", "US$ Bn"), "SEC_TAM_CAGR_SUB": ("22", "%"),
        "SEC_PEER_AGE": ("9", "Years"), "SEC_PEER_AGE_SUB": ("6", "Years"),
        "SEC_PEER_RAISE_MTHS": ("7", "Months"),
        "SEC_PEER_RAISE_MTHS_SUB": ("11", "Months"),
    }
    values = [{"inputKey": k, "value": v, "unit": u,
               "sourceUrl": f"https://example.com/mock/{k.lower()}",
               "sourceTier": 2, "confidence": 0.8,
               "justification": f"[MOCK] {k} for {company}."}
              for k, (v, u) in canned.items() if k in asked]

    anchors = [p.get("inputKey")
               for p in (context.get("qualitative_parameters") or [])]
    bands = [{"inputKey": k, "band": "Good", "confidence": 0.75,
              "justification": f"[MOCK] evidence for {k} reads as Good.",
              "sourceUrl": f"https://example.com/mock/{str(k).lower()}"}
             for k in anchors[:4]]

    return {"values": values, "bands": bands, "sector": "Fintech",
            "subSector": "Insurtech",
            "missing": ["[MOCK] financial model not supplied"]}


def _profile_research_batch(company, context):
    """A prose research answer, shaped like the real thing.

    Returned as markdown under `text` because that is what the live role
    returns — a mock that returned a dict here would make the mocked path
    diverge from production at the caller, so development would exercise a
    branch that never runs for real.
    """
    topic = (context or {}).get("topic") or "General"
    return {"text": (
        f"## [MOCK] {topic}\n\n"
        f"### What does {company} do?\n\n"
        f"{company} is a technology company (mock research; no search was "
        f"performed). (Source: mock, https://example.invalid)\n\n"
        f"### What is its funding history?\n\n"
        f"Not found.\n")}


def _profile_synthesis(company, context):
    """A complete, VALID 17-section profile.

    Every model-produced section carries plausible content. That matters more
    than it looks: mocked mode is the only way the pipeline is exercised in
    development and CI, and it is one call now rather than fifteen — so a
    thin mock does not merely under-fill the profile, it makes every
    downstream behaviour (completeness, the CKB financial sync, structured
    forms, the records CRUD) untestable without a live key.

    `document_center` is omitted deliberately: the pipeline overwrites it from
    its own upload records regardless of what comes back here.
    """
    return {"sections": {
        "company_profile": {"data": {
            "description_of_business":
                f"[MOCK] {company} builds software for mid-market teams. "
                f"This is canned sample content, not research.",
            "website": "https://example.invalid",
            "country": "IN", "macro_sector": "Technology",
            "sub_sector": "SaaS", "funding_status_name": "Seed Funded",
            "revenue_size_name": "USD 1M to 5M", "currency_id": "INR",
            "total_funding_raised_usd_mn": 4.5,
            "last_funding_round_date": "2025-03-01",
            "latest_pre_money_usd_mn": 20.0,
            "latest_post_money_usd_mn": 24.5,
        }},
        "founders": {"data": [
            {"name": "[MOCK] A. Founder", "role": "CEO",
             "background": "Mock background: prior operator, two exits.",
             "linkedin_url": "", "is_full_time": True, "is_founder": True},
            {"name": "[MOCK] B. Engineer", "role": "VP Engineering",
             "background": "Mock background: platform engineering lead.",
             "linkedin_url": "", "is_full_time": True, "is_founder": False},
        ]},
        "products_services": {"data": [
            {"name": "[MOCK] Core Platform", "category": "Software",
             "description": "The main product."},
        ]},
        "customers_markets": {"data": [
            {"market": "Mid-market SaaS", "customer_type": "SMB",
             "geography": "India"},
        ]},
        "competitive_advantages": {"data": [
            {"title": "[MOCK] Data advantage",
             "description": "Accumulated usage data raises switching costs."},
        ]},
        "business_model": {"data": {
            "business_model_types": ["B2B", "SaaS"],
            "customer_type": "SMB",
            "value_proposition": "[MOCK] Cheaper and faster than incumbents.",
            "delivery_model": "Cloud", "pricing_model": "Subscription",
            "sales_model": "Inside sales",
            "distribution_channels": ["Direct", "Partners"],
        }},
        "revenue_model": {"data": [
            {"stream": "Subscriptions", "share_percent": 80},
            {"stream": "Services", "share_percent": 20},
        ]},
        "company_metrics": {"data": [
            {"metric": "ARR", "value": "2.4", "unit": "USD mn"},
            {"metric": "Active Customers", "value": "180", "unit": "customers"},
        ]},
        "financial_summary": {"data": {
            "financials": [
                {"financial_year": "FY 2024", "is_estimate": False,
                 "revenue_m": 1.8, "ebitda_m": -0.6, "pat_m": -0.8,
                 "yoy_revenue_growth_pct": 60.0, "ev_revenue_multiple": None,
                 "currency": "USD"},
                {"financial_year": "FY 2025", "is_estimate": True,
                 "revenue_m": 2.4, "ebitda_m": -0.2, "pat_m": -0.3,
                 "yoy_revenue_growth_pct": 33.3, "ev_revenue_multiple": None,
                 "currency": "USD"},
            ],
            "observations": ["[MOCK] Growth is steady and burn is narrowing."],
        }},
        "funding_history": {"data": [
            {"date": "2025-03-01", "round": "Seed", "amount_usd_mn": 4.5,
             "pre_money_usd_mn": 20.0, "post_money_usd_mn": 24.5,
             "investors": ["[MOCK] Sample Ventures"],
             "lead_investors": ["[MOCK] Sample Ventures"]},
        ]},
        "competitors": {"data": [
            {"name": "[MOCK] Rival Inc", "fy_year": 2024, "revenue": 8.0,
             "funding_usd_mn": 30.0, "status": "Private",
             "investors": ["[MOCK] Other Ventures"],
             "business_model": "B2B SaaS",
             "market_positioning": "Enterprise-first",
             "relative_scale": "Larger",
             "key_differentiators": ["Bigger install base"],
             "strengths": ["Brand"], "weaknesses": ["Slow to ship"],
             "recent_activity": []},
        ]},
        "news": {"data": [
            {"title": f"[MOCK] {company} raises seed round",
             "date": "2025-03-02",
             "description": "Canned sample news item.",
             "source": "Mock Wire", "link": "https://example.invalid/news"},
        ]},
        "investors_cap_table": {"data": {
            "cap_table_summary": {"total_investors": 1, "ownership": [
                {"stakeholder_category": "Founder", "ownership_pct": 70.0},
                {"stakeholder_category": "Investor", "ownership_pct": 18.0},
                {"stakeholder_category": "ESOP", "ownership_pct": 12.0},
            ]},
            "investors_list": [
                {"investor_name": "[MOCK] Sample Ventures",
                 "investor_type": "VC", "funding_amount_usd_mn": 4.5,
                 "valuation_usd_mn": 24.5, "dilution_pct": 18.0,
                 "date": "2025-03-01", "round": "Seed"},
            ],
        }},
        "company_story": {"data": {
            "origin_story": f"[MOCK] {company} began as an internal tool.",
            "brand_evolution": "Renamed at seed.",
            "usp": "[MOCK] The fastest path from data to decision.",
            "milestones": [{"date": "2025-03-01", "title": "Seed round",
                            "description": "Raised USD 4.5m."}],
        }},
        "industry_research": {"data": {
            "industry_evolution": "[MOCK] The category consolidated post-2020.",
            "market_sizing": {"tam": "USD 12bn (2024, mock source)",
                              "sam": "USD 3bn (2024, mock source)",
                              "som": "USD 240m (2024, mock source)"},
            "performance_trends": ["Shift to usage-based pricing"],
            "regulatory_developments": ["Data localisation rules"],
        }},
        "investment_thesis": {"data": {
            "opportunity_explanation":
                "[MOCK] Large market, credible team, improving unit economics.",
            "leadership_assessment": "[MOCK] Experienced, thin on go-to-market.",
            "risks_and_concerns": ["Customer concentration",
                                   "Well-funded incumbent"],
        }},
    }}


_FACTORIES = {
    "profile_research_batch": _profile_research_batch,
    "profile_synthesis": _profile_synthesis,
    "assessment_inputs": _assessment_inputs,
    "company_profile_section": _company_profile_section,
    "company_profile_structured": _company_profile_structured,
    "company_profile_field": _company_profile_field,
    "company_profile_records": _company_profile_records,
    "research_synthesis": _research_synthesis,
    "material_critique": _material_critique,
    "readiness_summary": _readiness_summary,
    "peer_insight": _peer_insight,
    "raise_narrative": _raise_narrative,
    "valuation_negotiation": _valuation_negotiation,
    "instrument_explainer": _instrument_explainer,
    "strategy_blueprint": _strategy_blueprint,
    "investment_story": _investment_story,
    "teaser": _teaser,
    "pitch_story": _pitch_story,
    "pitch_slides": _pitch_slides,
    "fin_model_assist": _fin_model_assist,
    "fin_review": _fin_review,
    "im_section": _im_section,
    "im_narrative": _im_section,
    "im_consistency": _im_consistency,
    "package_review": _package_review,
    "objection_sim": _objection_sim,
}
