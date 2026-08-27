"""Helpers for exercising TOTP in tests."""

from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice


def current_token(device: TOTPDevice) -> str:
    value = totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)
    return f"{value:0{device.digits}d}"


def latest_device(user, *, confirmed=None) -> TOTPDevice:
    qs = TOTPDevice.objects.filter(user=user)
    if confirmed is not None:
        qs = qs.filter(confirmed=confirmed)
    return qs.order_by("-id").first()
