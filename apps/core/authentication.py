from django.middleware.csrf import CsrfViewMiddleware
from rest_framework.authentication import SessionAuthentication

from apps.core.exceptions import APIError


class CSRFSessionAuthentication(SessionAuthentication):
    """Session authentication with CSRF enforced for authenticated requests.

    DRF's default already enforces CSRF once a session user is found; this
    subclass exists so the behaviour is explicit and discoverable.

    ``authenticate_header`` is defined so that a request with no valid session
    gets a 401 ("not_authenticated") rather than DRF's default 403 — 403 is
    reserved for an authenticated user who lacks permission.
    """

    def enforce_csrf(self, request):  # pragma: no cover - thin passthrough
        return super().enforce_csrf(request)

    def authenticate_header(self, request):
        return "Session"


class _ForceCsrfCheck(CsrfViewMiddleware):
    def _reject(self, request, reason):
        return reason


def enforce_csrf(request) -> None:
    """Run Django's CSRF check on an otherwise CSRF-exempt DRF view.

    Used by unauthenticated state-changing endpoints (login, MFA, recovery)
    where ``SessionAuthentication`` would not trigger the check itself.
    """

    reason = _ForceCsrfCheck(lambda req: None).process_view(request, None, (), {})
    if reason:
        raise APIError(
            "CSRF verification failed. Refresh and try again.",
            code="csrf_failed",
            status_code=403,
        )
