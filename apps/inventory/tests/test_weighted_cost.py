from decimal import Decimal

import pytest

from apps.core.money import to_money, weighted_average_cost


@pytest.mark.parametrize(
    ("old_q", "old_avg", "recv_q", "recv_cost", "expected"),
    [
        (0, "0", 100, "1500.00", "1500.00"),
        (100, "1500.00", 50, "1800.00", "1600.00"),
        (150, "1600.00", 30, "1750.50", "1625.08"),  # 292515 / 180 -> HALF_UP
        (3, "1000.00", 1, "1000.01", "1000.00"),  # 4000.01/4 = 1000.0025 -> 1000.00
        (1, "999.99", 1, "1000.00", "1000.00"),  # 1999.99/2 = 999.995 -> 1000.00
    ],
)
def test_weighted_average_matches_blueprint_formula(
    old_q, old_avg, recv_q, recv_cost, expected
):
    result = weighted_average_cost(
        old_quantity=old_q,
        old_average=Decimal(old_avg),
        received_quantity=recv_q,
        received_unit_cost=Decimal(recv_cost),
    )
    assert result == Decimal(expected)
    assert result.as_tuple().exponent == -2  # always two decimal places


def test_to_money_rounds_half_up():
    assert to_money("2.005") == Decimal("2.01")
    assert to_money(Decimal("2.004")) == Decimal("2.00")


def test_zero_total_quantity_is_zero():
    assert weighted_average_cost(
        old_quantity=0,
        old_average=Decimal("0"),
        received_quantity=0,
        received_unit_cost=Decimal("0"),
    ) == Decimal("0.00")
