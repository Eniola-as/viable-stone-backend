"""Inventory domain signals.

``stock_balance_changed`` is emitted by
:func:`apps.inventory.services.stock.write_movement` — the single primitive
every stock-changing workflow funnels through — while the balance row is still
locked. Listeners (currently low/out-of-stock notifications) get a consistent,
serialised view of the transition regardless of which workflow caused it.

Signal kwargs:
    branch, variant, previous_quantity, new_quantity, movement, actor
"""

import django.dispatch

stock_balance_changed = django.dispatch.Signal()
