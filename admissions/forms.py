from decimal import Decimal
from django import forms
from django.utils import timezone

from leads.models import Lead, Course, LeadStage
from accounts.models import User
from .models import Admission, PaymentPlan, CourseStatus


class DirectAdmissionForm(forms.Form):
    # Student Details
    student_name = forms.CharField(
        label="Student Full Name", 
        max_length=150, 
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "e.g. John Doe"})
    )
    mobile = forms.CharField(
        label="Mobile Number *", 
        max_length=20, 
        required=True,
        widget=forms.TextInput(attrs={
            "placeholder": "10-digit mobile number",
            "maxlength": "10",
            "minlength": "10",
            "pattern": "^[0-9]{10}$",
            "inputmode": "numeric",
            "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
        })
    )
    email = forms.EmailField(
        label="Email Address", 
        required=False,
        widget=forms.EmailInput(attrs={
            "placeholder": "student@example.com",
            "pattern": r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
            "title": "Please enter a valid email address containing '@' (e.g. student@example.com)",
        })
    )
    city = forms.CharField(
        label="City", 
        max_length=100, 
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "City / Location"})
    )

    # Course & Admission Details
    course = forms.ModelChoiceField(
        queryset=Course.objects.filter(is_active=True),
        label="Course *",
        required=True,
        empty_label="-- Select Course --"
    )
    admission_date = forms.DateField(
        label="Admission Date *",
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=timezone.localdate,
        required=True
    )

    # Fees & Payment Plan
    total_fee = forms.DecimalField(
        label="Total Fee (₹) *", 
        max_digits=12, 
        decimal_places=2, 
        min_value=0, 
        required=True,
        widget=forms.NumberInput(attrs={"placeholder": "0.00"})
    )
    discount = forms.DecimalField(
        label="Discount Allowed (₹)", 
        max_digits=12, 
        decimal_places=2, 
        min_value=0, 
        initial=Decimal("0.00"), 
        required=False,
        widget=forms.NumberInput(attrs={"placeholder": "0.00"})
    )
    extra_discount_reason = forms.CharField(
        label="Discount Reason / Notes", 
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Optional justification if discount provided..."}), 
        required=False
    )
    payment_plan = forms.ChoiceField(label="Payment Plan", choices=PaymentPlan.choices, initial=PaymentPlan.FULL)
    course_status = forms.ChoiceField(label="Course Status", choices=CourseStatus.choices, initial=CourseStatus.STARTED)

    # Counselor Assignment
    assigned_counselor = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True, is_approved=True),
        label="Assigned Counselor / Employee",
        required=False,
        empty_label="-- Select Counselor / HR --"
    )
    notes = forms.CharField(
        label="Admission Notes", 
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Internal remarks or student background notes..."}), 
        required=False
    )

    is_existing_student = forms.BooleanField(
        label="Already Enrolled Student",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"})
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        today_str = timezone.localdate().isoformat()
        self.fields["admission_date"].widget.attrs["min"] = today_str

        if user and getattr(user, "hospital", None):
            self.fields["course"].queryset = Course.objects.filter(is_active=True, hospital=user.hospital)
            self.fields["assigned_counselor"].queryset = User.objects.filter(is_active=True, is_approved=True, hospital=user.hospital)
        else:
            self.fields["course"].queryset = Course.objects.filter(is_active=True, hospital__isnull=True)
            self.fields["assigned_counselor"].queryset = User.objects.filter(is_active=True, is_approved=True, hospital__isnull=True)

        for name, field in self.fields.items():
            if name != "is_existing_student":
                css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
                field.widget.attrs.setdefault("class", css)

    def clean_email(self):
        import re
        email = (self.cleaned_data.get("email") or "").strip()
        if email:
            if "@" not in email or not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email):
                raise forms.ValidationError("Please enter a valid email address with '@' (e.g. student@example.com).")
        return email

    def clean_mobile(self):
        import re
        mobile = self.cleaned_data.get("mobile", "")
        if mobile:
            digits = re.sub(r"\D", "", str(mobile))
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10:
                raise forms.ValidationError("Mobile number must be exactly 10 digits.")
            return digits
        return mobile

    def clean(self):
        cleaned_data = super().clean()
        is_existing = cleaned_data.get("is_existing_student")
        admission_date = cleaned_data.get("admission_date")
        course = cleaned_data.get("course")
        discount = cleaned_data.get("discount") or Decimal("0.00")

        if not is_existing and admission_date and admission_date < timezone.localdate():
            self.add_error("admission_date", "Admission / Enrollment date cannot be in the past for new admissions. Check 'Already Enrolled Student' if enrolling for a past date.")
        elif is_existing and admission_date and admission_date > timezone.localdate():
            self.add_error("admission_date", "For 'Already Enrolled Student', admission date cannot be a future date. Please select today or a past date.")

        if course and discount > Decimal("0.00"):
            max_allowed = Decimal(str(getattr(course, "max_discount", 0)))
            if max_allowed > 0 and discount > max_allowed:
                self.add_error("discount", f"Discount Limit Exceeded! Maximum allowed discount for {course.name} is ₹{max_allowed:,.0f}.")
        return cleaned_data
