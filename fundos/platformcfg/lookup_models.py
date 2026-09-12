"""
Lookup master tables (Requirement 1).

Standalone module to keep the main models.py untouched. Imported by
the migration and the LookupsView.
"""
from django.db import models


#: A value that arrived with a company rather than being curated.
#:
#: Blocking company creation over a dropdown is the wrong trade: a founder
#: whose industry is not listed picks whichever nearest value lets the form
#: submit, and the result is wrong data that looks clean. So a new label is
#: accepted, and marked -- so an administrator can tell the list they built
#: from the ones that arrived by accident, which is impossible once a
#: curated 60 has quietly become 300.
PENDING_HELP = ("Arrived with a company rather than being curated. An "
                "administrator confirms it, and for a sub-sector says which "
                "benchmark group it belongs to.")


class Sector(models.Model):
    """Macro sectors — admin-managed dropdown values."""
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    is_pending = models.BooleanField(default=False, help_text=PENDING_HELP)
    added_from = models.CharField(
        max_length=255, blank=True, default="",
        help_text="The company whose profile introduced this value.")

    class Meta:
        db_table = "lookup_sector"
        ordering = ["name"]

    def __str__(self):
        return self.name


class SubSector(models.Model):
    """Sub-sectors — optionally linked to a parent sector.

    A sub-sector is also the BENCHMARK JOIN KEY: it selects the peer cohort
    Category F scores a company against, which is a fifth of the rating. A
    new one therefore has two separate questions behind it, and only the
    first is answered by adding it here.

        1. Is this a real label? -- answered by accepting it.
        2. Which existing cohort is it? -- answered only by a person.

    Until (2) is answered the category scores BLANK, which is correct: a
    blank is excluded from the denominator and redistributes its weight,
    whereas a cohort invented to fill the gap would hold no deals and score
    the company against nothing while looking calculated.
    """
    name = models.CharField(max_length=100, unique=True)
    sector = models.ForeignKey(
        Sector, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="sub_sectors")
    is_active = models.BooleanField(default=True)
    is_pending = models.BooleanField(default=False, help_text=PENDING_HELP)
    added_from = models.CharField(
        max_length=255, blank=True, default="",
        help_text="The company whose profile introduced this value.")

    class Meta:
        db_table = "lookup_sub_sector"
        ordering = ["name"]

    def __str__(self):
        return self.name


class FundingStatus(models.Model):
    """Funding status labels with display ordering."""
    name = models.CharField(max_length=100, unique=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "lookup_funding_status"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class RevenueSize(models.Model):
    """Revenue size bands with display ordering."""
    name = models.CharField(max_length=100, unique=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "lookup_revenue_size"
        ordering = ["sort_order"]

    def __str__(self):
        return self.name


class Currency(models.Model):
    """ISO currency codes with display symbols."""
    code = models.CharField(max_length=10, unique=True)
    symbol = models.CharField(max_length=5, blank=True, default="")

    class Meta:
        db_table = "lookup_currency"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} ({self.symbol})"
