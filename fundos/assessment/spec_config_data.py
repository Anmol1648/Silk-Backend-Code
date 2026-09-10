"""Scoring config extracted verbatim from Fundraising_Journey_Spec.md.

Generated, not hand-written — every value traces to the spec so a reviewer
can diff it against the source document. Do not edit here; edit the spec and
re-extract, or change the value in Django Admin (config rows are versioned).
"""
import json

RUBRICS = json.loads(r"""[
 {
  "input_key": "TEAM_FDR_EXP",
  "name": "Founder Years of Experience in the Industry",
  "unit": "Years",
  "direction": "Higher",
  "ref_code": "A.1.d",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "10",
    "6",
    "3"
   ],
   "Series A": [
    "10",
    "6",
    "3"
   ],
   "Series B": [
    "12",
    "8",
    "4"
   ],
   "Growth": [
    "15",
    "10",
    "5"
   ]
  },
  "rationale": "Deep domain time is the single best predictor of founder judgement; later stages should have attracted more seasoned operators."
 },
 {
  "input_key": "TEAM_COFDR_YRS",
  "name": "Years Founders Have Worked Together / Known Each Other",
  "unit": "Years",
  "direction": "Higher",
  "ref_code": "A.1.c",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "5",
    "3",
    "1"
   ],
   "Series A": [
    "6",
    "4",
    "2"
   ],
   "Series B": [
    "7",
    "4",
    "2"
   ],
   "Growth": [
    "8",
    "5",
    "2"
   ]
  },
  "rationale": "Co-founder blow-ups are a top-3 cause of failure; tenure together is the cheapest proxy for trust."
 },
 {
  "input_key": "TEAM_CXO_SEATS",
  "name": "Key CXO Seats Filled (of 4: Product, Sales, Finance, Ops)",
  "unit": "Count",
  "direction": "Higher",
  "ref_code": "A.2 (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "3",
    "2",
    "1"
   ],
   "Series A": [
    "3",
    "2",
    "1"
   ],
   "Series B": [
    "4",
    "3",
    "2"
   ],
   "Growth": [
    "4",
    "3",
    "2"
   ]
  },
  "rationale": "Reference only \u2014 A.2 is computed from its four sub-items. Use this as a cross-check on the sub-item ratings."
 },
 {
  "input_key": "TEAM_INST_INV",
  "name": "Institutional Investors on the Cap Table",
  "unit": "Count",
  "direction": "Higher",
  "ref_code": "A.3.c",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "3",
    "2",
    "1"
   ],
   "Series A": [
    "3",
    "2",
    "1"
   ],
   "Series B": [
    "4",
    "3",
    "1"
   ],
   "Growth": [
    "5",
    "3",
    "2"
   ]
  },
  "rationale": "Counts SEBI-registered / institutional funds only \u2014 angels and family offices excluded. Fair cut of 0.5 at Seed means at least one."
 },
 {
  "input_key": "TEAM_MARQUEE_CLIENTS",
  "name": "Marquee Clients or Partners Won Through Founder's Network",
  "unit": "Count",
  "direction": "Higher",
  "ref_code": "A.1.b",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "3",
    "2",
    "1"
   ],
   "Series A": [
    "3",
    "2",
    "1"
   ],
   "Series B": [
    "5",
    "3",
    "1"
   ],
   "Growth": [
    "8",
    "4",
    "2"
   ]
  },
  "rationale": "Turns \"well connected\" into evidence. Count only named logos where the founder's relationship demonstrably opened the door."
 },
 {
  "input_key": "TEAM_PRIOR_VENTURES",
  "name": "Prior Ventures Founded (any outcome)",
  "unit": "Count",
  "direction": "Higher",
  "ref_code": "A.1.e (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "3",
    "2",
    "1"
   ],
   "Series A": [
    "3",
    "2",
    "1"
   ],
   "Series B": [
    "3",
    "2",
    "1"
   ],
   "Growth": [
    "3",
    "2",
    "1"
   ]
  },
  "rationale": "Reference row \u2014 count alone is a weak signal, so A.1.e is scored on the outcome anchors instead."
 },
 {
  "input_key": "TEAM_ADVISORS",
  "name": "Actively Engaged Advisors with Senior Domain Experience",
  "unit": "Count",
  "direction": "Higher",
  "ref_code": "A.3.b",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "3",
    "2",
    "1"
   ],
   "Series A": [
    "3",
    "2",
    "1"
   ],
   "Series B": [
    "4",
    "2",
    "1"
   ],
   "Growth": [
    "4",
    "2",
    "1"
   ]
  },
  "rationale": "\"Actively engaged\" means a documented cadence or equity/fee arrangement. Logos on a website do not count."
 },
 {
  "input_key": "FIN_REV_SCALE",
  "name": "Annualised Net Revenue (Run-Rate)",
  "unit": "INR Cr",
  "direction": "Higher",
  "ref_code": "B.1",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "25",
    "10",
    "3"
   ],
   "Series A": [
    "75",
    "40",
    "15"
   ],
   "Series B": [
    "250",
    "125",
    "60"
   ],
   "Growth": [
    "750",
    "400",
    "200"
   ]
  },
  "rationale": "Net revenue, not GMV. This is the row that punishes an old company still at seed-scale revenue."
 },
 {
  "input_key": "FIN_REV_GROWTH",
  "name": "Net Revenue Growth, YoY",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "B.2",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "150",
    "100",
    "50"
   ],
   "Series A": [
    "100",
    "70",
    "40"
   ],
   "Series B": [
    "70",
    "50",
    "30"
   ],
   "Growth": [
    "40",
    "25",
    "15"
   ]
  },
  "rationale": "Growth bar falls with stage because the base rises. Check against GMV growth \u2014 divergence is a quality flag."
 },
 {
  "input_key": "FIN_CM1",
  "name": "Gross Margin / CM1",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "B.3",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "50",
    "35",
    "20"
   ],
   "Series A": [
    "55",
    "40",
    "25"
   ],
   "Series B": [
    "60",
    "45",
    "30"
   ],
   "Growth": [
    "60",
    "48",
    "35"
   ]
  },
  "rationale": "Revenue less direct COGS and fulfilment. Bar rises with stage \u2014 scale should buy input leverage."
 },
 {
  "input_key": "FIN_CM2",
  "name": "CM2 Margin (Post Variable Marketing)",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "B.4",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "25",
    "12",
    "0"
   ],
   "Series A": [
    "30",
    "18",
    "5"
   ],
   "Series B": [
    "35",
    "22",
    "10"
   ],
   "Growth": [
    "40",
    "28",
    "15"
   ]
  },
  "rationale": "CM1 less CAC and variable marketing. Negative CM2 at any stage means the unit economics do not work yet."
 },
 {
  "input_key": "FIN_EBITDA_M",
  "name": "EBITDA Margin",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "B.5",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "0",
    "-15",
    "-40"
   ],
   "Series A": [
    "5",
    "-5",
    "-25"
   ],
   "Series B": [
    "12",
    "3",
    "-10"
   ],
   "Growth": [
    "18",
    "10",
    "0"
   ]
  },
  "rationale": "Losses are tolerated early and penalised hard at Growth. Exclude ESOP and one-offs; state adjustments in findings."
 },
 {
  "input_key": "FIN_PAT_M",
  "name": "PAT Margin",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "B.6",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "-5",
    "-20",
    "-45"
   ],
   "Series A": [
    "0",
    "-10",
    "-30"
   ],
   "Series B": [
    "8",
    "0",
    "-15"
   ],
   "Growth": [
    "12",
    "6",
    "-2"
   ]
  },
  "rationale": "Sits below EBITDA by interest, depreciation and tax \u2014 a wide EBITDA-to-PAT gap flags leverage or heavy capex."
 },
 {
  "input_key": "FIN_RUNWAY",
  "name": "Cash Runway at Current Net Monthly Burn",
  "unit": "Months",
  "direction": "Higher",
  "ref_code": "B.7",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "24",
    "18",
    "12"
   ],
   "Series A": [
    "24",
    "18",
    "12"
   ],
   "Series B": [
    "21",
    "15",
    "9"
   ],
   "Growth": [
    "18",
    "12",
    "6"
   ]
  },
  "rationale": "Closing cash / trailing-3-month average net burn, pre-money. Under the Fair cut the company is raising from weakness \u2014 read against Seller Motivation (D.8)."
 },
 {
  "input_key": "FIN_BURN_MULT",
  "name": "Burn Multiple (Net Burn / Net New Revenue)",
  "unit": "x",
  "direction": "Lower",
  "ref_code": "B.7 (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "2",
    "3",
    "5"
   ],
   "Series A": [
    "1.5",
    "2.5",
    "4"
   ],
   "Series B": [
    "1.2",
    "2",
    "3"
   ],
   "Growth": [
    "1",
    "1.5",
    "2.5"
   ]
  },
  "rationale": "Capital efficiency: rupees burnt per rupee of new revenue. Reference row \u2014 use to sanity-check the runway score, not replace it."
 },
 {
  "input_key": "FIN_WC_DAYS",
  "name": "Net Working Capital Cycle",
  "unit": "Days",
  "direction": "Lower",
  "ref_code": "B.8",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "15",
    "45",
    "75"
   ],
   "Series A": [
    "15",
    "40",
    "70"
   ],
   "Series B": [
    "10",
    "35",
    "60"
   ],
   "Growth": [
    "5",
    "30",
    "55"
   ]
  },
  "rationale": "Inventory + receivable days less payable days. Negative is excellent \u2014 the business is funded by its own float."
 },
 {
  "input_key": "FIN_DEBT_REV",
  "name": "Total Debt / Annualised Revenue",
  "unit": "%",
  "direction": "Lower",
  "ref_code": "B.9",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "5",
    "15",
    "30"
   ],
   "Series A": [
    "10",
    "20",
    "40"
   ],
   "Series B": [
    "15",
    "30",
    "50"
   ],
   "Growth": [
    "20",
    "40",
    "75"
   ]
  },
  "rationale": "Used instead of Debt/EBITDA because early-stage EBITDA is negative and the ratio breaks. Include venture debt and CCDs treated as debt."
 },
 {
  "input_key": "FIN_DEBT_EBITDA",
  "name": "Net Debt / EBITDA (only if EBITDA positive)",
  "unit": "x",
  "direction": "Lower",
  "ref_code": "B.9 (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "0.5",
    "1.5",
    "3"
   ],
   "Series A": [
    "1",
    "2",
    "3.5"
   ],
   "Series B": [
    "1.5",
    "2.5",
    "4"
   ],
   "Growth": [
    "2",
    "3",
    "4.5"
   ]
  },
  "rationale": "Reference row \u2014 leave blank when EBITDA is negative. Where both this and FIN_DEBT_REV are available, score on the worse of the two."
 },
 {
  "input_key": "BQ_REC_REV",
  "name": "Recurring / Contracted Share of Revenue",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "C.2",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "60",
    "35",
    "15"
   ],
   "Series A": [
    "65",
    "45",
    "20"
   ],
   "Series B": [
    "70",
    "50",
    "25"
   ],
   "Growth": [
    "75",
    "55",
    "30"
   ]
  },
  "rationale": "Subscription, AMC or contracted revenue with 12-month visibility. Repeat transactional revenue counts at half weight \u2014 note the treatment used."
 },
 {
  "input_key": "BQ_CHURN",
  "name": "Annual Revenue Churn",
  "unit": "%",
  "direction": "Lower",
  "ref_code": "C.2 (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "15",
    "25",
    "40"
   ],
   "Series A": [
    "12",
    "20",
    "32"
   ],
   "Series B": [
    "8",
    "15",
    "25"
   ],
   "Growth": [
    "5",
    "10",
    "20"
   ]
  },
  "rationale": "Reference row supporting C.2. Net revenue churn preferred; if only logo churn is available, say so."
 },
 {
  "input_key": "BQ_REV_PER_EMP",
  "name": "Revenue per Employee",
  "unit": "INR Lakh",
  "direction": "Higher",
  "ref_code": "C.4",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "25",
    "12",
    "6"
   ],
   "Series A": [
    "40",
    "20",
    "10"
   ],
   "Series B": [
    "60",
    "35",
    "18"
   ],
   "Growth": [
    "90",
    "50",
    "25"
   ]
  },
  "rationale": "Standard scalability proxy: does revenue grow faster than headcount? Include contract staff in the denominator."
 },
 {
  "input_key": "BQ_GM_TREND",
  "name": "Gross Margin Change, YoY",
  "unit": "bps",
  "direction": "Higher",
  "ref_code": "C.5",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "300",
    "100",
    "0"
   ],
   "Series A": [
    "300",
    "100",
    "0"
   ],
   "Series B": [
    "300",
    "100",
    "0"
   ],
   "Growth": [
    "300",
    "100",
    "0"
   ]
  },
  "rationale": "Pricing-power proxy: a company with real pricing power expands margin. Direction of travel matters more than level."
 },
 {
  "input_key": "BQ_TOP5_CLIENT",
  "name": "Top-5 Client Share of Revenue",
  "unit": "%",
  "direction": "Lower",
  "ref_code": "C.6",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "60",
    "75",
    "90"
   ],
   "Series A": [
    "45",
    "65",
    "80"
   ],
   "Series B": [
    "35",
    "55",
    "75"
   ],
   "Growth": [
    "30",
    "45",
    "65"
   ]
  },
  "rationale": "Concentration is tolerated at Seed (early customers are lumpy) and penalised at Growth, where it should have diversified."
 },
 {
  "input_key": "BQ_TOP1_CLIENT",
  "name": "Largest Single Client Share of Revenue",
  "unit": "%",
  "direction": "Lower",
  "ref_code": "C.6 (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "25",
    "40",
    "60"
   ],
   "Series A": [
    "20",
    "35",
    "50"
   ],
   "Series B": [
    "15",
    "28",
    "45"
   ],
   "Growth": [
    "12",
    "22",
    "35"
   ]
  },
  "rationale": "If the top-1 score is two bands worse than top-5, override C.6 down and flag it \u2014 single-client risk is the real exposure."
 },
 {
  "input_key": "BQ_TOP_SUPPLIER",
  "name": "Largest Supplier Share of COGS",
  "unit": "%",
  "direction": "Lower",
  "ref_code": "C.7",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "30",
    "50",
    "70"
   ],
   "Series A": [
    "25",
    "45",
    "65"
   ],
   "Series B": [
    "25",
    "45",
    "65"
   ],
   "Growth": [
    "20",
    "40",
    "60"
   ]
  },
  "rationale": "Score worse than the raw number implies if the supplier is also a competitor, sole-source, or an exclusive import agent."
 },
 {
  "input_key": "BQ_PATENTS",
  "name": "Granted Patents or Registered IP Central to the Product",
  "unit": "Count",
  "direction": "Higher",
  "ref_code": "C.3.a (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "3",
    "2",
    "1"
   ],
   "Series A": [
    "4",
    "2",
    "1"
   ],
   "Series B": [
    "5",
    "3",
    "1"
   ],
   "Growth": [
    "7",
    "4",
    "2"
   ]
  },
  "rationale": "Granted only \u2014 filings count as Fair at best, and an unenforced patent is not a moat."
 },
 {
  "input_key": "BQ_CAC_TREND",
  "name": "Change in Blended CAC, YoY",
  "unit": "%",
  "direction": "Lower",
  "ref_code": "C.3.b (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "-10",
    "0",
    "15"
   ],
   "Series A": [
    "-10",
    "0",
    "15"
   ],
   "Series B": [
    "-5",
    "5",
    "20"
   ],
   "Growth": [
    "-5",
    "5",
    "20"
   ]
  },
  "rationale": "The hard test of network effects: real ones make acquisition cheaper as you scale. Rising CAC alongside a network-effects claim is a contradiction \u2014 flag it."
 },
 {
  "input_key": "BQ_ORGANIC_PCT",
  "name": "Organic / Inbound Share of New Customers",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "C.3.c",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "50",
    "30",
    "15"
   ],
   "Series A": [
    "50",
    "30",
    "15"
   ],
   "Series B": [
    "55",
    "35",
    "18"
   ],
   "Growth": [
    "60",
    "40",
    "20"
   ]
  },
  "rationale": "Brand strength that shows up in the P&L: customers who arrive without being paid for."
 },
 {
  "input_key": "BQ_NRR",
  "name": "Net Revenue Retention (cohort, 12-month)",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "C.3.d",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "105",
    "90",
    "75"
   ],
   "Series A": [
    "110",
    "100",
    "85"
   ],
   "Series B": [
    "115",
    "105",
    "90"
   ],
   "Growth": [
    "120",
    "108",
    "95"
   ]
  },
  "rationale": "The cleanest stickiness measure there is. Above 100% the existing base grows on its own."
 },
 {
  "input_key": "BQ_UNIT_COST_ADV",
  "name": "Unit Cost vs. Largest Peer (negative = cheaper)",
  "unit": "%",
  "direction": "Lower",
  "ref_code": "C.3.e",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "-15",
    "-5",
    "5"
   ],
   "Series A": [
    "-15",
    "-5",
    "5"
   ],
   "Series B": [
    "-20",
    "-8",
    "3"
   ],
   "Growth": [
    "-20",
    "-10",
    "0"
   ]
  },
  "rationale": "A cost advantage that does not widen with scale is not a scale advantage."
 },
 {
  "input_key": "BQ_PRICE_CHG",
  "name": "Realised Price Increase Taken in Last 12 Months",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "C.5 (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "5",
    "2",
    "0"
   ],
   "Series A": [
    "5",
    "2",
    "0"
   ],
   "Series B": [
    "5",
    "2",
    "0"
   ],
   "Growth": [
    "5",
    "2",
    "0"
   ]
  },
  "rationale": "Pricing power is proven by having actually raised price without losing volume \u2014 check churn in the same period."
 },
 {
  "input_key": "DD_EXIST_INV",
  "name": "Existing Investors' Share of the Current Round",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "D.3",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "50",
    "30",
    "10"
   ],
   "Series A": [
    "40",
    "25",
    "10"
   ],
   "Series B": [
    "35",
    "20",
    "8"
   ],
   "Growth": [
    "30",
    "15",
    "5"
   ]
  },
  "rationale": "The single strongest conviction signal in the deal \u2014 insiders know most. Zero insider participation should force a hard look at Seller Motivation."
 },
 {
  "input_key": "DD_VAL_PREMIUM",
  "name": "Implied EV/Revenue Premium to Sub-Sector Median",
  "unit": "%",
  "direction": "Lower",
  "ref_code": "D.4",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "10",
    "40",
    "80"
   ],
   "Series A": [
    "0",
    "25",
    "60"
   ],
   "Series B": [
    "0",
    "20",
    "50"
   ],
   "Growth": [
    "0",
    "15",
    "40"
   ]
  },
  "rationale": "Premium over the peer median multiple. Negative (a discount) is excellent. Seed given more latitude because comparables are thin."
 },
 {
  "input_key": "DD_FDR_STAKE",
  "name": "Founders' Combined Stake, Post-Round",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "D.5",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "75",
    "60",
    "45"
   ],
   "Series A": [
    "60",
    "48",
    "35"
   ],
   "Series B": [
    "45",
    "35",
    "25"
   ],
   "Growth": [
    "35",
    "25",
    "15"
   ]
  },
  "rationale": "Expressed as remaining stake so higher is better. Below the Fair cut, founder incentives are impaired and future rounds get harder."
 },
 {
  "input_key": "DD_GROWTH_USE",
  "name": "Share of Proceeds Going to Growth (not losses or debt)",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "D.7",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "80",
    "60",
    "40"
   ],
   "Series A": [
    "80",
    "60",
    "40"
   ],
   "Series B": [
    "80",
    "60",
    "40"
   ],
   "Growth": [
    "80",
    "60",
    "40"
   ]
  },
  "rationale": "Growth = capex, hiring, market entry, product. Excludes funding existing losses, repaying debt or buying out shareholders."
 },
 {
  "input_key": "DD_RAISE_TO_ARR",
  "name": "Current Raise / Annualised Revenue",
  "unit": "x",
  "direction": "Lower",
  "ref_code": "D.2 (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "3",
    "5",
    "8"
   ],
   "Series A": [
    "2",
    "3.5",
    "6"
   ],
   "Series B": [
    "1.5",
    "2.5",
    "4"
   ],
   "Growth": [
    "1",
    "2",
    "3.5"
   ]
  },
  "rationale": "Sizes the ask against the business that exists today rather than the story. A high multiple here usually means the valuation is the real issue."
 },
 {
  "input_key": "DD_DIRECT_APPROACHES",
  "name": "Investors the Founder Has Already Approached Directly",
  "unit": "Count",
  "direction": "Lower",
  "ref_code": "D.6",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "2",
    "5",
    "12"
   ],
   "Series A": [
    "2",
    "5",
    "12"
   ],
   "Series B": [
    "3",
    "8",
    "15"
   ],
   "Growth": [
    "3",
    "8",
    "15"
   ]
  },
  "rationale": "A shopped deal is a damaged deal \u2014 every burnt name is one that can't be taken to market."
 },
 {
  "input_key": "SEC_TAM",
  "name": "Addressable Market (TAM)",
  "unit": "US$ Bn",
  "direction": "Higher",
  "ref_code": "E.1 / F.1",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "10",
    "3",
    "1"
   ],
   "Series A": [
    "10",
    "3",
    "1"
   ],
   "Series B": [
    "10",
    "3",
    "1"
   ],
   "Growth": [
    "10",
    "3",
    "1"
   ]
  },
  "rationale": "India-addressable TAM, not global. Cite the source and the build-up method \u2014 reject any figure derived by multiplying population \u00d7 price."
 },
 {
  "input_key": "SEC_TAM_CAGR",
  "name": "Market Growth Rate (CAGR, next 3\u20135 years)",
  "unit": "%",
  "direction": "Higher",
  "ref_code": "E.1 / F.1",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "25",
    "15",
    "8"
   ],
   "Series A": [
    "25",
    "15",
    "8"
   ],
   "Series B": [
    "25",
    "15",
    "8"
   ],
   "Growth": [
    "25",
    "15",
    "8"
   ]
  },
  "rationale": "Score E.1 / F.1 on the worse of TAM size and growth \u2014 a large but stagnant market is not attractive."
 },
 {
  "input_key": "SEC_PEER_AGE",
  "name": "Average Age of Top-3 Peers",
  "unit": "Years",
  "direction": "Higher",
  "ref_code": "E.4 / F.4",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "8",
    "5",
    "3"
   ],
   "Series A": [
    "8",
    "5",
    "3"
   ],
   "Series B": [
    "8",
    "5",
    "3"
   ],
   "Growth": [
    "8",
    "5",
    "3"
   ]
  },
  "rationale": "Established peers prove the category is real and give a reference point. Expect sub-sector peers to be younger than sector peers."
 },
 {
  "input_key": "SEC_PEER_RAISE_MTHS",
  "name": "Months Since Top-3 Peers' Last Capital Raise",
  "unit": "Months",
  "direction": "Lower",
  "ref_code": "E.5 / F.5",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "9",
    "18",
    "30"
   ],
   "Series A": [
    "9",
    "18",
    "30"
   ],
   "Series B": [
    "9",
    "18",
    "30"
   ],
   "Growth": [
    "9",
    "18",
    "30"
   ]
  },
  "rationale": "Recent peer raises mean live investor appetite. Stale raises mean the category has gone quiet."
 },
 {
  "input_key": "SEC_PEER_NEWS_CNT",
  "name": "Positive Funding/Expansion News on Top Peers, Last 6 Months",
  "unit": "Count",
  "direction": "Higher",
  "ref_code": "E.7.a",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "5",
    "3",
    "1"
   ],
   "Series A": [
    "5",
    "3",
    "1"
   ],
   "Series B": [
    "5",
    "3",
    "1"
   ],
   "Growth": [
    "5",
    "3",
    "1"
   ]
  },
  "rationale": "Count distinct events, not distinct articles about one event."
 },
 {
  "input_key": "SEC_SECTOR_SENTIMENT",
  "name": "Sector News Sentiment Ratio (Positive : Negative, Last 6 Months)",
  "unit": "x",
  "direction": "Higher",
  "ref_code": "E.7.b",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "3",
    "1.5",
    "0.8"
   ],
   "Series A": [
    "3",
    "1.5",
    "0.8"
   ],
   "Series B": [
    "3",
    "1.5",
    "0.8"
   ],
   "Growth": [
    "3",
    "1.5",
    "0.8"
   ]
  },
  "rationale": "Below 1.0x the sector is generating more bad news than good \u2014 a headwind regardless of company performance."
 },
 {
  "input_key": "IB_GTM_WEEKS",
  "name": "Weeks to Prepare Materials and Launch GTM",
  "unit": "Weeks",
  "direction": "Lower",
  "ref_code": "G.2",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "4",
    "8",
    "12"
   ],
   "Series A": [
    "4",
    "8",
    "12"
   ],
   "Series B": [
    "4",
    "8",
    "12"
   ],
   "Growth": [
    "4",
    "8",
    "12"
   ]
  },
  "rationale": "Driven by data-room readiness and audit quality. Anything past 12 weeks signals the company is not raise-ready."
 },
 {
  "input_key": "IB_CLOSE_MTHS",
  "name": "Expected Months from Launch to Close",
  "unit": "Months",
  "direction": "Lower",
  "ref_code": "G.3",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "4",
    "6",
    "9"
   ],
   "Series A": [
    "4",
    "6",
    "9"
   ],
   "Series B": [
    "5",
    "7",
    "10"
   ],
   "Growth": [
    "5",
    "8",
    "12"
   ]
  },
  "rationale": "Larger, later deals legitimately take longer, so the bar loosens at Series B and Growth."
 },
 {
  "input_key": "IB_DEAL_SIZE",
  "name": "Deal Size (Capital Being Raised)",
  "unit": "US$ Mn",
  "direction": "Higher",
  "ref_code": "G.5",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "5",
    "3",
    "1.5"
   ],
   "Series A": [
    "15",
    "8",
    "4"
   ],
   "Series B": [
    "40",
    "20",
    "10"
   ],
   "Growth": [
    "100",
    "50",
    "25"
   ]
  },
  "rationale": "Cross-check against the sub-sector average ticket in the deal database."
 },
 {
  "input_key": "IB_SECTOR_DEALS",
  "name": "Deals Closed in This Sector, Last 3 Years",
  "unit": "Count",
  "direction": "Higher",
  "ref_code": "G.1",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "3",
    "2",
    "1"
   ],
   "Series A": [
    "3",
    "2",
    "1"
   ],
   "Series B": [
    "3",
    "2",
    "1"
   ],
   "Growth": [
    "3",
    "2",
    "1"
   ]
  },
  "rationale": "Closed deals only \u2014 mandates that did not complete do not build a track record."
 },
 {
  "input_key": "IB_INVESTOR_RELS",
  "name": "Warm Investor Relationships Active in This Sub-Sector",
  "unit": "Count",
  "direction": "Higher",
  "ref_code": "G.1 (ref)",
  "is_reference_only": true,
  "stage_cuts": {
   "Seed": [
    "10",
    "5",
    "2"
   ],
   "Series A": [
    "12",
    "6",
    "3"
   ],
   "Series B": [
    "15",
    "8",
    "4"
   ],
   "Growth": [
    "20",
    "10",
    "5"
   ]
  },
  "rationale": "\"Warm\" means a first meeting without an introduction is possible."
 },
 {
  "input_key": "IB_LIVE_MANDATES",
  "name": "Live Mandates per Senior Banker on the Team",
  "unit": "Count",
  "direction": "Lower",
  "ref_code": "G.4",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "2",
    "3",
    "5"
   ],
   "Series A": [
    "2",
    "3",
    "5"
   ],
   "Series B": [
    "2",
    "3",
    "4"
   ],
   "Growth": [
    "1",
    "2",
    "4"
   ]
  },
  "rationale": "Capacity is a real constraint, not an afterthought. Larger deals need more senior bandwidth."
 },
 {
  "input_key": "BQ_CO_AGE",
  "name": "Years Since Inception",
  "unit": "Years",
  "direction": "Range",
  "ref_code": "C.1",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "1",
    "4"
   ],
   "Series A": [
    "2",
    "6"
   ],
   "Series B": [
    "4",
    "9"
   ],
   "Growth": [
    "6",
    "15"
   ]
  },
  "rationale": ""
 },
 {
  "input_key": "DD_RAISE_MULT",
  "name": "Current Raise / Last Raise",
  "unit": "x",
  "direction": "Range",
  "ref_code": "D.1",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "1.2",
    "2.5"
   ],
   "Series A": [
    "1.5",
    "3"
   ],
   "Series B": [
    "1.5",
    "3"
   ],
   "Growth": [
    "1.2",
    "2.5"
   ]
  },
  "rationale": ""
 },
 {
  "input_key": "DD_RAISE_SHARE",
  "name": "Current Raise / Total Capital Raised to Date",
  "unit": "%",
  "direction": "Range",
  "ref_code": "D.2",
  "is_reference_only": false,
  "stage_cuts": {
   "Seed": [
    "40",
    "120"
   ],
   "Series A": [
    "35",
    "100"
   ],
   "Series B": [
    "30",
    "80"
   ],
   "Growth": [
    "25",
    "70"
   ]
  },
  "rationale": ""
 }
]
""")

