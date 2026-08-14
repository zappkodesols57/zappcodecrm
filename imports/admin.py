from django.contrib import admin
from .models import ImportJob, ImportError


class ImportErrorInline(admin.TabularInline):
    model = ImportError
    extra = 0


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "status", "total_rows", "imported_count", "duplicate_count", "invalid_count", "created_at")
    inlines = [ImportErrorInline]
