#!/usr/bin/env python3
"""
Find out what actually controls whether Gemini issues a web search.

The company profile is only as good as its evidence, and the research stage
(`profile_research_batch`) is worthless if the model answers from recollection
instead of searching. This script measures that directly: it runs the same
prompt eight times across three binary variables and reports which one decides
whether a search happens. It talks to the Gemini REST API directly -- no
Django, no config layer, no FundOS imports -- so the answer is a fact about the
provider rather than about our configuration.

Run it when research batches come back with `searches=0`, or after changing the
model or system prompt on the `profile.research` config profile.

    export GEMINI_API_KEY=...
    python tools/diagnose_gemini_search.py
    python tools/diagnose_gemini_search.py --model gemini-2.5-flash \
        --context-file site.txt

Variables tested
----------------
    schema      responseSchema present vs absent      (hypothesis H1)
    thinking    thinkingBudget 0 vs 2048              (hypothesis H2)
    position    directive before vs after the context (hypothesis H3)

Reads groundingMetadata.webSearchQueries from each response. That field is
the list of queries actually ISSUED -- absent means no search ran. This is
the same signal adapter._count_searches() uses, so results map directly.
"""

import argparse
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Mirrors adapter._search_directive(expect_search=True).
DIRECTIVE = """SEARCH FIRST - THIS IS NOT OPTIONAL.
The context you have been given is a starting point, not the answer. Several
of the parameters requested - market size, growth rates, peer funding history,
investor names, headcount - are NOT in the supplied material and cannot be. If
you answer them without searching you are answering from recollection, which is
the one failure this task cannot tolerate.
Run web searches BEFORE you answer. Use up to 6 of them; plan the queries so
each one covers several parameters. Answering entirely from the supplied
context is a FAILED response, even if every field is populated.
When the budget is spent, leave what remains unresolved in `missing` rather
than filling it from memory."""

TASK = (
    "Extract a company profile for the company described in the context. "
    "Populate total funding raised, the names of its institutional investors, "
    "the date and size of its most recent round, current headcount, and the "
    "addressable market size for its sector. None of these are stated in the "
    "supplied context."
)

