"""One-use MFA recovery codes. Raw codes are shown once and never stored."""

from __future__ import annotations

import secrets

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone

from apps.accounts.models import RecoveryCode

RECOVERY_CODE_COUNT = 10
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no easily-confused characters


def _new_code() -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"{body[:4]}-{body[4:8]}-{body[8:]}"


def generate_recovery_codes(user, *, count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Replace ``user``'s recovery codes with a fresh set; return the raw codes."""

    RecoveryCode.objects.filter(user=user).delete()
    raw_codes: list[str] = []
    to_create: list[RecoveryCode] = []
    for _ in range(count):
        code = _new_code()
        raw_codes.append(code)
        to_create.append(RecoveryCode(user=user, code_hash=make_password(code)))
    RecoveryCode.objects.bulk_create(to_create)
    return raw_codes


def consume_recovery_code(user, raw_code: str) -> bool:
    """Verify and burn a recovery code. Returns True on success."""

    candidate = (raw_code or "").strip().lower().replace(" ", "")
    if not candidate:
        return False
    for record in user.recovery_codes.filter(used_at__isnull=True):
        if check_password(candidate, record.code_hash):
            record.used_at = timezone.now()
            record.save(update_fields=["used_at"])
            return True
    return False


def unused_recovery_code_count(user) -> int:
    return user.recovery_codes.filter(used_at__isnull=True).count()
