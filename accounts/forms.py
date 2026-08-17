from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import User


class CRMUserCreateForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "role", "department", "speciality", "phone", "reports_to")

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if self.user and self.user.hospital:
            self.fields["role"].choices = [
                (User.Role.ADMIN, "Admin"),
                (User.Role.MANAGER, "Manager"),
                (User.Role.LEAD_ATTENDENT, "Lead Attendent"),
                (User.Role.DOCTOR, "Doctor"),
            ]
        for name, field in self.fields.items():
            css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
            field.widget.attrs.setdefault("class", css)


class CRMUserEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "role", "department", "speciality", "phone", "reports_to", "is_active_employee", "is_active")

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if self.user and self.user.hospital:
            self.fields["role"].choices = [
                (User.Role.ADMIN, "Admin"),
                (User.Role.MANAGER, "Manager"),
                (User.Role.LEAD_ATTENDENT, "Lead Attendent"),
                (User.Role.DOCTOR, "Doctor"),
            ]
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
                field.widget.attrs.setdefault("class", css)


class CRMUserPasswordResetForm(forms.Form):
    new_password = forms.CharField(
        label="New Password",
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Enter new password"}),
        min_length=6,
        required=True
    )
    confirm_password = forms.CharField(
        label="Confirm New Password",
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Confirm new password"}),
        min_length=6,
        required=True
    )

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get("new_password")
        p2 = cleaned_data.get("confirm_password")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data


class CRMUserRegisterForm(UserCreationForm):
    role = forms.ChoiceField(choices=[
        (User.Role.ADMIN, "Admin"),
        (User.Role.MANAGER, "Manager"),
        (User.Role.LEAD_ATTENDENT, "Lead Attendent"),
        (User.Role.DOCTOR, "Doctor"),
    ], initial=User.Role.LEAD_ATTENDENT, required=True)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "role", "department", "speciality", "phone")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
            field.widget.attrs.setdefault("class", css)
