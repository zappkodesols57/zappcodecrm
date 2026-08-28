import re
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
            allowed = self.user.hospital.get_allowed_roles()
            if self.user.role == User.Role.MANAGER:
                allowed = [r for r in allowed if r not in [User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER]]
            self.fields["role"].choices = [(r, dict(User.Role.choices).get(r, r)) for r in allowed]
            if "hospital" in self.fields:
                del self.fields["hospital"]
            if "reports_to" in self.fields:
                self.fields["reports_to"].queryset = User.objects.filter(hospital=self.user.hospital, is_active=True)
        
        if "reports_to" in self.fields:
            self.fields["reports_to"].empty_label = "Select Reporting Manager"
            self.fields["reports_to"].widget.attrs.update({
                "placeholder": "Select Reporting Manager",
                "data-placeholder": "Select Reporting Manager",
            })

        if "hospital" in self.fields:
            self.fields["hospital"].label = "Business"
            
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
                field.widget.attrs.setdefault("class", css)

        if "phone" in self.fields:
            self.fields["phone"].widget.attrs.update({
                "maxlength": "10",
                "minlength": "10",
                "pattern": "^[0-9]{10}$",
                "inputmode": "numeric",
                "placeholder": "10-digit mobile number",
                "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            })

        if "email" in self.fields:
            self.fields["email"].widget = forms.EmailInput(attrs={
                "class": "form-control",
                "placeholder": "name@example.com",
                "pattern": r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
                "title": "Please enter a valid email address containing '@' (e.g. name@example.com)",
            })

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if email:
            if "@" not in email or not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email):
                raise forms.ValidationError("Please enter a valid email address with '@' (e.g. name@example.com).")
        return email

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "")
        if phone:
            digits = re.sub(r"\D", "", str(phone))
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10:
                raise forms.ValidationError("Mobile number must be exactly 10 digits.")
            return digits
        return phone

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
            allowed = self.user.hospital.get_allowed_roles()
            if self.user.role == User.Role.MANAGER:
                allowed = [r for r in allowed if r not in [User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER]]
            self.fields["role"].choices = [(r, dict(User.Role.choices).get(r, r)) for r in allowed]
            if "hospital" in self.fields:
                del self.fields["hospital"]
            if "reports_to" in self.fields:
                self.fields["reports_to"].queryset = User.objects.filter(hospital=self.user.hospital, is_active=True)
                
        if "reports_to" in self.fields:
            self.fields["reports_to"].empty_label = "Select Reporting Manager"
            self.fields["reports_to"].widget.attrs.update({
                "placeholder": "Select Reporting Manager",
                "data-placeholder": "Select Reporting Manager",
            })

        if "hospital" in self.fields:
            self.fields["hospital"].label = "Business"
            
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
                field.widget.attrs.setdefault("class", css)

        if "phone" in self.fields:
            self.fields["phone"].widget.attrs.update({
                "maxlength": "10",
                "minlength": "10",
                "pattern": "^[0-9]{10}$",
                "inputmode": "numeric",
                "placeholder": "10-digit mobile number",
                "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            })

        if "email" in self.fields:
            self.fields["email"].widget = forms.EmailInput(attrs={
                "class": "form-control",
                "placeholder": "name@example.com",
                "pattern": r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
                "title": "Please enter a valid email address containing '@' (e.g. name@example.com)",
            })

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if email:
            if "@" not in email or not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email):
                raise forms.ValidationError("Please enter a valid email address with '@' (e.g. name@example.com).")
        return email

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "")
        if phone:
            digits = re.sub(r"\D", "", str(phone))
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10:
                raise forms.ValidationError("Mobile number must be exactly 10 digits.")
            return digits
        return phone

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
        (User.Role.LEAD_ATTENDENT, "Lead Attendant"),
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
        if "phone" in self.fields:
            self.fields["phone"].widget.attrs.update({
                "maxlength": "10",
                "minlength": "10",
                "pattern": "^[0-9]{10}$",
                "inputmode": "numeric",
                "placeholder": "10-digit mobile number",
                "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            })

        if "email" in self.fields:
            self.fields["email"].widget = forms.EmailInput(attrs={
                "class": "form-control",
                "placeholder": "name@example.com",
                "pattern": r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
                "title": "Please enter a valid email address containing '@' (e.g. name@example.com)",
            })

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if email:
            if "@" not in email or not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email):
                raise forms.ValidationError("Please enter a valid email address with '@' (e.g. name@example.com).")
        return email

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "")
        if phone:
            digits = re.sub(r"\D", "", str(phone))
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10:
                raise forms.ValidationError("Mobile number must be exactly 10 digits.")
            return digits
        return phone


