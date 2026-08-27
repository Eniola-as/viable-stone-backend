from django.urls import path

from apps.accounts.api.auth_views import (
    CSRFTokenView,
    CurrentUserView,
    LoginView,
    LogoutView,
    MFASetupConfirmView,
    MFASetupView,
    MFAVerifyView,
    PasswordChangeView,
    RecoveryCodesRegenerateView,
    RecoveryCodeVerifyView,
)

app_name = "auth"

urlpatterns = [
    path("csrf/", CSRFTokenView.as_view(), name="csrf"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", CurrentUserView.as_view(), name="me"),
    path("password/change/", PasswordChangeView.as_view(), name="password-change"),
    path("mfa/setup/", MFASetupView.as_view(), name="mfa-setup"),
    path("mfa/setup/confirm/", MFASetupConfirmView.as_view(), name="mfa-setup-confirm"),
    path("mfa/verify/", MFAVerifyView.as_view(), name="mfa-verify"),
    path("mfa/recovery/", RecoveryCodeVerifyView.as_view(), name="mfa-recovery"),
    path(
        "mfa/recovery-codes/regenerate/",
        RecoveryCodesRegenerateView.as_view(),
        name="mfa-recovery-regenerate",
    ),
]
