"""The Fundraising assessment hierarchy — the shape the payload is read in.

ONE statement of the tree the product presents: six parents (A-F), their
children, and the terminal parameters that carry the assessment. Nothing here
extracts, scores, bands or weights anything. It is a projection of config that
already exists, and every number in the payload still comes from the scoring
path — this module only says where each one belongs.

Three rules the rest of the code depends on:

1. THE TERMINAL NODE IS THE PARAMETER. A terminal sits at the grandchild level
   under A.1/A.2/A.3/C.3/E.7 and at the child level everywhere else. It is the
   only node that carries value, score, band, reasoning, evidence, citations,
   confidence, rubric/anchor, thresholds, trace and diligence findings.

2. PARENTS AND CHILDREN ARE DISPLAY ONLY. They carry ref, name, weight and a
   rolled-up score/band. No evidence hangs off them.

3. INPUTS ARE NOT NODES. Several config inputs may feed one terminal — a
   rubric row plus its anchor, TAM plus TAM CAGR — and the `(ref)` cross-check
   rows feed one without ever being scored. All of them stay seeded, stay
   extracted and stay available to the scoring engine; none of them appears as
   a node. They are carried INSIDE their terminal, under `inputs`.

Category G (Mandate Context) is deliberately absent. Its config rows, its
rubrics and its extraction are untouched and still live in the model — G rates
the advisor's own mandate fit, not the company, and it has no place in the
company's fundraising hierarchy or in its roll-up.
"""

# ---------------------------------------------------------------------------
# The tree. (ref, name, [children]) — a node with no children is terminal.
#
# Names are the canonical display strings and are matched exactly by the
# contract tests; they intentionally override the labels seeded in
# ConfigWeight, which the workbook spells its own way ("Gross Margin (CM1)",
# "Raise vs. Last Round", "News Flow & Sentiment").
# ---------------------------------------------------------------------------
HIERARCHY = [
    ("A", "Team", [
        ("A.1", "Founder Profile", [
            ("A.1.a", "Founder Education", []),
            ("A.1.b", "Founder Industry Network", []),
            ("A.1.c", "Co-Founder Relationship", []),
            ("A.1.d", "Founder Industry Experience", []),
            ("A.1.e", "Prior Startup Experience", []),
        ]),
        ("A.2", "Leadership Team", [
            ("A.2.a", "Product & Technology Leadership", []),
            ("A.2.b", "Sales & GTM Leadership", []),
            ("A.2.c", "Finance Leadership", []),
            ("A.2.d", "Operations Leadership", []),
        ]),
        ("A.3", "Board, Advisors & Investors", [
            ("A.3.a", "Formal Board", []),
            ("A.3.b", "Advisor Quality", []),
            ("A.3.c", "Investor Quality", []),
        ]),
    ]),
    ("B", "Financials", [
        ("B.1", "Revenue Scale", []),
        ("B.2", "Revenue Growth", []),
        ("B.3", "Gross Margin CM1", []),
        ("B.4", "Contribution Margin CM2", []),
        ("B.5", "EBITDA Margin", []),
        ("B.6", "PAT Margin", []),
        ("B.7", "Cash Runway", []),
        ("B.8", "Working Capital Cycle", []),
        ("B.9", "Debt Level", []),
    ]),
    ("C", "Business Quality", [
        ("C.1", "Company Age", []),
        ("C.2", "Revenue Predictability", []),
        ("C.3", "Competitive Moat", [
            ("C.3.a", "Proprietary IP & Technology", []),
            ("C.3.b", "Network Effects", []),
            ("C.3.c", "Brand Strength", []),
            ("C.3.d", "Customer Stickiness", []),
            ("C.3.e", "Scale & Cost Advantage", []),
        ]),
        ("C.4", "Scalability", []),
        ("C.5", "Pricing Power", []),
        ("C.6", "Client Concentration", []),
        ("C.7", "Supplier Concentration", []),
        ("C.8", "Government Regulation", []),
    ]),
    ("D", "Deal Dynamics", [
        ("D.1", "Raise vs Last Round", []),
        ("D.2", "Raise vs Total Raised", []),
        ("D.3", "Existing Investor Contribution", []),
        ("D.4", "Valuation Expectations", []),
        ("D.5", "Founder Dilution", []),
        ("D.6", "Founder Process Discipline", []),
        ("D.7", "Use of Proceeds", []),
        ("D.8", "Seller Motivation", []),
    ]),
    ("E", "Sector Attractiveness", [
        ("E.1", "Market Size & Growth", []),
        ("E.2", "Deal Velocity", []),
        ("E.3", "Deal Ticket Size", []),
        ("E.4", "Peer Tenor", []),
        ("E.5", "Peer Raise Recency", []),
        ("E.6", "Active Investors", []),
        ("E.7", "News Flow", [
            ("E.7.a", "Peer News Flow", []),
            ("E.7.b", "Sector News Flow", []),
        ]),
    ]),
    ("F", "Sub-Sector Attractiveness", [
        ("F.1", "Market Size & Growth", []),
        ("F.2", "Deal Velocity", []),
        ("F.3", "Deal Ticket Size", []),
        ("F.4", "Peer Tenor", []),
        ("F.5", "Peer Raise Recency", []),
        ("F.6", "Active Investors", []),
    ]),
]

# The categories this hierarchy admits. G is not one of them, and this set is
# what filters it out of the payload, the roll-up and the findings.
CATEGORY_CODES = tuple(code for code, _n, _c in HIERARCHY)

# Every node's canonical name, by ref.
NAMES = {}
# Every terminal ref, in document order.
TERMINAL_REFS = []
# ref -> "category" | "child" | "grandchild".
LEVELS = {}
# terminal ref -> the child/category it hangs under.
PARENT_OF = {}


def _index():
    for cat, cat_name, children in HIERARCHY:
        NAMES[cat] = cat_name
        LEVELS[cat] = "category"
        for child, child_name, grandchildren in children:
            NAMES[child] = child_name
            LEVELS[child] = "child"
            PARENT_OF[child] = cat
            if not grandchildren:
                TERMINAL_REFS.append(child)
                continue
            for gc, gc_name, _none in grandchildren:
                NAMES[gc] = gc_name
                LEVELS[gc] = "grandchild"
                PARENT_OF[gc] = child
                TERMINAL_REFS.append(gc)


_index()

TERMINAL_REF_SET = frozenset(TERMINAL_REFS)


def is_fundraising_category(code):
    """True for A-F. The single place G is turned away."""
    return str(code or "").strip().upper()[:1] in CATEGORY_CODES


def terminal_ref_for(ref_code):
    """The terminal node a config ref belongs to, or "" if it belongs to none.

    Config ref codes arrive already normalised by the seeder — "C.6 (ref)" is
    stored as "C.6", "E.1 / F.1" as "E.1" — so a cross-check row names the
    same terminal as the row it cross-checks, which is exactly where it should
    be carried. A ref naming a non-terminal node (TEAM_CXO_SEATS sits on the
    child "A.2") has no terminal of its own and is held internally instead of
    being promoted to a node it is not.
    """
    ref = str(ref_code or "").strip()
    return ref if ref in TERMINAL_REF_SET else ""
