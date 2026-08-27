"""TOTP enrolment and verification, built on django-otp."""

from __future__ import annotations

import base64
import binascii

from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core.exceptions import APIError

_DEVICE_NAME = "primary"


def _b32_secret(device: TOTPDevice) -> str:
    return base64.b32encode(binascii.unhexlify(device.key)).decode("ascii")


def start_totp_enrolment(user) -> dict:
    """Create (or reuse) an unconfirmed TOTP device and return provisioning data."""

    device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
    if device is None:
        device = TOTPDevice.objects.create(
            user=user, name=_DEVICE_NAME, confirmed=False
        )
    return {
        "secret": _b32_secret(device),
        "otpauth_url": device.config_url,
    }


def confirm_totp_enrolment(user, token: str) -> None:
    device = (
        TOTPDevice.objects.filter(user=user, confirmed=False).order_by("-id").first()
    )
    if device is None:
        raise APIError(
            "There is no pending authenticator to confirm.",
            code="mfa_not_pending",
        )
    if not device.verify_token(token):
        raise APIError(
            "That authenticator code is not valid.",
            code="mfa_invalid_token",
            field_errors={"token": ["Invalid or expired code."]},
        )
    TOTPDevice.objects.filter(user=user, confirmed=True).delete()
    device.confirmed = True
    device.name = _DEVICE_NAME
    device.save(update_fields=["confirmed", "name"])


def has_confirmed_totp(user) -> bool:
    return TOTPDevice.objects.filter(user=user, confirmed=True).exists()


def verify_totp(user, token: str) -> bool:
    device = TOTPDevice.objects.filter(user=user, confirmed=True).first()
    if device is None:
        return False
    return bool(device.verify_token(token))


def reset_totp(user) -> None:
    TOTPDevice.objects.filter(user=user).delete()
