from django.contrib import admin

from fundos.config.models import (
    AppConfiguration, InvestorCategoryMap, MasterValue, ResearchSourceFlags,
    ScoringConfig, SectorTaxonomyMap,
)

admin.site.register(AppConfiguration)
admin.site.register(ResearchSourceFlags)


@admin.register(ScoringConfig)
class ScoringConfigAdmin(admin.ModelAdmin):
    list_display = ("version_no", "is_active", "created_at")


@admin.register(MasterValue)
class MasterValueAdmin(admin.ModelAdmin):
    list_display = ("table", "code", "label", "is_active", "sort_order")
    list_filter = ("table",)


admin.site.register(InvestorCategoryMap)
admin.site.register(SectorTaxonomyMap)
