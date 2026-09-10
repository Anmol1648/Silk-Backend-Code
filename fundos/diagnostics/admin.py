from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import path

from fundos.diagnostics.models import DiagEvent, RunRecord
from fundos.diagnostics.services import health


@admin.register(RunRecord)
class RunRecordAdmin(admin.ModelAdmin):
    list_display = ["started_at", "kind", "subject", "status", "duration_ms",
                    "correlation_id"]
    list_filter = ["kind", "status"]
    search_fields = ["subject", "correlation_id", "deal_id", "company_id"]
    readonly_fields = [f.name for f in RunRecord._meta.fields]
    change_list_template = "admin/diagnostics/runrecord_changelist.html"

    def has_add_permission(self, request):
        return False

    def get_urls(self):
        return [path("dashboard/",
                     self.admin_site.admin_view(self.dashboard),
                     name="diagnostics_dashboard")] + super().get_urls()

    def dashboard(self, request):
        context = dict(self.admin_site.each_context(request),
                       title="System health",
                       health=health(),
                       recent=RunRecord.objects.all()[:15])
        return TemplateResponse(
            request, "admin/diagnostics/dashboard.html", context)


@admin.register(DiagEvent)
class DiagEventAdmin(admin.ModelAdmin):
    list_display = ["created_at", "level", "component", "short_message",
                    "correlation_id"]
    list_filter = ["level", "component"]
    search_fields = ["message", "correlation_id"]
    readonly_fields = [f.name for f in DiagEvent._meta.fields]

    @admin.display(description="Message")
    def short_message(self, obj):
        return obj.message[:110]

    def has_add_permission(self, request):
        return False
