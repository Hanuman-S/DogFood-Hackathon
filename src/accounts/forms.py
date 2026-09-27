from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import PasswordChangeForm as DjangoPasswordChangeForm

from accounts.models import User, normalize_email


class LoginForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={"autocomplete": "username", "placeholder": "the email you signed up with", "autofocus": True}
        )
    )
    password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "current-password", "placeholder": "your password"}
        ),
    )
    remember = forms.BooleanField(required=False, label="remember this terminal for 14 days")


class _NewPasswordMixin:
    """Two password fields, checked against each other and Django's validators."""

    def _check_new_password(self, user):
        p1 = self.cleaned_data.get("password1")
        p2 = self.cleaned_data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The two passwords do not match.")
        elif p1:
            try:
                password_validation.validate_password(p1, user)
            except forms.ValidationError as error:
                self.add_error("password1", error)


class SignupForm(_NewPasswordMixin, forms.Form):
    duplicate_email_message = "An account with this email already exists. Log in instead."

    name = forms.CharField(
        max_length=120,
        widget=forms.TextInput(
            attrs={"autocomplete": "name", "placeholder": "your name as teammates should see it, e.g. Ada Lovelace", "autofocus": True}
        ),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "e.g. ada@example.org"})
    )
    password1 = forms.CharField(
        label="password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "placeholder": "at least 10 characters"}),
        help_text="at least 10 characters, not all digits, not a common password",
    )
    password2 = forms.CharField(
        label="password (again)",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "placeholder": "type it again"}),
    )

    def clean_email(self):
        email = normalize_email(self.cleaned_data["email"])
        # Trade-off, stated plainly: this reveals that an address is registered. The
        # alternative ("check your inbox") needs outbound email, which an offline portal does
        # not have. Login itself never reveals it.
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError(self.duplicate_email_message)
        return email

    def clean(self):
        cleaned = super().clean()
        probe = User(email=cleaned.get("email", ""), name=cleaned.get("name", ""))
        self._check_new_password(probe)
        return cleaned


class AccountCreateForm(SignupForm):
    """Used by an admin to create an account for someone else.

    Only the two platform-wide powers are set here. Judge and organizer are roles *in an event*
    and are granted from that event's control page.
    """

    duplicate_email_message = "An account with this email already exists."
    can_create_events = forms.BooleanField(
        required=False, label="may create events (becomes their organizer)"
    )
    is_platform_admin = forms.BooleanField(required=False, label="platform admin")

    field_order = ["name", "email", "password1", "password2", "can_create_events", "is_platform_admin"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # This form sits below other content on the admin page; do not steal focus.
        self.fields["name"].widget.attrs.pop("autofocus", None)


class PasswordChangeForm(DjangoPasswordChangeForm):
    """Django's form (checks the old password, runs the validators), relabelled for the UI."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["old_password"].label = "current password"
        self.fields["new_password1"].label = "new password"
        self.fields["new_password2"].label = "new password (again)"
        self.fields["old_password"].widget.attrs.pop("autofocus", None)
        self.fields["new_password1"].help_text = "at least 10 characters, not all digits"
        self.fields["new_password2"].help_text = ""
        self.fields["old_password"].widget.attrs["placeholder"] = "your current password"
        self.fields["new_password1"].widget.attrs["placeholder"] = "at least 10 characters"
        self.fields["new_password2"].widget.attrs["placeholder"] = "type it again"


class TokenForm(forms.Form):
    name = forms.CharField(
        max_length=60,
        widget=forms.TextInput(attrs={"placeholder": "what it is for, e.g. laptop-scripts"}),
    )
