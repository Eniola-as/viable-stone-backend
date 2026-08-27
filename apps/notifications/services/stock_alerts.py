"""Central low-stock / out-of-stock transition detection.

Every stock-changing workflow — sales, returns, restocks, physical stock counts
and protected adjustments — funnels through
``apps.inventory.services.stock.write_movement``, which emits the
``stock_balance_changed`` signal while the balance row is still locked. This
module is the single place that decides whether a balance change crosses into
low or out-of-stock, so all workflows behave identically:

* an alert fires only when the stock *level* changes, never repeatedly while it
  stays low or out (rule 3);
* recovering above the threshold silently resets the cycle (rule 4);
* moving from out-of-stock to a still-low quantity may raise one LOW_STOCK
  alert (rule 5);
* a direct drop from normal to zero raises only OUT_OF_STOCK (rule 2).
"""

from __future__ import annotations

from apps.notifications.models import NotificationType

OK = "OK"
LOW = "LOW"
OUT = "OUT"


def classify_stock(quantity: int, threshold: int) -> str:
    """Bucket a quantity into ``OK`` / ``LOW`` / ``OUT``.

    ``threshold`` is the variant's ``low_stock_level``; ``0`` disables the low
    band but a quantity of zero is still ``OUT``.
    """

    if quantity <= 0:
        return OUT
    if threshold and quantity <= threshold:
        return LOW
    return OK


def alert_for_transition(
    previous_quantity: int, new_quantity: int, threshold: int
) -> str | None:
    """Return the alert type for a balance change, or ``None`` for no alert."""

    before = classify_stock(previous_quantity, threshold)
    after = classify_stock(new_quantity, threshold)
    if before == after:
        return None
    if after == OUT:
        return NotificationType.OUT_OF_STOCK
    if after == LOW:
        return NotificationType.LOW_STOCK
    return None
