"""
Lookup master tables (Requirement 1).

Standalone module to keep the main models.py untouched. Imported by
the migration and the LookupsView.
"""
from django.db import models


class Sector(models.Model):
    """Macro sectors — admin-managed dropdown values."""
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "lookup_sector"
        ordering = ["name"]

    def __str__(self):
        return self.name


class SubSector(models.Model):
    """Sub-sectors — optionally linked to a parent sector."""
    name = models.CharField(max_length=100, unique=True)
    sector = models.ForeignKey(
        Sector, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="sub_sectors")
    is_active = models.BooleanField(default=True)

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
