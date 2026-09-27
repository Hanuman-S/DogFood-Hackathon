"""Forms for signup, login, password change and token creation.

Forms validate *shape*; `accounts.services` decides what happens. So there is no `save()` on the
signup form -- the view calls the service, which is the same function the API would call.
"""

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import PasswordChangeForm as DjangoPasswordChangeForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from accounts.models import User


class SignupForm(forms.Form):
    email = forms.EmailField(
        label="Email",
        help_text="Used to sign in.",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )
    display_name = forms.CharField(
        label="Display name",
        max_length=120,
        help_text="Shown in the gallery next to your team's projects.",
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise ValidationError("The two passwords do not match.")
        return password2

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password1")
        if password:
            # Django's configured validators: length, commonness, not all-numeric, and not too
            # similar to the user's own attributes. The unsaved instance is passed so the
            # similarity check has an email and a name to compare against.
            candidate = User(
                email=cleaned.get("email") or "",
                display_name=cleaned.get("display_name") or "",
            )
            try:
                validate_password(password, user=candidate)
            except ValidationError as error:
                self.add_error("password1", error)
        return cleaned


class LoginForm(forms.Form):
    """Email and password. No "remember me": session lifetime is a deployment setting, not a
    per-login choice, and offering the box without honouring it would be worse than omitting it."""

    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class PasswordChangeForm(DjangoPasswordChangeForm):
    """Django's form, which already requires the current password and runs the validators."""


class TokenCreateForm(forms.Form):
    name = forms.CharField(
        label="Token name",
        max_length=120,
        help_text="What this token is for, e.g. 'acceptance checker' or 'my laptop'.",
        widget=forms.TextInput(attrs={"placeholder": "my laptop"}),
    )

    def clean_name(self):
        return self.cleaned_data["name"].strip()
