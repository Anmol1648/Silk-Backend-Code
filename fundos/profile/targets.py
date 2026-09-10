"""
Manual deal targets (C6).

A founder or advisor who already knows what they are raising may enter the
headline terms directly and proceed to Investor Discovery without running
the guided strategy. The stage process is not mandatory; the major outputs
are.

Four fields, of which two are INPUTS and two are CALCULATED:

    1. Target raise            input
    2. Pre-money valuation     input
    3. Post-money valuation    calculated = pre-money + raise
    4. Dilution %              calculated = raise / post-money x 100

Post-money and dilution are arithmetic, not opinions, so they are computed
here rather than typed. Storing them alongside the inputs keeps the record
self-contained and auditable — a reader does not have to re-derive them —
while `recalculate()` guarantees they can never drift from the inputs.

Money is stored as a triple (value, currency, basis) per the platform
convention, and the USD equivalents are stored beside the entered values so
downstream comparison never has to guess a rate. The FX rate used and its
as-at date are recorded on the row (C11).
"""
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import models

from fundos.core.models.base import BaseModel


class DealTargets(BaseModel):
    """Founder-entered headline terms for a raise.

    Deal-scoped: a company may run several concurrent raises (C1), and each
    has its own targets.
    """

    deal_id = models.UUIDField(db_index=True)
    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, related_name="deal_targets")

    # --- 1. Target raise (input) ---
    target_raise_value = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True,
        help_text="How much the company intends to raise.")
    target_raise_ccy = models.CharField(max_length=3, default="USD")
    target_raise_basis = models.CharField(max_length=32, default="target")

    # --- 2. Pre-money valuation (input) ---
    pre_money_value = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True,
        help_text="Company valuation before the new money comes in.")
    pre_money_ccy = models.CharField(max_length=3, default="USD")
    pre_money_basis = models.CharField(max_length=32, default="pre_money")

    # --- 3. Post-money valuation (calculated) ---
    post_money_value = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True,
        help_text="Calculated: pre-money + target raise. Never entered.")
    post_money_ccy = models.CharField(max_length=3, default="USD")
    post_money_basis = models.CharField(max_length=32, default="post_money")

    # --- 4. Dilution (calculated) ---
    dilution_pct = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
        help_text="Calculated: raise / post-money x 100. Never entered.")

    # USD equivalents for cross-company comparison and bucket classification.
    target_raise_usd = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True)
    pre_money_usd = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True)
    post_money_usd = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True)

    # C11 — the rate used is part of the record, not an ambient global.
    fx_rate_used = models.DecimalField(
        max_digits=20, decimal_places=6, null=True, blank=True)
    fx_as_of = models.DateField(null=True, blank=True)
    fx_source = models.CharField(max_length=64, blank=True, default="")

    # Provenance: these are founder-stated terms, not system recommendations.
    source = models.CharField(
        max_length=32, default="founder",
        help_text="founder | advisor | derived_from_strategy")
    notes = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True, db_index=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "deal_targets"
        constraints = [
            models.UniqueConstraint(
                fields=["deal_id"],
                condition=models.Q(is_active=True, is_deleted=False),
                name="uq_active_deal_targets"),
        ]

    def __str__(self):
        return f"Targets · deal {self.deal_id}"

    # ------------------------------------------------------------------
    # Calculation
    # ------------------------------------------------------------------
    def recalculate(self):
        """Derive post-money and dilution from the two inputs.

        Called on every save, so the calculated fields can never drift from
        the values they are derived from. Both are left null when the
        inputs are incomplete rather than being guessed at.
        """
        raise_v = self.target_raise_value
        pre_v = self.pre_money_value

        if raise_v is None or pre_v is None:
            self.post_money_value = None
            self.dilution_pct = None
            return self

        raise_v = Decimal(str(raise_v))
        pre_v = Decimal(str(pre_v))

        # 3. Post-money = pre-money + raise
        post = pre_v + raise_v
        self.post_money_value = post.quantize(Decimal("0.01"),
                                              rounding=ROUND_HALF_UP)
        self.post_money_ccy = self.pre_money_ccy or "USD"

        # 4. Dilution = raise / post-money. Undefined at zero post-money.
        if post > 0:
            self.dilution_pct = (raise_v / post * Decimal("100")).quantize(
                Decimal("0.001"), rounding=ROUND_HALF_UP)
        else:
            self.dilution_pct = None
        return self

    def convert_to_usd(self):
        """Store USD equivalents and record the rate used (C11)."""
        from fundos.platformcfg.services import to_usd

        raise_usd, rate, as_of, source = to_usd(
            self.target_raise_value, self.target_raise_ccy)
        pre_usd, _, _, _ = to_usd(self.pre_money_value, self.pre_money_ccy)
        post_usd, _, _, _ = to_usd(self.post_money_value, self.post_money_ccy)

        self.target_raise_usd = raise_usd
        self.pre_money_usd = pre_usd
        self.post_money_usd = post_usd
        if rate is not None:
            self.fx_rate_used = rate
            self.fx_as_of = as_of
            self.fx_source = source or ""
        return self

    def save(self, *args, **kwargs):
        # Calculated fields are derived on every write — there is no path
        # by which a caller can set them directly to an inconsistent value.
        self.recalculate()
        self.convert_to_usd()
        super().save(*args, **kwargs)

    # ------------------------------------------------------------------
    @property
    def is_complete(self):
        """Both inputs present, so the calculated pair is meaningful."""
        return (self.target_raise_value is not None
                and self.pre_money_value is not None)

    def as_payload(self):
        def num(v):
            return float(v) if v is not None else None

        return {
            # 1 & 2 — entered
            "targetRaise": num(self.target_raise_value),
            "preMoneyValuation": num(self.pre_money_value),
            # 3 & 4 — calculated, never entered
            "postMoneyValuation": num(self.post_money_value),
            "dilutionPct": num(self.dilution_pct),
            "ccy": self.target_raise_ccy or "USD",
            "usd": {
                "targetRaise": num(self.target_raise_usd),
                "preMoneyValuation": num(self.pre_money_usd),
                "postMoneyValuation": num(self.post_money_usd),
            },
            "fx": {
                "rateUsed": num(self.fx_rate_used),
                "asOf": self.fx_as_of,
                "source": self.fx_source or None,
            },
            "isComplete": self.is_complete,
            "source": self.source,
            "notes": self.notes,
            "updatedAt": self.updated_at,
        }
