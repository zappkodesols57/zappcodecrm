from django.contrib import admin
from .models import Admission


@admin.register(Admission)
class AdmissionAdmin(admin.ModelAdmin):
    list_display = ("student_name", "course", "admission_date", "final_fee", "assigned_counselor")
    list_filter = ("course",)
