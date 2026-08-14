from django.contrib import admin
from .models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("admission", "amount", "payment_date", "payment_mode", "payment_status")
    list_filter = ("payment_mode", "payment_status")
