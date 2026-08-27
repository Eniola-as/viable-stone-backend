"""Stage 15 — central low/out-of-stock transition rules (pure logic)."""

import pytest

from apps.notifications.models import NotificationType
from apps.notifications.services.stock_alerts import (
    alert_for_transition,
    classify_stock,
)


@pytest.mark.parametrize(
    ("qty", "threshold", "expected"),
    [
        (10, 5, "OK"),
        (6, 5, "OK"),
        (5, 5, "LOW"),
        (1, 5, "LOW"),
        (0, 5, "OUT"),
        (10, 0, "OK"),  # threshold 0 -> low-stock alerting disabled
        (0, 0, "OUT"),  # reaching zero is out-of-stock regardless of threshold
    ],
)
def test_classify_stock(qty, threshold, expected):
    assert classify_stock(qty, threshold) == expected


@pytest.mark.parametrize(
    ("prev", "new", "threshold", "expected"),
    [
        (10, 4, 5, NotificationType.LOW_STOCK),  # OK -> LOW
        (10, 0, 5, NotificationType.OUT_OF_STOCK),  # OK -> OUT: only OUT, not both
        (4, 0, 5, NotificationType.OUT_OF_STOCK),  # LOW -> OUT
        (0, 3, 5, NotificationType.LOW_STOCK),  # OUT -> LOW: one new alert
        (0, 10, 5, None),  # OUT -> OK: cycle reset, no alert
        (4, 10, 5, None),  # LOW -> OK: reset
        (4, 3, 5, None),  # LOW -> LOW: no repeat while low
        (0, 0, 5, None),  # OUT -> OUT: no repeat while out
        (10, 8, 5, None),  # OK -> OK
        (3, 12, 0, None),  # threshold disabled entirely
    ],
)
def test_alert_for_transition(prev, new, threshold, expected):
    assert alert_for_transition(prev, new, threshold) == expected
