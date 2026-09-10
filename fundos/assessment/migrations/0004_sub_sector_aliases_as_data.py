"""Move the hard-coded sub-sector aliases into the table they belong in.

`resolve_sub_sector` carried a literal dictionary of nine label -> group
aliases. Sector knowledge in source is a design violation with its own
failing test, but the more telling problem was that four of the nine did not
work: the tyre entries, added to rescue exactly the Tyreplex case, targeted
"B2B E-commerce". The benchmark table holds "B2B Ecommerce", without the
hyphen. So the aliases written to fix the problem resolved to a cohort that
does not exist, did nothing, and said nothing — an alias pointing at a
missing group is indistinguishable from an alias that was never consulted.

Four of the remaining five were already redundant: SectorMapping resolves
"Digital Health", "digital healthcare", "Telemedicine" and "Teleconsultation"
from the imported workbook. Only "health management" was doing real work.

Written as rows here so they are visible, editable in admin without a deploy,
and — because the target is checked against SectorDealData below — incapable
of silently pointing at a group that is not there.
"""
from django.db import migrations

#: Aliases the source dictionary carried, with the tyre targets corrected.
ALIASES = (
    ("Health Management", "Healthtech"),
    ("B2B Aftermarket Tyre E-commerce", "B2B Ecommerce"),
    ("Automotive E-commerce", "B2B Ecommerce"),
    ("Aftermarket Tyre", "B2B Ecommerce"),
    ("Aftermarket Tyre E-commerce", "B2B Ecommerce"),
    ("B2B E-commerce Platform", "B2B Ecommerce"),
)


def add_aliases(apps, schema_editor):
    SectorMapping = apps.get_model("assessment", "SectorMapping")
    SectorDealData = apps.get_model("assessment", "SectorDealData")

    # An alias to a group that does not exist is worse than no alias: it
    # resolves, reports a method of "exact", and then finds no cohort. Only
    # write the ones whose target is really there.
    if not SectorDealData.objects.exists():
        return
    for raw_label, group in ALIASES:
        if not SectorDealData.objects.filter(
                clubbed_group__iexact=group).exists():
            continue
        SectorMapping.objects.update_or_create(
            raw_label=raw_label, defaults={"clubbed_group": group})


def remove_aliases(apps, schema_editor):
    SectorMapping = apps.get_model("assessment", "SectorMapping")
    SectorMapping.objects.filter(
        raw_label__in=[raw for raw, _ in ALIASES]).delete()


class Migration(migrations.Migration):

    dependencies = [("assessment", "0003_workbook_config")]

    operations = [migrations.RunPython(add_aliases, remove_aliases)]
