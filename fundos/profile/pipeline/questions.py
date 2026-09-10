"""The Source 1 research question bank: 10 batches x 10 questions.

Questions are drawn from the Silk Format question set and extended to cover
every field of the 17-section profile schema. Batches are topically coherent so
each search-grounded call can ground on one subject area — grouping "Funding
History & Investors" together, say, rather than scattering funding questions
across unrelated batches, lets one call chase a coherent line of research
instead of jumping topics mid-call.

The grouping also determines fan-out and failure isolation in
:mod:`fundos.profile.pipeline.source1_research`: one batch is one LLM call, so
a batch succeeding or failing is the unit of partial progress the dossier
reports. ``covers`` documents which profile sections a failed batch leaves thin.

Where the questions live
------------------------
The authoritative bank is the admin table (``platformcfg.ResearchQuestionBatch``
/ ``ResearchQuestion``), seeded from :data:`SHIPPED_BATCHES` below. An
administrator can retopic a batch, add or reword a question, or deactivate
batches to cut cost — all without a deploy.

:data:`SHIPPED_BATCHES` remains as the seed *and* as the runtime fallback: an
empty or unreachable table falls back to it rather than running a company
profile with no research at all. That mirrors how ``PromptTemplate`` falls back
to ``default_prompts`` — an unseeded install must still work.

Placeholders ``{company_name}`` and ``{website}`` are substituted per run by
:func:`build_batches`, kept as ``str.format`` placeholders in the stored text so
the same question is reusable across every company.
"""
import logging
from collections import namedtuple

logger = logging.getLogger("fundos.profile")

QuestionBatch = namedtuple("QuestionBatch",
                           ("index", "topic", "covers", "questions"))
"""One rendered batch, ready to send.

index    -- 1-based position, used in logs and activity events
           ("research batch 3/10 finished").
topic    -- short label shown to the model and in progress narration.
covers   -- which profile sections this batch's answers feed; both a hint to
           the model and documentation for whoever reads a partial dossier.
questions -- the fully-substituted question strings.
"""