# Deliberately small: the point is to detect whether a schema suppresses the
# search tool, not to reproduce the full 206-field extract.
SCHEMA = {
    "type": "object",
    "properties": {
        "totalFundingUsd": {"type": "string"},
        "investors": {"type": "array", "items": {"type": "string"}},
        "lastRoundDate": {"type": "string"},
        "lastRoundSizeUsd": {"type": "string"},
        "headcount": {"type": "string"},
        "marketSizeUsd": {"type": "string"},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["totalFundingUsd", "investors", "missing"],
}

FALLBACK_CONTEXT = (
    "ClearDekho is an Indian eyewear retail chain operating franchise-model "
    "optical stores across tier-2 and tier-3 cities. It sells prescription "
    "glasses, sunglasses and contact lenses at accessible price points, and "
    "offers in-store eye testing. " * 60
)


# H4 — the EXTRACTION FRAMING hypothesis.
#
# Both H1 and H4 are the reason the profile pipeline is built the way it is, so
# they are worth stating rather than just testing.
#
# The 19 Aug grid settled H1 (responseSchema suppresses google_search: 0/4 vs
# 4/4) but left one production result unexplained. The single retrieval call of
# the previous design sent NO responseSchema — its caps_sent read
# "thinking,web_search" with no structured_output — and still searched zero
# times, while the per-section calls, with identical capabilities, searched
# 2 and then 6 times. Context size is ruled out: the run-2 retrieval call had a
# SMALLER context (3,299 chars) than the section calls that searched (5,945).
#
# The remaining difference was the system prompt: 592 chars against 60-79. The
# hypothesis is that a "populate every field of this structure from the
# supplied material" instruction reads to the model as an EXTRACTION task, and
# an extraction task is one you complete from the material you were given — so
# the model never considers leaving it, whatever the search directive says.
#
# If H4 holds, dropping the schema alone would not have restored search on the
# call that most needed it. Splitting retrieval from structuring does: the
# research batches now ask open questions with no structure to populate, and
# synthesis — which does have a schema — never needs to search. Keep this grid
# runnable, because that reasoning is a prediction until a live run confirms
# `searches > 0`.
EXTRACTION_SYSTEM = (
    "You are a data extraction engine. Read the supplied material and "
    "populate every field of the requested structure from it. Extract only "
    "what the material supports; leave a field empty rather than inferring "
    "it. Return the populated structure and nothing else."
)

RESEARCH_SYSTEM = "You are a company research analyst."


def build_body(context, *, schema, thinking, directive_after,
               extraction_framing=False):
    """Assemble one generateContent payload.

    directive_after=True places the search instruction immediately before the
    task and AFTER the context, instead of at the very top where the adapter
    currently puts it (adapter.py:1081 prepends context_block to the prompt,
    leaving the system directive ~24k chars from the instruction).
    """
    base = EXTRACTION_SYSTEM if extraction_framing else RESEARCH_SYSTEM
    if directive_after:
        system = base
        prompt = f"{context}\n\n---\n{DIRECTIVE}\n\n{TASK}"
    else:
        system = f"{base}\n\n{DIRECTIVE}"
        prompt = f"{context}\n\n---\n{TASK}"

    gen = {"maxOutputTokens": 4096 + (thinking or 0), "temperature": 0.2}
    gen["thinkingConfig"] = {"thinkingBudget": thinking}
    if schema:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = SCHEMA

    return {
        "contents": [{"parts": [{"text": f"{system}\n\n{prompt}"}]}],
        "generationConfig": gen,
        "tools": [{"google_search": {}}],
    }


def call(api_key, model, body, timeout):
    req = urllib.request.Request(
        API.format(model=model),
        data=json.dumps(body).encode(),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        return None, f"HTTP {e.code}: {detail}"
    except Exception as e:                       # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def read_result(data):
    """(search_count, thinking_tokens, completion_tokens, finish_reason).

    search_count is None when groundingMetadata carries no webSearchQueries --
    the same 'unknown vs genuine zero' distinction adapter._count_searches()
    preserves. None here means the tool was never exercised.
    """
    try:
        cand = (data.get("candidates") or [{}])[0]
    except (AttributeError, IndexError):
        cand = {}
    gm = cand.get("groundingMetadata") or {}
    queries = gm.get("webSearchQueries")
    meta = data.get("usageMetadata") or {}
    return (
        len(queries) if queries is not None else None,
        int(meta.get("thoughtsTokenCount", 0) or 0),
        int(meta.get("candidatesTokenCount", 0) or 0),
        cand.get("finishReason", ""),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini-3.5-flash")
    ap.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY"))
    ap.add_argument("--context-file",
                    help="Real scraped site text. Defaults to filler sized to "
                         "match the 24,081-char context in the 17 Aug log.")
    ap.add_argument("--context-chars", type=int, default=24081)
    ap.add_argument("--thinking-budget", type=int, default=2048)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--no-framing", action="store_true",
                    help="Skip H4 and run the original 8-cell grid. The full "
                         "grid is 16 calls; use this if quota is tight.")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="Seconds between calls, to stay under rate limits.")
    args = ap.parse_args()

    if not args.api_key:
        sys.exit("Set GEMINI_API_KEY or pass --api-key")

    if args.context_file:
        context = open(args.context_file, encoding="utf-8").read()
    else:
        context = FALLBACK_CONTEXT
    context = context[:args.context_chars]

    print(f"model={args.model}  context={len(context):,} chars  "
          f"thinking_on={args.thinking_budget}\n")
    header = f"{'schema':<8} {'thinking':<9} {'directive':<11} " \
             f"{'framing':<12} {'searches':<9} {'think_tok':<10} " \
             f"{'out_tok':<8} finish"
    print(header)
    print("-" * len(header))

    results = []
    framings = [False] if args.no_framing else [False, True]
    combos = itertools.product([True, False], [0, args.thinking_budget],
                               [False, True], framings)
    for schema, thinking, after, extraction in combos:
        body = build_body(context, schema=schema, thinking=thinking,
                          directive_after=after,
                          extraction_framing=extraction)
        pos = "after" if after else "before"
        frame = "extraction" if extraction else "research"
        data, err = call(args.api_key, args.model, body, args.timeout)
        if err:
            print(f"{str(schema):<8} {thinking:<9} {pos:<11} "
                  f"{frame:<12} ERROR  {err}")
            results.append((schema, thinking, after, extraction, None, True))
            time.sleep(args.delay)
            continue

        n, think_tok, out_tok, finish = read_result(data)
        shown = "unknown" if n is None else str(n)
        print(f"{str(schema):<8} {thinking:<9} {pos:<11} {frame:<12} "
              f"{shown:<9} {think_tok:<10} {out_tok:<8} {finish}")
        results.append((schema, thinking, after, extraction, n, False))
        time.sleep(args.delay)

    verdict(results)


def verdict(results):
    """Report which variable, if any, controls searching."""
    ok = [r for r in results if not r[5]]
    if not ok:
        print("\nEvery call errored. Nothing can be concluded.")
        return

    def searched(r):
        return bool(r[4])

    print("\n" + "=" * 60)
    if not any(searched(r) for r in ok):
        print("NO combination searched.")
        print("None of H1/H2/H3/H4 explains it. Look upstream instead:")
        print("  - is google_search enabled on this API key's project?")
        print("  - does this model string support Search grounding at all?")
        print("  - try the same call with NO tools but a plain question the")
        print("    model cannot answer, to confirm grounding works on the key.")
        return
    if all(searched(r) for r in ok):
        print("EVERY combination searched.")
        print("The provider is not the constraint. The bug is in FundOS:")
        print("  - confirm web_search survives capabilities_payload()")
        print("  - confirm search_breaker_tripped() is not returning True")
        print("  - confirm the resolved profile is the one you think it is")
        return

    for idx, name, labels in ((0, "SCHEMA (H1)", ("with", "without")),
                              (1, "THINKING (H2)", ("off", "on")),
                              (2, "POSITION (H3)", ("before", "after")),
                              (3, "FRAMING (H4)", ("research", "extraction"))):
        groups = {}
        for r in ok:
            groups.setdefault(bool(r[idx]) if idx != 1 else bool(r[1]),
                              []).append(searched(r))
        if len(groups) < 2:
            continue
        rates = {k: sum(v) / len(v) for k, v in groups.items()}
        lo, hi = min(rates.values()), max(rates.values())
        if hi - lo >= 0.5:
            print(f"{name} is decisive: "
                  f"{labels[0]}={rates.get(True, 0):.0%} searched, "
                  f"{labels[1]}={rates.get(False, 0):.0%} searched")
        elif hi - lo > 0:
            print(f"{name} has partial influence "
                  f"({lo:.0%} vs {hi:.0%}) -- likely compounding.")
        else:
            print(f"{name} has no effect.")
    print("=" * 60)
    print("Apply the decisive variable to the `profile.research` config "
          "profile (admin -> LLM -> Config profiles), re-run one real "
          "generation, and confirm searches>0 in "
          "llm.call.profile_research_batch.")


if __name__ == "__main__":
    main()
