from decimal import Decimal
from django import forms
from django.utils import timezone

from leads.models import Lead, Course, LeadStage
from accounts.models import User
from .models import Admission, PaymentPlan, CourseStatus


class DirectAdmissionForm(forms.Form):
    # Student Details
    student_name = forms.CharField(label="Student Full Name", max_length=150, required=True)
    mobile = forms.CharField(label="Mobile Number", max_length=20, required=True)
    email = forms.EmailField(label="Email Address", required=False)
    city = forms.CharField(label="City", max_length=100, required=False)

    # Course & Admission Details
    course = forms.ModelChoiceField(
        queryset=Course.objects.filter(is_active=True),
        label="Course *",
        required=True
    )
    admission_date = forms.DateField(
        label="Admission Date *",
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=timezone.localdate,
        required=True
    )

    # Fees & Payment Plan
    total_fee = forms.DecimalField(label="Total Fee (₹) *", max_digits=12, decimal_places=2, min_value=0, required=True)
    discount = forms.DecimalField(label="Discount Allowed (₹)", max_digits=12, decimal_places=2, min_value=0, initial=Decimal("0.00"), required=False)
    extra_discount_reason = forms.CharField(label="Discount Reason / Notes", widget=forms.Textarea(attrs={"rows": 2}), required=False)
    payment_plan = forms.ChoiceField(label="Payment Plan", choices=PaymentPlan.choices, initial=PaymentPlan.FULL)
    course_status = forms.ChoiceField(label="Course Status", choices=CourseStatus.choices, initial=CourseStatus.STARTED)

    # Counselor Assignment
    assigned_counselor = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True, is_approved=True),
        label="Assigned Counselor / Employee",
        required=False
    )
    notes = forms.CharField(label="Admission Notes", widget=forms.Textarea(attrs={"rows": 2}), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
            field.widget.attrs.setdefault("class", css)
