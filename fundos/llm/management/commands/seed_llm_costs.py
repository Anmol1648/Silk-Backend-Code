"""Seed / refresh the LLM price book — provider-agnostic.

WHY THIS IS DERIVED, NOT HARD-CODED
-----------------------------------
`_log_call` writes `LLMCallLog.provider = endpoint.code` (e.g. "ANTHROPIC",
"GEMINI"), and `LLMModelCost.cost_inr` looks a row up by that same
(provider, model_name) pair. A hard-coded price list keyed on a provider
FAMILY name ("anthropic") therefore never joins, and every call silently
prices off the wildcard row — or off nothing at all, recording zero.

So this command walks the endpoints and config profiles that are ACTUALLY
configured, and creates a price row for every (endpoint code, model string)
combination in use, plus a wildcard per endpoint as a safety net. Add a
Gemini or OpenAI endpoint tomorrow and re-run: the rows appear, correctly
keyed, with no code change.

    python manage.py seed_llm_costs
    python manage.py seed_llm_costs --usd-inr 87.5 --overwrite
    python manage.py seed_llm_costs --check      # audit only, no writes
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

# Published rates in USD per MILLION tokens, keyed by provider family and a
# model-string PREFIX (longest match wins). Prefix matching means a dated
# variant such as claude-haiku-4-5-20251001 inherits the claude-haiku-4-5
# rate without needing its own entry.
RATE_TABLE = {
    "anthropic": [
        ("claude-fable", 10.00, 50.00),
        ("claude-opus-4-1", 15.00, 75.00),   # legacy, priced high
        ("claude-opus", 5.00, 25.00),
        ("claude-sonnet-5", 3.00, 15.00),    # post-31-Aug-2026 standard rate
        ("claude-sonnet", 3.00, 15.00),
        ("claude-haiku", 1.00, 5.00),
    ],
    "openai": [
        ("gpt-5.6-sol", 5.00, 30.00),
        ("gpt-5.6-terra", 2.00, 12.00),
        ("gpt-5.6-luna", 0.20, 1.20),
        ("gpt-5.6", 2.00, 12.00),
        ("gpt-5.5", 5.00, 30.00),
        ("gpt-5.4", 2.50, 15.00),
        ("gpt-5", 2.50, 15.00),
        ("gpt-4o-mini", 0.15, 0.60),
        ("gpt-4o", 2.50, 10.00),
        ("gpt-4", 10.00, 30.00),
        ("o3", 2.00, 8.00),
    ],
    "gemini": [
        ("gemini-3.1-pro", 2.00, 12.00),
        ("gemini-3-pro", 2.00, 12.00),
        ("gemini-3.6-flash", 1.50, 7.50),
        ("gemini-3.5-flash-lite", 0.30, 2.50),
        ("gemini-3.5-flash", 1.50, 9.00),
        ("gemini-3.1-flash-lite", 0.25, 1.50),
        ("gemini-3", 1.50, 9.00),
        ("gemini-2.5-pro", 1.25, 10.00),
        ("gemini-2.5-flash-lite", 0.10, 0.40),
        ("gemini-2.5-flash", 0.30, 2.50),
        ("gemini", 1.25, 5.00),
    ],
}

# Fallback per family when no prefix matches — deliberately pessimistic, so an
# unpriced model over-reports rather than silently costing nothing.
FAMILY_DEFAULT = {
    "anthropic": (5.00, 25.00),
    "openai": (2.50, 10.00),
    "gemini": (1.25, 5.00),
}

CACHE_READ_MULTIPLIER = Decimal("0.10")
CACHE_WRITE_MULTIPLIER = Decimal("1.25")


def rates_for(family, model_string):
    """(input_usd, output_usd) per million tokens — longest prefix wins."""
    best = None
    for prefix, usd_in, usd_out in RATE_TABLE.get(family, []):
        if model_string.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, usd_in, usd_out)
    if best:
        return best[1], best[2]
    return FAMILY_DEFAULT.get(family, (5.00, 25.00))


class Command(BaseCommand):
    help = ("Seed the LLMModelCost price book from the endpoints and config "
            "profiles that are actually configured.")

    def add_arguments(self, parser):
        parser.add_argument("--usd-inr", type=float, default=88.0,
                            help="USD to INR conversion (default 88).")
        parser.add_argument("--overwrite", action="store_true",
                            help="Replace existing rows instead of keeping "
                                 "admin edits.")
        parser.add_argument("--check", action="store_true",
                            help="Audit only: report unpriced models, write "
                                 "nothing.")

    def handle(self, *args, **options):
        fx = Decimal(str(options["usd_inr"]))
        overwrite = options["overwrite"]
        check_only = options["check"]

        wanted = self._configured_pairs()
        if not wanted:
            self.stdout.write(self.style.ERROR(
                "No LLM endpoints configured — nothing to price. Run "
                "seed_platform_config first."))
            return

        if check_only:
            self._audit(wanted)
            return

        from fundos.llm.models import LLMModelCost

        created = updated = kept = 0
        for endpoint_code, model_string, family in sorted(wanted):
            if model_string == "*":
                usd_in, usd_out = FAMILY_DEFAULT.get(family, (5.00, 25.00))
            else:
                usd_in, usd_out = rates_for(family, model_string)

            in_inr = (Decimal(str(usd_in)) * fx / Decimal("1000")).quantize(
                Decimal("0.000001"))
            out_inr = (Decimal(str(usd_out)) * fx / Decimal("1000")).quantize(
                Decimal("0.000001"))
            defaults = {
                "input_cost_per_1k_inr": in_inr,
                "output_cost_per_1k_inr": out_inr,
                "cache_read_cost_per_1k_inr": (
                    in_inr * CACHE_READ_MULTIPLIER).quantize(
                        Decimal("0.000001")),
                "cache_write_cost_per_1k_inr": (
                    in_inr * CACHE_WRITE_MULTIPLIER).quantize(
                        Decimal("0.000001")),
                "is_active": True,
            }

            row = LLMModelCost.objects.filter(
                provider=endpoint_code, model_name=model_string).first()
            if row is None:
                LLMModelCost.objects.create(
                    provider=endpoint_code, model_name=model_string,
                    **defaults)
                created += 1
            elif overwrite:
                for k, v in defaults.items():
                    setattr(row, k, v)
                row.save()
                updated += 1
            else:
                kept += 1

        self.stdout.write(self.style.SUCCESS(
            f"  price book: {created} created, {updated} updated, "
            f"{kept} kept."))
        self._audit(wanted, quiet_when_clean=True)

    def _configured_pairs(self):
        """Every (endpoint_code, model_string, family) the adapter can emit."""
        from fundos.llm.models import (ENDPOINT_KIND_TO_PROVIDER,
                                       LLMConfigProfile, LLMEndpoint,
                                       LLMModelCatalog)

        wanted = set()
        for ep in LLMEndpoint.objects.all():
            family = ENDPOINT_KIND_TO_PROVIDER.get(ep.provider_kind)
            if not family:
                continue
            wanted.add((ep.code, "*", family))
            if ep.default_model:
                wanted.add((ep.code, ep.default_model, family))
            for prof in LLMConfigProfile.objects.filter(endpoint_id=ep.code):
                if prof.model_string:
                    wanted.add((ep.code, prof.model_string, family))
            for cat in LLMModelCatalog.objects.filter(provider=family,
                                                      is_active=True):
                wanted.add((ep.code, cat.model_string, family))
        return wanted

    def _audit(self, wanted, quiet_when_clean=False):
        """Report any configured model with no matching price row."""
        from fundos.llm.models import LLMModelCost

        missing = []
        for endpoint_code, model_string, _family in sorted(wanted):
            if model_string == "*":
                continue
            if LLMModelCost.objects.filter(
                    provider=endpoint_code, model_name=model_string,
                    is_active=True).exists():
                continue
            wildcard = LLMModelCost.objects.filter(
                provider=endpoint_code, model_name="*",
                is_active=True).exists()
            missing.append((endpoint_code, model_string, wildcard))

        if not missing:
            if not quiet_when_clean:
                self.stdout.write(self.style.SUCCESS(
                    "Every configured model has an exact price row."))
            return

        self.stdout.write(self.style.WARNING(
            "Models with no EXACT price row:"))
        for endpoint_code, model_string, wildcard in missing:
            note = ("falls back to the wildcard row" if wildcard
                    else "WILL RECORD ZERO COST")
            style = self.style.WARNING if wildcard else self.style.ERROR
            self.stdout.write(style(
                f"  {endpoint_code} / {model_string} — {note}"))