ANCHORS = json.loads(r"""[
 {
  "input_key": "ANC_FDR_EDU",
  "ref_code": "A.1.a",
  "parameter_name": "Founder Education in the Field",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "Degree directly in the domain from a top-tier institution (IIT/IIM/ISB/NIT/BITS/equivalent global), or a professional qualification that is a licence to operate in this sector.",
  "good_def": "Top-tier institution but adjacent field, OR domain-specific degree from a credible non-tier-1 institution, OR a recognised domain certification earned while operating.",
  "fair_def": "Graduate in an unrelated field with no domain qualification, but no evidence the gap has hurt execution.",
  "poor_def": "No relevant formal education and no substitute credential, in a sector where technical/regulatory literacy is a precondition.",
  "evidence_required": "Institution, degree, year. Note explicitly if education is irrelevant to this sector and score on operating record instead."
 },
 {
  "input_key": "ANC_FDR_NETWORK",
  "ref_code": "A.1.b",
  "parameter_name": "Founder Connections in the Field",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Warm, named access to the top 5 buyers/suppliers in the sector, demonstrated by marquee logos already won through that network.",
  "good_def": "Credible sector network with named introductions the founder can make on request; some commercial results already traceable to it.",
  "fair_def": "Knows the sector but pipeline is built mostly on cold outbound; network has not yet produced revenue.",
  "poor_def": "No usable network; every relationship has to be built from scratch, and this is a relationship-led sector.",
  "evidence_required": "Name the logos won through the network and the specific relationship that opened each."
 },
 {
  "input_key": "ANC_PRIOR_STARTUP",
  "ref_code": "A.1.e",
  "parameter_name": "Previous Startup Experience",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "Founded a prior venture with a realised exit, or scaled one past \u20b9100 Cr revenue.",
  "good_def": "Founded a prior venture that reached institutional funding, whatever the outcome; or a documented, well-handled failure with clear lessons.",
  "fair_def": "Among the first 20 employees of a funded startup, or a senior role through a scaling phase, but never founded before.",
  "poor_def": "No startup exposure at all \u2014 career entirely in large corporates, government, or family business.",
  "evidence_required": "Venture name, years, outcome, and the founder's actual role. A failed venture handled well is not a Poor."
 },
 {
  "input_key": "ANC_CXO_PRODUCT",
  "ref_code": "A.2.a",
  "parameter_name": "CXO Seat \u2014 Product/Tech",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "Full-time leader, >12 months in seat, has already shipped/scaled a comparable product at the next stage of scale. Owns the roadmap outright.",
  "good_def": "Full-time, credible leader with relevant domain experience, but either <12 months in seat or running the function for the first time at this scale.",
  "fair_def": "Interim/fractional/agency-based cover, or founder still acting CTO/CPO while an active search runs.",
  "poor_def": "Vacant with no active search, or filled by someone plainly under-qualified, where product is the primary differentiator.",
  "evidence_required": "Name, title, start date, prior employer, product scale previously operated at."
 },
 {
  "input_key": "ANC_CXO_SALES",
  "ref_code": "A.2.b",
  "parameter_name": "CXO Seat \u2014 Sales/GTM",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "Full-time revenue leader, >12 months, has built and quota-carried a team through the next revenue band; pipeline/forecast discipline demonstrably theirs.",
  "good_def": "Full-time, credible leader with relevant channel experience, but <12 months or first time scaling a team.",
  "fair_def": "Fractional CRO/commission-only consultants, or founder still primary closer while search runs.",
  "poor_def": "Vacant with no search, or no experience of this buyer/channel, where the company must sell its way to the next round.",
  "evidence_required": "Name, title, start date, prior employer, revenue band/team size previously carried; note whether founder still closes largest accounts."
 },
 {
  "input_key": "ANC_CXO_FINANCE",
  "ref_code": "A.2.c",
  "parameter_name": "CXO Seat \u2014 Finance",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "Full-time CFO/finance head, >12 months, has taken a company through audit, diligence and a funding round; MIS closes monthly without founder involvement.",
  "good_def": "Full-time, credible leader, but <12 months or without prior transaction/diligence exposure.",
  "fair_def": "Outsourced accountant/fractional CFO on compliance only; no management reporting discipline.",
  "poor_def": "Vacant with no search, or books maintained only for statutory filing, in a company already carrying institutional capital.",
  "evidence_required": "Name, title, start date, prior employer, prior diligence exposure, date of last closed monthly MIS."
 },
 {
  "input_key": "ANC_CXO_OPS",
  "ref_code": "A.2.d",
  "parameter_name": "CXO Seat \u2014 Operations",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "Full-time, >12 months, has run this function at several times current volume; unit economics and service levels owned/instrumented by them.",
  "good_def": "Full-time, credible, relevant sector experience, but <12 months or no experience of the next volume step.",
  "fair_def": "Interim or vendor-managed cover, or founder still resolving day-to-day exceptions while search runs.",
  "poor_def": "Vacant with no search, or filled without relevant sector experience, where margin depends on operational execution.",
  "evidence_required": "Name, title, start date, prior employer, volume/throughput previously managed."
 },
 {
  "input_key": "ANC_BOARD",
  "ref_code": "A.3.a",
  "parameter_name": "Formal Board in Place",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "Meets \u2265quarterly with a genuine independent director, circulated packs and signed minutes; has demonstrably overruled/redirected the founder at least once.",
  "good_def": "Board exists and meets quarterly (investor + founder seats only) with packs and minutes but no independent voice.",
  "fair_def": "Exists on paper, meets ad hoc; no packs, no minutes, decisions taken outside the room.",
  "poor_def": "No board, or a board that has never met, in a company already carrying institutional capital.",
  "evidence_required": "Composition, meeting frequency over last 4 quarters, whether minutes exist."
 },
 {
  "input_key": "ANC_ADVISORS",
  "ref_code": "A.3.b",
  "parameter_name": "Marquee/Credible Advisors",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "\u22653 advisors of genuine sector standing, each on a documented cadence with equity/fees at stake, each traceable to a specific outcome.",
  "good_def": "\u22652 credible advisors, actually engaged and reachable, real if informal cadence.",
  "fair_def": "One engaged advisor, or several impressive names with no evidence of contact in the last six months.",
  "poor_def": "Advisor logos on a website with no arrangement, cadence, or traceable contribution.",
  "evidence_required": "Name, background, engagement terms, last interaction date, one thing they have actually delivered."
 },
 {
  "input_key": "ANC_INVESTOR_QUALITY",
  "ref_code": "A.3.c",
  "parameter_name": "Nature of Investors",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Tier-1 institutional lead with a relevant sector portfolio, demonstrated follow-on capacity, clean simple cap table with standard terms.",
  "good_def": "Credible institutional investor on cap table, standard terms, some follow-on capacity.",
  "fair_def": "Angels/family offices only, or an institution with no follow-on capacity; cap table cluttered but fixable.",
  "poor_def": "No institutional capital, or structural problems (heavy preference, unusual veto rights, dominant hostile/absent shareholder).",
  "evidence_required": "Investor names, round, stake, board rights, non-standard terms (non-standard terms cap this at Fair regardless of investor quality)."
 },
 {
  "input_key": "ANC_MOAT_IP",
  "ref_code": "C.3.a",
  "parameter_name": "Proprietary IP/Technology",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "Granted patents/registered IP central to the product, or a proprietary data asset a competitor could not rebuild, with shown willingness to enforce.",
  "good_def": "Filed applications, or genuinely proprietary tech giving >12-month lead time, core know-how held in-house.",
  "fair_def": "Accumulated know-how/process advantage a well-funded competitor could replicate in 6\u201312 months.",
  "poor_def": "Off-the-shelf/licensed stack with nothing proprietary.",
  "evidence_required": "Patent/application numbers and status, in-house vs licensed, honest replication-time estimate."
 },
 {
  "input_key": "ANC_MOAT_NETWORK",
  "ref_code": "C.3.b",
  "parameter_name": "Network Effects",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "Each additional user measurably improves the product for the rest \u2014 cohort retention improving and CAC falling as base grows.",
  "good_def": "Real cross-side benefit with marketplace liquidity established in core geography/category, not yet visible in CAC.",
  "fair_def": "Local or one-sided network effects \u2014 exist within a city/category but don't travel.",
  "poor_def": "No network effect. Growth is bought.",
  "evidence_required": "Cohort retention curves and CAC by cohort. A network-effects claim alongside rising CAC must be rejected and flagged."
 },
 {
  "input_key": "ANC_MOAT_BRAND",
  "ref_code": "C.3.c",
  "parameter_name": "Strong Brand",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Recognised category name \u2014 majority of new customers arrive organically, brand supports a price premium.",
  "good_def": "Known/trusted within core segment; a meaningful minority of demand is inbound and unpaid.",
  "fair_def": "Some recognition among existing customers but unknown outside them; growth depends on paid acquisition.",
  "poor_def": "No brand equity \u2014 product is interchangeable.",
  "evidence_required": "Organic vs paid split of new customers, evidence of price premium."
 },
 {
  "input_key": "ANC_MOAT_STICKY",
  "ref_code": "C.3.d",
  "parameter_name": "Customer Stickiness",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Embedded in customer's workflow/systems with real switching cost; installed base expands on its own.",
  "good_def": "Strong retention with contracted/habitual repeat purchase, but a determined customer could switch within a quarter.",
  "fair_def": "Retention adequate but driven by convenience or price rather than switching cost.",
  "poor_def": "Customers churn readily and buy on price; no lock-in.",
  "evidence_required": "Net revenue retention, contract length, notice periods, integration depth."
 },
 {
  "input_key": "ANC_MOAT_SCALE",
  "ref_code": "C.3.e",
  "parameter_name": "Scale/Cost Advantage",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Materially lower unit cost than largest peer, gap widens with volume \u2014 structural, not negotiated.",
  "good_def": "Modest cost advantage from procurement scale/density, defensible near-term.",
  "fair_def": "Cost parity with peers; any advantage from temporary supplier terms.",
  "poor_def": "Structurally higher unit cost than larger peers, no path to closing the gap.",
  "evidence_required": "Cost per delivered unit vs named largest peer, whether advantage is structural or negotiated."
 },
 {
  "input_key": "ANC_PRICING_POWER",
  "ref_code": "C.5",
  "parameter_name": "Pricing Power",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Raised price in last 12 months with no measurable volume/churn impact, gross margin expanded as a result.",
  "good_def": "Holds price in a discounting market, passed through input cost increases without losing customers.",
  "fair_def": "Price set by market; company follows and occasionally discounts to close.",
  "poor_def": "Competes primarily on price and is losing margin to do it.",
  "evidence_required": "Price changes taken, date, churn in the following two quarters."
 },
 {
  "input_key": "ANC_REGULATION",
  "ref_code": "C.8",
  "parameter_name": "Low Government Regulation",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "No licensing regime, no price control, no pending adverse legislation.",
  "good_def": "Light-touch registration/self-certification only, stable regime, nothing material in pipeline.",
  "fair_def": "A licensed sector with periodic compliance obligations, or a known pending change whose impact is unclear.",
  "poor_def": "Price-controlled or heavily licensed, dependent on a subsidy/approval that could be withdrawn, or facing live adverse regulatory action.",
  "evidence_required": "Licences held and renewal dates, regulator, pending legislation/litigation. *This is the only place regulation is scored \u2014 do not double-count under Sector.*"
 },
 {
  "input_key": "ANC_VAL_FLEX",
  "ref_code": "D.4",
  "parameter_name": "Low Valuation Expectation Sensitivity",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Founder has given a range rather than a number, accepts the market sets the price, has said what he'd do if the market came in below it.",
  "good_def": "Has a target valuation with a stated rationale, has shown willingness to move on evidence.",
  "fair_def": "Anchored to a specific number drawn from a peer headline, but negotiable under pressure.",
  "poor_def": "Fixed on a number with no rationale and no flexibility \u2014 has already refused a credible term sheet on price alone.",
  "evidence_required": "Valuation asked, basis given, implied multiple vs peer median, any offer already declined."
 },
 {
  "input_key": "ANC_FDR_BANKER",
  "ref_code": "D.6",
  "parameter_name": "Founder Does Not Think He Is the Banker",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Defers on process, investor list and pricing; makes no direct approaches; brings inbound interest to us.",
  "good_def": "Engaged and opinionated but respects the process/perimeter once agreed.",
  "fair_def": "Runs parallel conversations, second-guesses the investor list, needs repeated reminding on process discipline.",
  "poor_def": "Has already shopped the deal widely, insists on running the process himself.",
  "evidence_required": "Every investor already contacted directly and when. Burnt names cannot be re-approached."
 },
 {
  "input_key": "ANC_SELLER_MOTIVE",
  "ref_code": "D.8",
  "parameter_name": "Seller Motivation",
  "scoring_basis": "Anchor-scored",
  "excellent_def": "A specific, time-bound, board-approved use for the money (committed capex, a signed contract to fund, defined market entry) with >12 months runway \u2014 raising from strength.",
  "good_def": "A credible growth need with a coherent plan, enough runway that the timetable isn't set by the bank balance.",
  "fair_def": "Vague/opportunistic (\"raising because the market is open\") with no specific deployment plan.",
  "poor_def": "Distress: <6 months runway, or an undisclosed motive (founder fatigue, a shareholder dispute, a quiet search for an exit).",
  "evidence_required": "Stated reason, board approval status, current runway. Cross-check against `FIN_RUNWAY` \u2014 short runway caps this at Fair however good the story."
 },
 {
  "input_key": "ANC_PEER_NEWS",
  "ref_code": "E.7.a",
  "parameter_name": "News of Peers in the Sector",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Multiple peers have raised, expanded or been acquired in the last 6 months at healthy valuations.",
  "good_def": "Steady positive peer news flow; at least one significant raise/expansion in the last 6 months.",
  "fair_def": "Little peer news either way \u2014 the category is quiet and off the investor radar.",
  "poor_def": "Peer news is materially negative: shutdowns, down rounds, layoffs, governance failures, fraud allegations.",
  "evidence_required": "List events with dates and sources. Count distinct events, not distinct articles."
 },
 {
  "input_key": "ANC_SECTOR_NEWS",
  "ref_code": "E.7.b",
  "parameter_name": "News of the Sector",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Sector coverage strongly positive and forward-looking \u2014 policy support, capital inflow, demand tailwinds from credible outlets.",
  "good_def": "Balanced-to-positive coverage; structural story intact and generally accepted.",
  "fair_def": "Mixed coverage with unresolved questions about the model or demand outlook.",
  "poor_def": "Persistently negative coverage \u2014 a structural challenge, demand collapse, or loss of investor belief in the category.",
  "evidence_required": "Positive-to-negative event ratio over 6 months with sources."
 },
 {
  "input_key": "ANC_IB_SECTOR_KNOW",
  "ref_code": "G.1",
  "parameter_name": "Sector Knowledge, Relationships",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "Closed multiple deals in this sector, warm relationships with investors actively writing cheques in it, can name likely buyers unprompted.",
  "good_def": "Closed at least one deal in the sector, know the main active investors, though some introductions would be cold.",
  "fair_def": "Understand the sector but haven't transacted in it; investor list would be built largely from scratch.",
  "poor_def": "New sector, no relationships, no reference transactions.",
  "evidence_required": "Deals closed with dates, count of warm investor relationships active in it."
 },
 {
  "input_key": "ANC_IB_CAPACITY",
  "ref_code": "G.4",
  "parameter_name": "Capacity Available",
  "scoring_basis": "Numeric-primary",
  "excellent_def": "A senior owner can run this end to end with clear headroom, supporting bench in place for the whole timetable.",
  "good_def": "Senior coverage available with a manageable load; execution support identified but not ring-fenced.",
  "fair_def": "Senior bandwidth already stretched; taking this on means something else gets less attention.",
  "poor_def": "No genuine capacity \u2014 under-served from day one, or depends on a hire not yet made.",
  "evidence_required": "Live mandates per senior banker, who specifically would run this."
 }
]
""")
