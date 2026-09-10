"""
Per-run LLM accounting ledger.

WHY THIS EXISTS
---------------
Answering "what did that generation cost?" used to require parsing eleven
individual `llm.call.*` trace lines out of a 135-line log and adding them up
by hand -- and the answer came out 39% low, because `thinking_tokens` reached
the trace line but not `LLMCallLog`. Meanwhile `llmCalls` was DERIVED from
section bookkeeping in a completely different module and reported 9 while 11
calls had actually been dispatched.

Three numbers for one quantity, none of them measured at the place that knew.

The adapter is the only component that knows what it dispatched, so it keeps
the book. Every call that is BILLED -- success, fallback, mocked, or hard
failure -- appends exactly one entry, at the same choke point that writes
`LLMCallLog`, so the ledger and the database cannot disagree.

WHY THREAD-LOCAL
----------------
Celery workers run concurrent generations in one process. A module-level list
would blend two tenants' runs together and produce a cost figure that belongs
to neither. Thread-local storage keeps one book per run.

WHY THREAD-LOCAL IS NOT ENOUGH ANY MORE
---------------------------------------
The company-profile pipeline fans its research batches out across a thread
pool, so a single run now dispatches from several threads at once. Under plain
thread-local storage each worker would start a fresh, empty book, append its
calls there, and let it die with the thread -- and the run would report one
call (the synthesis) out of eleven. That is the same class of defect this
module was written to end: a cost figure measured somewhere other than where
the dispatch happened.

A contextvar is NOT the fix. `contextvars.copy_context()` copies the mapping,
so a worker would still need the parent's list object by reference to append
into it -- and a bare contextvar default would reintroduce the cross-run
blending thread-local storage exists to prevent.

Instead the parent hands its book to each worker explicitly:
:func:`book` reads the current one, :func:`adopt` installs it on another
thread. `list.append` is atomic under the GIL, so concurrent workers appending
to one shared book is safe. Threads that are never given a book keep their own,
so two unrelated runs still cannot blend. See
`fundos.profile.pipeline.concurrency.in_worker_context`, which is the only
caller.

WHY NOT JUST QUERY LLMCallLog
-----------------------------
Three reasons. The trace layer must work when the database write fails --
`_log_call` deliberately swallows its own exceptions so tracking can never
break a generation, which means a run can complete with rows missing. A run
is not identifiable in that table without a run_id column that does not exist.
And the trace line has to be emitted before the transaction commits, so the
rows are not readable yet anyway.
"""
import threading
from decimal import Decimal

_storage = threading.local()

# Fields carried per call. Kept flat and primitive so an entry can be
# serialised into a trace line without transformation.
_ENTRY_FIELDS = (
    "role", "endpoint_code", "model", "status",
    "prompt_tokens", "completion_tokens", "thinking_tokens",
    "cache_read_tokens", "cache_write_tokens",
    "cost_inr", "latency_ms", "context_chars",
    "search_count", "tier", "config_profile",
    "was_repaired", "was_normalized", "finish_reason",
)


def _entries():
    book = getattr(_storage, "entries", None)
    if book is None:
        book = []
        _storage.entries = book
    return book


def record(**kwargs):
    """Append one billed call. Never raises -- accounting must not break a run.

    Called from adapter._log_call, which is the single point every dispatch
    path funnels through (success, fallback, mocked, hard failure). Recording
    anywhere else would drift the moment a path was added.
    """
    try:
        entry = {k: kwargs.get(k) for k in _ENTRY_FIELDS}
        entry["prompt_tokens"] = int(entry.get("prompt_tokens") or 0)
        entry["completion_tokens"] = int(entry.get("completion_tokens") or 0)
        entry["thinking_tokens"] = int(entry.get("thinking_tokens") or 0)
        entry["cache_read_tokens"] = int(entry.get("cache_read_tokens") or 0)
        entry["cache_write_tokens"] = int(entry.get("cache_write_tokens") or 0)
        entry["latency_ms"] = int(entry.get("latency_ms") or 0)
        entry["context_chars"] = int(entry.get("context_chars") or 0)
        try:
            entry["cost_inr"] = Decimal(str(entry.get("cost_inr") or 0))
        except Exception:
            entry["cost_inr"] = Decimal("0")
        _entries().append(entry)
    except Exception:      # pragma: no cover - defensive only
        pass


def mark():
    """Opaque cursor for the current position. Pass to `since()`."""
    return len(_entries())


def since(cursor):
    """Entries recorded after `cursor`. Safe against a reset in between."""
    book = _entries()
    start = min(max(int(cursor or 0), 0), len(book))
    return list(book[start:])


def reset():
    """Drop this thread's book. For tests and long-lived worker hygiene."""
    _storage.entries = []


def book():
    """This thread's book, by reference — to hand to a worker thread.

    Returns the live list, not a copy: the point is for a worker to append into
    the same object the parent will later read.
    """
    return _entries()