# fmt: off
SHIPPED_BATCHES = [
    (
        "company_basics",
        "Company Basics & Identity",
        "8.1 Company Overview",
        [
            "What is the company overview of {company_name} ({website})? Give a detailed description of what the business does.",
            "What is the full legal/registered name of {company_name}, and what brand names does it trade under?",
            "In which year was {company_name} founded, and what is its founding history?",
            "What is the macro sector and specific sub-sector of {company_name}? Use standard industry taxonomy.",
            "Where is {company_name} headquartered? Give the city and country, citing physical addresses or contact numbers found on {website}.",
            "Which countries and offices does {company_name} operate from beyond its headquarters?",
            "What is the primary reporting currency of {company_name}, and what denomination (thousands, millions, billions, lakhs, crores) does it report financials in?",
            "What is the financial year start month and fiscal calendar convention used by {company_name}?",
            "What is the employee headcount of {company_name}, and which band does it fall into (1-10, 11-50, 51-200, 201-500, 501-1000, 1000+)?",
            "What is the official LinkedIn company page URL of {company_name}, and what is its current funding status (Bootstrapped, Angel Funded, Seed Funded, VC Funded, Private Equity Funded, Corporate Backed, Public, Acquired, Subsidiary/Group Owned)?",
        ],
    ),
    (
        "founders_leadership",
        "Founders & Leadership",
        "8.2 Founders & Key People",
        [
            "Who are the founders and co-founders of {company_name} ({website})? Give full names and current titles.",
            "What are the verified LinkedIn profile URLs of each founder of {company_name}? Give the exact URL for each named person.",
            "What is the professional background of each founder of {company_name}: previous companies, education, years of experience and notable achievements?",
            "Which founders of {company_name} are still working on the company full-time, and have any founders departed? Give dates for any departures.",
            "Who are the key executives of {company_name} - CEO, CFO, COO, CTO, CPO, Head of Sales, Head of Operations? Give names, titles and appointment dates.",
            "What are the verified LinkedIn profile URLs of the key executives and C-suite of {company_name}?",
            "What is the professional background of each key executive of {company_name}: prior employers, education and relevant experience?",
            "Have there been any recent leadership changes, senior hires or executive departures at {company_name} in the last 24 months?",
            "Who sits on the board of directors of {company_name}, and which investors hold board seats or board observer rights?",
            "How large is the senior leadership team of {company_name}, and how would you assess the depth and relevant domain experience of the management team?",
        ],
    ),
    (
        "products_business_model",
        "Products, Services & Business Model",
        "8.3 Products & Services / 8.6 Business Model",
        [
            "What are the products and services offered by {company_name} ({website})? Describe each offering, its category and what it does.",
            "What are the product lines, service tiers or packages that {company_name} sells, and what differentiates each tier?",
            "What is the core business of {company_name}, and what specific problem does it solve for its customers?",
            "What business model types does {company_name} operate (e.g. B2B SaaS, B2B2C, marketplace, D2C, licensing, services)?",
            "What is the value proposition of {company_name} as stated by the company and as described by third parties?",
            "What is the pricing model of {company_name}? Give specific price points, tiers or commission rates where disclosed.",
            "What is the delivery model of {company_name} - how does the product or service actually reach the customer?",
            "What is the sales model of {company_name} (self-serve, inside sales, enterprise field sales, channel/partner-led)?",
            "What distribution channels and partnerships does {company_name} use to reach customers?",
            "What technology platform, proprietary technology or intellectual property (patents, trademarks) underpins the products of {company_name}?",
        ],
    ),
    (
        "customers_differentiators",
        "Customers, Markets & Differentiators",
        "8.4 Customers & Markets / 8.5 Competitive Advantages",
        [
            "What are the key geographic markets of {company_name} ({website})? List the cities, regions and countries served.",
            "Who are the customers of {company_name}? Describe the customer segments and types (enterprise, SMB, consumers, government).",
            "Which named clients, logos or reference customers does {company_name} publicly disclose?",
            "Which industry verticals does {company_name} serve, and which vertical contributes the most revenue?",
            "What is the customer concentration of {company_name} - what share of revenue comes from its largest customers?",
            "What are the key differentiators and competitive advantages of {company_name} versus its competitors?",
            "What makes the offering of {company_name} defensible - network effects, switching costs, data advantage, regulatory licences, exclusive partnerships?",
            "What awards, certifications, regulatory licences or accreditations does {company_name} hold?",
            "What do customer reviews and third-party ratings say about {company_name}, and what are the recurring criticisms?",
            "How does {company_name} position itself in the market, and what is its stated target customer profile?",
        ],
    ),
    (
        "metrics_kpis",
        "Operating Metrics & KPIs",
        "8.8 Company Metrics",
        [
            "What are the key performance indicators (KPIs) of {company_name} ({website})? Give the metric, value, unit and the date each was reported.",
            "What is the ARR or MRR of {company_name}, and how has it grown over time?",
            "What is the number of active customers, users or subscribers of {company_name}, and as of what date?",
            "What are the retention, churn and net revenue retention figures reported by {company_name}?",
            "What are the unit economics of {company_name} - customer acquisition cost, lifetime value, payback period, contribution margin?",
            "What are the gross transaction value, order volume or throughput metrics of {company_name}?",
            "What is the gross margin of {company_name}, and how has it trended?",
            "What operational scale metrics does {company_name} report (locations, network size, partners, transactions per month, capacity)?",
            "What growth rates has {company_name} publicly claimed, and over what period were those claims made?",
            "What is the burn rate, runway or path to profitability disclosed by or reported about {company_name}?",
        ],
    ),
    (
        "funding_investors",
        "Funding History & Investors",
        "8.10 Funding History / 8.13 Investors & Cap Table",
        [
            "What is the complete funding history of {company_name} ({website})? For each round give the date, round name and amount raised.",
            "What is the total combined funding raised to date by {company_name}, in USD millions?",
            "What is the latest funding round of {company_name} - date, round name, amount, and the investors who participated?",
            "What were the pre-money and post-money valuations of {company_name} at each funding round?",
            "What is the current valuation of {company_name}, and on what basis or reported source is that figure derived?",
            "Who are all the investors in {company_name}? List institutional VCs, PE firms, corporate/strategic investors and notable angels.",
            "Who were the lead investors in each round of {company_name}, and which investors are strategic rather than purely financial?",
            "What is the shareholding or cap table structure of {company_name} - what ownership percentages are held by founders, ESOP, and investor categories?",
            "What dilution occurred at each funding round of {company_name}, and what stake does each major investor hold?",
            "Has {company_name} raised any debt, venture debt, convertible notes or grants, and are there any secondary transactions, buybacks or exits by early investors?",
        ],
    ),
    (
        "financial_performance",
        "Financial Performance",
        "8.9 Financial Summary",
        [
            "What is the revenue of {company_name} ({website}) for each of the past five financial years? Give the figure, currency and financial year label.",
            "What is the year-on-year revenue growth of {company_name} for each of the past five years?",
            "What is the EBITDA of {company_name} for each of the past five financial years, and what is the EBITDA margin?",
            "What is the profit after tax (PAT) or net loss of {company_name} for each of the past five financial years?",
            "What is the operating expense breakdown of {company_name} - employee benefit expense, marketing spend, and other major cost lines?",
            "What do the regulatory filings, annual reports or MCA/Companies House records of {company_name} disclose about its financials?",
            "What revenue streams does {company_name} have, and what percentage share of total revenue does each stream contribute?",
            "What is the balance sheet position of {company_name} - cash on hand, total assets, total debt and net worth?",
            "What EV/Revenue and EV/EBITDA multiples have been applied to {company_name} or implied by its funding rounds?",
            "What are the key financial observations about {company_name} regarding revenue visibility, operating leverage, profitability trajectory and cash efficiency?",
        ],
    ),
    (
        "competitive_landscape",
        "Competitive Landscape",
        "8.11 Competitors & Market Positioning",
        [
            "Who are the direct competitors and closest alternatives to {company_name} ({website})? Name both local and international players with the same business model.",
            "For each competitor of {company_name}, what is their latest reported revenue, the financial year it relates to, and their currency?",
            "For each competitor of {company_name}, what is their total funding raised and their latest known valuation?",
            "For each competitor of {company_name}, what were their most recent funding round sizes, dates and lead investors?",
            "How does {company_name} compare in scale to each competitor - is each competitor larger, comparable or smaller by revenue and funding?",
            "What are the key differentiators, strengths and weaknesses of each competitor of {company_name}?",
            "What is the business model and market positioning of each competitor of {company_name}?",
            "What is the estimated market share of {company_name} and of each of its main competitors?",
            "What recent M&A activity, acquisitions or consolidation has occurred among the competitors of {company_name}? Give deal values and implied multiples.",
            "What EV/Revenue and EV/EBITDA multiples have been observed in recent transactions involving competitors of {company_name}?",
        ],
    ),
    (
        "recent_news",
        "Recent News & Developments",
        "8.12 Recent News & Media",
        [
            "What are the most significant news items about {company_name} ({website}) in the last 18 months? For each, give the date, headline, a summary, the publication and the full article URL.",
            "What funding announcements has {company_name} made recently, and which outlets covered them? Include article URLs.",
            "What expansion plans, new market entries or new office openings has {company_name} announced?",
            "What new product launches or major feature releases has {company_name} announced recently?",
            "What strategic partnerships, joint ventures or major client wins has {company_name} announced?",
            "What M&A activity has {company_name} been involved in - as acquirer or as a target - and what were the deal terms?",
            "What leadership changes at {company_name} have been reported in the press, with dates and sources?",
            "What regulatory, legal or compliance issues, litigation or investigations involving {company_name} have been reported?",
            "What negative press, controversies, layoffs or restructuring involving {company_name} has been reported?",
            "What has the management of {company_name} said publicly in recent interviews about strategy, targets, fundraising plans or an IPO?",
        ],
    ),
    (
        "story_industry_thesis",
        "Company Story, Industry Research & Investment Thesis",
        "8.14 Company Story / 8.15 Industry Research / 8.16 Investment Thesis",
        [
            "What is the origin and founding story of {company_name} ({website})? What insight or problem led the founders to start it?",
            "How has the brand and positioning of {company_name} evolved since founding, including any rebrands or pivots?",
            "What is the unique selling proposition (USP) of {company_name} in a single clear statement?",
            "What are the major corporate milestones of {company_name}? Give the date, title and description of each.",
            "How has the industry that {company_name} operates in evolved over the past five to ten years?",
            "What is the TAM, SAM and SOM for the market {company_name} operates in? Give figures with currency, year and the source of each estimate.",
            "What are the key performance and technology trends shaping the industry of {company_name}?",
            "What regulatory developments affect the industry of {company_name}, and what compliance obligations apply?",
            "What is the investment case for {company_name} - why is this an attractive opportunity, considering market size, growth, moat and team?",
            "What are the key risks, red flags and diligence concerns for an investor or acquirer evaluating {company_name}?",
        ],
    ),
]
# fmt: on

