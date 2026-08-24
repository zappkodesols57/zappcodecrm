from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import User


class CRMUserCreateForm(UserCreationForm):
    can_import_export = forms.BooleanField(
        required=False, 
        label="Allow Lead Data Import & Export (Excel/CSV)",
        help_text="Check to allow this employee to import and export lead data from Excel/CSV files."
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "role", "hospital", "department", "speciality", "phone", "reports_to", "can_import_export")

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if self.user and self.user.hospital:
            if self.user.role == User.Role.MANAGER:
                self.fields["role"].choices = [
                    (User.Role.LEAD_ATTENDENT, "Lead Attendent"),
                    (User.Role.DOCTOR, "Doctor"),
                ]
            else:
                self.fields["role"].choices = [
                    (User.Role.ADMIN, "Admin"),
                    (User.Role.MANAGER, "Manager"),
                    (User.Role.LEAD_ATTENDENT, "Lead Attendent"),
                    (User.Role.DOCTOR, "Doctor"),
                ]
            if "hospital" in self.fields:
                del self.fields["hospital"]
            if "reports_to" in self.fields:
                self.fields["reports_to"].queryset = User.objects.filter(hospital=self.user.hospital, is_active=True)
        
        if "hospital" in self.fields:
            self.fields["hospital"].label = "Business"
            
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
                field.widget.attrs.setdefault("class", css)

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.user and self.user.hospital:
            user.hospital = self.user.hospital
        can_imp = self.cleaned_data.get("can_import_export", False)
        user.custom_permissions["import_export"] = can_imp
        if commit:
            user.save()
        return user


class CRMUserEditForm(forms.ModelForm):
    can_import_export = forms.BooleanField(
        required=False, 
        label="Allow Lead Data Import & Export (Excel/CSV)",
        help_text="Check to allow this employee to import and export lead data from Excel/CSV files."
    )

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "role", "hospital", "department", "speciality", "phone", "reports_to", "can_import_export", "is_active_employee", "is_active")

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["can_import_export"].initial = self.instance.can_import_export

        if self.user and self.user.hospital:
            if self.user.role == User.Role.MANAGER:
                self.fields["role"].choices = [
                    (User.Role.LEAD_ATTENDENT, "Lead Attendent"),
                    (User.Role.DOCTOR, "Doctor"),
                ]
            else:
                self.fields["role"].choices = [
                    (User.Role.ADMIN, "Admin"),
                    (User.Role.MANAGER, "Manager"),
                    (User.Role.LEAD_ATTENDENT, "Lead Attendent"),
                    (User.Role.DOCTOR, "Doctor"),
                ]
            if "hospital" in self.fields:
                del self.fields["hospital"]
            if "reports_to" in self.fields:
                self.fields["reports_to"].queryset = User.objects.filter(hospital=self.user.hospital, is_active=True)
                
        if "hospital" in self.fields:
            self.fields["hospital"].label = "Business"
            
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
                field.widget.attrs.setdefault("class", css)

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.user and self.user.hospital:
            user.hospital = self.user.hospital
        can_imp = self.cleaned_data.get("can_import_export", False)
        if not user.custom_permissions:
            user.custom_permissions = {}
        user.custom_permissions["import_export"] = can_imp
        if commit:
            user.save()
        return user


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

from .models import Hospital
class BusinessForm(forms.ModelForm):
    class Meta:
        model = Hospital
        fields = ("name", "contact_email", "phone", "address")
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")

class UserProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'phone', 'profile_picture']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'profile_picture': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
        }

    def clean_profile_picture(self):
        picture = self.cleaned_data.get('profile_picture')
        if picture:
            max_size = 5 * 1024 * 1024  # 5MB in bytes
            if picture.size > max_size:
                raise forms.ValidationError("Image file size must be less than 5 MB.")
        return picture