def adopt(shared):
    """Record into ``shared`` on this thread instead of a fresh book.

    Called at the top of a worker thread with the book captured from the thread
    that spawned it, so calls dispatched in the worker are counted against the
    run that caused them rather than lost when the thread ends.

    Passing ``None`` is a no-op, which keeps the caller free of a null check
    for the case where there was no active run to inherit from.
    """
    if shared is not None:
        _storage.entries = shared


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def summarise(entries):
    """Roll a list of entries into the figures a run report needs.

    `billed_output_tokens` is completion + thinking DELIBERATELY. Every
    provider bills reasoning at the output rate and none of them include it in
    the completion count, so the two must be added before any cost or budget
    comparison. Both components stay visible separately because the split is
    exactly what you need to decide whether a thinking budget is earning its
    keep.
    """
    entries = list(entries or [])
    prompt = sum(e["prompt_tokens"] for e in entries)
    completion = sum(e["completion_tokens"] for e in entries)
    thinking = sum(e["thinking_tokens"] for e in entries)
    cache_read = sum(e["cache_read_tokens"] for e in entries)
    cache_write = sum(e["cache_write_tokens"] for e in entries)
    cost = sum((e["cost_inr"] for e in entries), Decimal("0"))

    searched = [e for e in entries if isinstance(e.get("search_count"), int)
                and e["search_count"] >= 0]
    return {
        "calls": len(entries),
        "failed_calls": sum(1 for e in entries
                            if e.get("status") not in ("success", "fallback",
                                                       "mocked")),
        "mocked_calls": sum(1 for e in entries if e.get("status") == "mocked"),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "thinking_tokens": thinking,
        "billed_output_tokens": completion + thinking,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": prompt + completion + thinking + cache_read + cache_write,
        "cost_inr": _money(cost),
        "latency_ms": sum(e["latency_ms"] for e in entries),
        "context_chars": sum(e["context_chars"] for e in entries),
        # Searches actually performed, and how many calls could report at all.
        # `unknown` is not zero: it means the provider returned no grounding
        # metadata, which is a different fault with a different fix.
        "searches": sum(e["search_count"] for e in searched),
        "searches_unknown": len(entries) - len(searched),
        # Share of INPUT spent re-sending context to follow-up calls. This is
        # the C-03 fan-out number, computed rather than eyeballed.
        "redundant_input_pct": _redundancy(entries),
    }


def by_role(entries):
    """Per-role rollup, heaviest first. Ordered by cost, then tokens.

    The 17 Aug analysis had to build this table by hand to discover that 14
    calls across two roles were burning 51,092 input tokens to produce 3,217
    output tokens. That should fall out of the log.
    """
    buckets = {}
    for e in entries or []:
        b = buckets.setdefault(e.get("role") or "?", {
            "role": e.get("role") or "?", "calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "thinking_tokens": 0,
            "cost_inr": Decimal("0"), "latency_ms": 0, "searches": 0,
            "searches_unknown": 0,
        })
        b["calls"] += 1
        b["prompt_tokens"] += e["prompt_tokens"]
        b["completion_tokens"] += e["completion_tokens"]
        b["thinking_tokens"] += e["thinking_tokens"]
        b["cost_inr"] += e["cost_inr"]
        b["latency_ms"] += e["latency_ms"]
        sc = e.get("search_count")
        if isinstance(sc, int) and sc >= 0:
            b["searches"] += sc
        else:
            b["searches_unknown"] += 1
    out = sorted(buckets.values(),
                 key=lambda b: (b["cost_inr"], b["prompt_tokens"]),
                 reverse=True)
    for b in out:
        b["cost_inr"] = _money(b["cost_inr"])
    return out


def _redundancy(entries):
    """Input tokens spent re-sending near-identical context, as a percentage.

    A HEURISTIC, and labelled as one wherever it is displayed. Within a role,
    one full payload is treated as irreducible and every further call of that
    role is counted as a re-send. A role called once is never redundant with
    itself, so a single-call role such as `profile_synthesis` never
    contributes; the ten `profile_research_batch` calls of one run do.

    On the 17 Aug run 332d9f this yields ~44% against the 51% computed by hand
    across both runs -- the right order of magnitude, which is the accuracy
    this needs. It exists so the C-03 fan-out shows a number that MOVES when
    the batching lands, not to bill anyone.
    """
    total = sum(e["prompt_tokens"] for e in entries)
    if total <= 0:
        return 0.0
    roles = {}
    for e in entries:
        roles.setdefault(e.get("role") or "?", []).append(e["prompt_tokens"])
    irreducible = sum(max(sizes) for sizes in roles.values())
    return round(100.0 * max(total - irreducible, 0) / total, 1)


def _money(value):
    """Two-decimal float. Trace lines are read by humans, not accountants."""
    try:
        return float(round(Decimal(str(value or 0)), 2))
    except Exception:
        return 0.0