SHIPPED_QUESTION_COUNT = sum(len(questions) for _, _, _, questions in SHIPPED_BATCHES)
"""100, given 10 batches of 10. Surfaced in the research prompt and in progress
narration so operators and the model see the same, single-sourced total."""


def _render(index, topic, covers, questions, company_name, website):
    """Substitute the company placeholders into one batch.

    A question with a stray brace (an admin typing ``{`` in the text) would
    otherwise raise ``KeyError``/``IndexError`` from ``str.format`` and take the
    whole run down at build time. Substitution failure falls back to the raw
    text: a question that reads slightly wrong is worth far more than a run
    that never starts.
    """
    rendered = []
    for question in questions:
        try:
            rendered.append(question.format(company_name=company_name,
                                            website=website))
        except (KeyError, IndexError, ValueError):
            logger.warning(
                "PIPELINE: research question has an unsubstitutable "
                "placeholder and was sent as written: %.80s", question)
            rendered.append(question)
    return QuestionBatch(index=index, topic=topic, covers=covers,
                         questions=rendered)


def _from_database():
    """The active bank as ``(topic, covers, [question, …])`` tuples, or None.

    Returns None — not an empty list — when the table is unusable or unseeded,
    so the caller can tell "the admin has deactivated everything" (an empty
    list, which is a deliberate instruction to do no research) from "there is
    no bank" (None, which means fall back to the shipped set).
    """
    try:
        from fundos.platformcfg.models import ResearchQuestionBatch
    except Exception:
        return None
    try:
        batches = list(ResearchQuestionBatch.objects.filter(is_active=True)
                       .order_by("sort_order", "code")
                       .prefetch_related("questions"))
    except Exception as exc:
        # An unmigrated database is the expected case on first deploy.
        logger.info("PIPELINE: research question bank unavailable (%s) — "
                    "using the shipped question set.", exc)
        return None
    if not batches:
        return None

    out = []
    for batch in batches:
        questions = [q.text for q in
                     sorted((q for q in batch.questions.all() if q.is_active),
                            key=lambda q: (q.sort_order, str(q.id)))]
        if not questions:
            # A batch with no active questions is one LLM call that can only
            # ask nothing. Skip it rather than paying for it.
            logger.info("PIPELINE: research batch %r has no active questions "
                        "— skipped.", batch.code)
            continue
        out.append((batch.topic, batch.covers, questions))
    return out


def build_batches(company_name, website):
    """The batches for one run, with company placeholders substituted.

    Reads the admin bank; falls back to :data:`SHIPPED_BATCHES` when there is
    none. Called once per run, so each run gets its own fully-rendered set even
    though the stored templates are shared.

    :returns: Batches in their configured order, indexed from 1. May be empty
        if an administrator has deactivated every batch — the caller treats
        that as "do no web research", not as an error.
    """
    configured = _from_database()
    if configured is None:
        source = [(topic, covers, questions)
                  for _, topic, covers, questions in SHIPPED_BATCHES]
        logger.info("PIPELINE: using the shipped research bank (%d batches, "
                    "%d questions).", len(source), SHIPPED_QUESTION_COUNT)
    else:
        source = configured

    return [_render(offset + 1, topic, covers, questions,
                    company_name, website)
            for offset, (topic, covers, questions) in enumerate(source)]


def total_questions(batches):
    """Questions across the given batches — the denominator quoted to the model
    and in progress narration. Computed from what will actually be asked, not
    from the shipped constant, so a trimmed bank reports its own real total."""
    return sum(len(batch.questions) for batch in batches)
