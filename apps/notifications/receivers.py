"""Signal receivers that turn domain events into notifications."""

from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.inventory.signals import stock_balance_changed
from apps.notifications.action_paths import action_path
from apps.notifications.models import NotificationType
from apps.notifications.selectors import active_branch_owners
from apps.notifications.services.dispatch import enqueue
from apps.notifications.services.stock_alerts import alert_for_transition

logger = logging.getLogger("apps.notifications")


@receiver(
    stock_balance_changed,
    dispatch_uid="notifications.on_stock_balance_changed",
)
def on_stock_balance_changed(
    sender,
    *,
    branch,
    variant,
    previous_quantity,
    new_quantity,
    movement,
    actor=None,
    **_kwargs,
):
    threshold = variant.low_stock_level or 0
    alert = alert_for_transition(previous_quantity, new_quantity, threshold)
    if alert is None:
        return

    sku = variant.sku
    if alert == NotificationType.OUT_OF_STOCK:
        title = f"Out of stock: {sku}"
        message = f"{sku} has reached zero units and needs restocking."
    else:
        title = f"Low stock: {sku}"
        message = (
            f"{sku} has fallen to {new_quantity} unit(s), at or below its "
            f"low-stock level of {threshold}."
        )

    enqueue(
        recipients=lambda: active_branch_owners(branch),
        branch=branch,
        notification_type=alert,
        title=title,
        message=message,
        # One crossing == one movement, so this key is unique per episode and
        # makes re-delivery idempotent.
        dedupe_key=f"{alert}:{movement.id}",
        action_path=action_path("inventory_variant", variant_id=variant.id),
        related_object_type="inventory_variant",
        related_object_id=variant.id,
    )
