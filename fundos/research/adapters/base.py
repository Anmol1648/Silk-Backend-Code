"""
Research source adapters (Doc 1 LLM-M1-001) — a provider-registry of
independent, individually-toggleable adapters. One file per source
(Doc 8 §1). Each adapter is best-effort: an unreachable source marks itself
failed and research proceeds (ERR-M1-001, never blocks).

Adapters return a NORMALISED payload dict:
    {"facts": {...}, "raw": {...}, "confidence": 0..1}
"""
import abc
import logging
import re

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"^https?://[a-zA-Z0-9.-]+", re.I)


def validate_url(url: str) -> bool:
    """VAL-M1-001 — scheme/host validation before dispatch."""
    return bool(url and _URL_RE.match(url.strip()))


class BaseResearchAdapter(abc.ABC):
    source_type = "base"

    # Does this adapter read a real source, or return a placeholder pending
    # its feed? A fixture that reports OK is worse than one that reports
    # nothing: it contributes a few dozen characters of prose, counts toward
    # the source total, and makes a run that retrieved NOTHING look like a
    # run with nine working research sources.
    is_fixture = False

    def __init__(self, deal, ckb_snapshot: dict):
        self.deal = deal
        self.ckb = ckb_snapshot

    @abc.abstractmethod
    def fetch(self) -> dict:
        """Return the normalised payload; raise on hard failure."""
