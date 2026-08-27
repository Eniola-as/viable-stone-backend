"""Session auth: login, logout, current user, password change, MFA, recovery."""

from __future__ import annotations

from axes.handlers.proxy import AxesProxyHandler
from django.contrib.auth import authenticate, update_session_auth_hash
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.middleware.csrf import get_token
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.api.serializers import (
    LoginResponseSerializer,
    LoginSerializer,
    MFASetupResponseSerializer,
    PasswordChangeSerializer,
    RecoveryCodeSerializer,
    RecoveryCodesResponseSerializer,
    TokenSerializer,
    UserSelfSerializer,
)
from apps.accounts.auth import lockout_response
from apps.accounts.models import User
from apps.accounts.services import mfa as mfa_service
from apps.accounts.services.recovery import (
    consume_recovery_code,
    generate_recovery_codes,
    unused_recovery_code_count,
)
from apps.core.authentication import enforce_csrf
from apps.core.exceptions import APIError
from apps.core.permissions import IsAuthenticatedAndMFAVerified, mfa_satisfied


def _mark_mfa_verified(request) -> None:
    request.session["mfa_verified"] = True
    request.session["mfa_verified_at"] = timezone.now().isoformat()


def _self_payload(request):
    return {
        "mfa_required": request.user.mfa_required,
        "mfa_enrolled": mfa_service.has_confirmed_totp(request.user),
        "mfa_verified": mfa_satisfied(request),
        "must_change_password": request.user.must_change_password,
        "user": UserSelfSerializer(request.user).data
        if mfa_satisfied(request)
        else None,
    }


class CSRFTokenView(APIView):
    """Bootstrap the CSRF cookie for the SPA before its first write request."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    @extend_schema(
        request=None,
        responses={204: None},
        summary="Set the CSRF cookie",
        tags=["Auth"],
        auth=[],
    )
    def get(self, request):
        get_token(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LoginView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_scope = "auth_login"

    @extend_schema(
        request=LoginSerializer,
        responses={200: LoginResponseSerializer},
        summary="Sign in with username and password",
        tags=["Auth"],
        auth=[],
    )
    def post(self, request):
        enforce_csrf(request)
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]
        password = serializer.validated_data["password"]

        credentials = {"username": username}
        if AxesProxyHandler.is_locked(request, credentials):
            return lockout_response(request, credentials)

        user = authenticate(request, username=username, password=password)
        if user is None:
            # ModelBackend returns None for a disabled account even with the
            # right password; distinguish that so the owner knows why.
            disabled = User.objects.filter(username=username, is_active=False).first()
            if disabled is not None and disabled.check_password(password):
                raise APIError(
                    "This account has been disabled. Contact the owner.",
                    code="account_disabled",
                    status_code=status.HTTP_403_FORBIDDEN,
                )
            raise APIError(
                "Incorrect username or password.",
                code="invalid_credentials",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        django_login(request, user)
        request.session["mfa_verified"] = not user.mfa_required
        return Response(_self_payload(request))


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=None, responses={204: None}, summary="Sign out", tags=["Auth"]
    )
    def post(self, request):
        django_logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticatedAndMFAVerified]

    @extend_schema(
        responses={200: UserSelfSerializer},
        summary="The signed-in user's profile",
        tags=["Auth"],
    )
    def get(self, request):
        return Response(UserSelfSerializer(request.user).data)


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=PasswordChangeSerializer,
        responses={200: LoginResponseSerializer},
        summary="Change the current user's password",
        tags=["Auth"],
    )
    def post(self, request):
        serializer = PasswordChangeSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(serializer.validated_data["current_password"]):
            raise APIError(
                "The current password is incorrect.",
                code="invalid_credentials",
                field_errors={"current_password": ["Incorrect password."]},
            )
        user.set_password(serializer.validated_data["new_password"])
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password", "updated_at"])
        # Keep the current session valid after the password (and its auth hash)
        # change — no re-login, so no backend juggling.
        update_session_auth_hash(request, user)
        return Response(_self_payload(request))


class MFASetupView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth_mfa"

    @extend_schema(
        request=None,
        responses={200: MFASetupResponseSerializer},
        summary="Begin authenticator (TOTP) enrolment",
        tags=["Auth"],
    )
    def post(self, request):
        data = mfa_service.start_totp_enrolment(request.user)
        return Response(data)


class MFASetupConfirmView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth_mfa"

    @extend_schema(
        request=TokenSerializer,
        responses={200: RecoveryCodesResponseSerializer},
        summary="Confirm authenticator enrolment and receive recovery codes",
        tags=["Auth"],
    )
    def post(self, request):
        serializer = TokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mfa_service.confirm_totp_enrolment(
            request.user, serializer.validated_data["token"]
        )
        codes = generate_recovery_codes(request.user)
        _mark_mfa_verified(request)
        return Response({"recovery_codes": codes})


class MFAVerifyView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth_mfa"

    @extend_schema(
        request=TokenSerializer,
        responses={200: LoginResponseSerializer},
        summary="Verify an authenticator code to clear MFA for this session",
        tags=["Auth"],
    )
    def post(self, request):
        serializer = TokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not mfa_service.verify_totp(
            request.user, serializer.validated_data["token"]
        ):
            raise APIError(
                "That authenticator code is not valid.",
                code="mfa_invalid_token",
                field_errors={"token": ["Invalid or expired code."]},
            )
        _mark_mfa_verified(request)
        return Response(_self_payload(request))


class RecoveryCodeVerifyView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth_recovery"

    @extend_schema(
        request=RecoveryCodeSerializer,
        responses={200: LoginResponseSerializer},
        summary="Clear MFA using a one-use recovery code",
        tags=["Auth"],
    )
    def post(self, request):
        serializer = RecoveryCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not consume_recovery_code(request.user, serializer.validated_data["code"]):
            raise APIError(
                "That recovery code is not valid or has already been used.",
                code="recovery_code_invalid",
                field_errors={"code": ["Invalid or used code."]},
            )
        _mark_mfa_verified(request)
        payload = _self_payload(request)
        payload["recovery_codes_remaining"] = unused_recovery_code_count(request.user)
        return Response(payload)


class RecoveryCodesRegenerateView(APIView):
    permission_classes = [IsAuthenticatedAndMFAVerified]
    throttle_scope = "auth_recovery"

    @extend_schema(
        request=None,
        responses={200: RecoveryCodesResponseSerializer},
        summary="Replace the current recovery codes with a fresh set",
        tags=["Auth"],
    )
    def post(self, request):
        if not mfa_service.has_confirmed_totp(request.user):
            raise APIError(
                "Enrol an authenticator before generating recovery codes.",
                code="mfa_not_enrolled",
            )
        return Response({"recovery_codes": generate_recovery_codes(request.user)})
