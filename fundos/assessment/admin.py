"""Assessment admin — chiefly the benchmark ingestion screen.

Categories E (Sector, 10%) and F (Sub-Sector, 20%) are scored against the
benchmark tables, so between them a third of every deal rating rests on
this data. It is refreshed by an administrator, from a workbook, without a
deploy — which is why the upload screen shows the column mapping it detected
and a full dry-run diff before anything is written.
"""
import datetime as dt
import os
import tempfile

from django import forms
from django.contrib import admin, messages
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from fundos.assessment.models import SectorDealData, SectorMapping


class BenchmarkUploadForm(forms.Form):
    workbook = forms.FileField(
        label="Deal assessment workbook (.xlsx)",
        help_text="The file containing the 'Sector Deal Data' sheet.")
    sheet_name = forms.CharField(
        initial="Sector Deal Data", required=False,
        help_text="Leave as-is unless the sheet was renamed.")
    as_of = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The date this market data represents. Defaults to today.")
    confirm = forms.BooleanField(
        required=False, label="Write the changes",
        help_text="Leave UNTICKED for a dry run. A real import updates rows "
                  "in place and will move the Sector and Sub-Sector scores "
                  "of any assessment re-run afterwards.")


@admin.register(SectorDealData)
class SectorDealDataAdmin(admin.ModelAdmin):
    list_display = ("name", "level", "deal_count", "total_raised_usd_mn",
                    "avg_ticket_usd_mn", "investor_count", "velocity_score",
                    "ticket_score", "investors_score", "sample_quality",
                    "as_of_date")
    list_filter = ("level", "sample_quality", "as_of_date")
    search_fields = ("name", "clubbed_group")
    readonly_fields = ("updated_at",)
    change_list_template = "admin/assessment/sectordealdata_changelist.html"

    def get_urls(self):
        return [
            path("import/", self.admin_site.admin_view(self.import_view),
                 name="assessment_sectordealdata_import"),
        ] + super().get_urls()

    @admin.display(description="Import")
    def import_link(self, obj):
        return format_html(
            '<a class="button" href="{}">Import benchmarks</a>',
            reverse("admin:assessment_sectordealdata_import"))

    def import_view(self, request):
        from fundos.assessment.benchmarks import apply_rows, parse_workbook
        from django.db import transaction

        context = dict(
            self.admin_site.each_context(request),
            title="Import sector benchmarks",
            form=BenchmarkUploadForm(), report=None, result=None, error=None,
            dry_run=True)

        if request.method != "POST":
            return TemplateResponse(
                request, "admin/assessment/import_benchmarks.html", context)

        form = BenchmarkUploadForm(request.POST, request.FILES)
        context["form"] = form
        if not form.is_valid():
            return TemplateResponse(
                request, "admin/assessment/import_benchmarks.html", context)

        upload = form.cleaned_data["workbook"]
        sheet = form.cleaned_data.get("sheet_name") or "Sector Deal Data"
        as_of = form.cleaned_data.get("as_of") or dt.date.today()
        dry_run = not form.cleaned_data.get("confirm")
        context["dry_run"] = dry_run

        # openpyxl needs a real path, and an uploaded file may only exist in
        # memory, so it is spooled to disk and removed either way.
        handle, temp_path = tempfile.mkstemp(suffix=".xlsx")
        try:
            with os.fdopen(handle, "wb") as out:
                for chunk in upload.chunks():
                    out.write(chunk)
            sectors, subs, report = parse_workbook(temp_path,
                                                   sheet_name=sheet)
            context["report"] = report
            context["sector_count"] = len(sectors)
            context["sub_count"] = len(subs)
            if not sectors and not subs:
                context["error"] = (
                    "No rows were read. Check the sheet name, and that the "
                    "header row carries a deal-count column.")
                return TemplateResponse(
                    request, "admin/assessment/import_benchmarks.html",
                    context)

            with transaction.atomic():
                result = apply_rows(
                    sectors + subs, as_of_date=as_of,
                    source_file=upload.name, dry_run=dry_run,
                    mappings=report.get("mappings"))
                if dry_run:
                    transaction.set_rollback(True)
            context["result"] = result

            if dry_run:
                messages.info(
                    request,
                    f"Dry run: {len(result['created'])} row(s) would be "
                    f"created, {len(result['updated'])} updated, "
                    f"{result['unchanged']} unchanged. Nothing was written — "
                    f"tick 'Write the changes' to apply.")
            else:
                messages.success(
                    request,
                    f"Imported as of {as_of}: {len(result['created'])} "
                    f"created, {len(result['updated'])} updated, "
                    f"{result['mappings_created']} new sub-sector mappings. "
                    f"Assessments re-run from now on will use this data.")
        except Exception as e:
            context["error"] = f"{type(e).__name__}: {e}"
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

        return TemplateResponse(
            request, "admin/assessment/import_benchmarks.html", context)


@admin.register(SectorMapping)
class SectorMappingAdmin(admin.ModelAdmin):
    """Raw label -> clubbed group.

    This decides which benchmark row a company is judged against, so an
    unmapped or wrongly-mapped label misprices a category worth 20% of the
    rating. Editable here because a new label is data, not a deploy.
    """
    list_display = ("raw_label", "clubbed_group")
    list_filter = ("clubbed_group",)
    search_fields = ("raw_label", "clubbed_group")
    list_editable = ("clubbed_group",)
    list_per_page = 50
