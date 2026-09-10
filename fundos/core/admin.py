"""Django admin — operational back-office (Doc 6 §6.6 uses the API surface;
this is a convenience for platform staff)."""
from django.contrib import admin

from fundos.core.models import (
    AlertDefinition, AuditLog, Company, Deal, Membership, Notification,
    Tenant, User,
)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "created_at")


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "tenant_id", "is_active", "is_deleted")
    search_fields = ("email", "name")


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "domain", "tenant_id", "is_deleted")


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "round_type", "status", "is_deleted")


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "scope_type", "scope_id", "role", "status")


@admin.register(AlertDefinition)
class AlertDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "source_type", "event_key", "is_active",
                    "last_run_status")
    list_filter = ("source_type", "is_active")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("recipient", "type", "title", "is_read", "created_at")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "actor", "deal_id", "entity", "at")
    readonly_fields = [f.name for f in AuditLog._meta.fields]