from .models import Hospital
class BusinessForm(forms.ModelForm):
    allowed_roles = forms.MultipleChoiceField(
        choices=User.Role.choices,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Allowed Roles for this Business"
    )

    class Meta:
        model = Hospital
        fields = ("name", "contact_email", "phone", "address", "allowed_roles", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.allowed_roles:
            self.initial["allowed_roles"] = self.instance.get_allowed_roles()
        else:
            self.initial["allowed_roles"] = [User.Role.ADMIN, User.Role.MANAGER, User.Role.LEAD_ATTENDENT, User.Role.DOCTOR]

        for name, field in self.fields.items():
            if name != "allowed_roles" and not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-control")
        if "phone" in self.fields:
            self.fields["phone"].widget.attrs.update({
                "maxlength": "10",
                "minlength": "10",
                "pattern": "^[0-9]{10}$",
                "inputmode": "numeric",
                "placeholder": "10-digit phone number",
                "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            })
        if "contact_email" in self.fields:
            self.fields["contact_email"].widget = forms.EmailInput(attrs={
                "class": "form-control",
                "placeholder": "contact@business.com",
                "pattern": r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
                "title": "Please enter a valid email address containing '@' (e.g. contact@business.com)",
            })

    def clean_contact_email(self):
        email = (self.cleaned_data.get("contact_email") or "").strip()
        if email:
            if "@" not in email or not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email):
                raise forms.ValidationError("Please enter a valid contact email with '@' (e.g. contact@business.com).")
        return email

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "")
        if phone:
            digits = re.sub(r"\D", "", str(phone))
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10:
                raise forms.ValidationError("Phone number must be exactly 10 digits.")
            return digits
        return phone


class UserProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'phone', 'profile_picture']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={
                'class': 'form-control',
                'placeholder': 'name@example.com',
                'pattern': r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
                'title': "Please enter a valid email address containing '@' (e.g. name@example.com)",
            }),
            'phone': forms.TextInput(attrs={
                'class': 'form-control',
                'maxlength': '10',
                'minlength': '10',
                'pattern': '^[0-9]{10}$',
                'inputmode': 'numeric',
                'placeholder': '10-digit mobile number',
                'oninput': "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            }),
            'profile_picture': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
        }

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if email:
            if "@" not in email or not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email):
                raise forms.ValidationError("Please enter a valid email address with '@' (e.g. name@example.com).")
        return email

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "")
        if phone:
            digits = re.sub(r"\D", "", str(phone))
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10:
                raise forms.ValidationError("Mobile number must be exactly 10 digits.")
            return digits
        return phone

    def clean_profile_picture(self):
        picture = self.cleaned_data.get('profile_picture')
        if picture:
            # 1. Size Validation (Max 5MB)
            max_size = 5 * 1024 * 1024  # 5MB in bytes
            if picture.size > max_size:
                raise forms.ValidationError("Image file size must be less than 5 MB. Please choose a smaller photo.")
            
            # 2. File Type / Extension Validation
            valid_extensions = ('.jpg', '.jpeg', '.png', '.webp')
            import os
            ext = os.path.splitext(picture.name)[1].lower()
            if ext not in valid_extensions:
                raise forms.ValidationError("Invalid image format! Only JPG, JPEG, PNG, and WEBP formats are allowed.")
                
            # 3. Content Type Validation
            if hasattr(picture, 'content_type') and picture.content_type:
                valid_content_types = ['image/jpeg', 'image/png', 'image/webp', 'image/jpg', 'image/pjpeg']
                if picture.content_type.lower() not in valid_content_types:
                    raise forms.ValidationError("Invalid file type! Please upload a valid image file.")
        return picture
