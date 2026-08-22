"""One month against the one before it.

The gap this closed: a single closed month says what happened and never whether
it is getting better or worse, which is the question an owner actually has.

The assertion that matters most here is the direction rule. Fuel going up is
bad and revenue going up is good, and getting that backwards is how a dashboard
congratulates a firm for burning more diesel.
"""
from __future__ import annotations

import pytest

from archon.domain.models import Statements
from archon.domain.trends import MATERIAL_PCT, compare, narrate


def month(period: str, **overrides) -> Statements:
    base = {
        "period": period, "revenue_linehaul": 10_000.0,
        "revenue_accessorial": 0.0, "revenue": 10_000.0, "fuel": 3_000.0,
        "tolls": 200.0, "maintenance": 500.0, "insurance": 1_000.0,
        "driver_pay": 3_000.0, "factoring_fees": 300.0,
        "operating_expenses": 8_000.0, "net_profit": 2_000.0,
        "cash_in": 9_000.0, "cash_out": 8_000.0, "net_cash": 1_000.0,
        "accounts_receivable": 0.0, "accounts_payable": 0.0,
        "total_miles": 5_000.0, "cost_per_mile": 1.6,
        "revenue_per_mile": 2.0,
    }
    base.update(overrides)
    return Statements(**base)


# ── the direction rule ───────────────────────────────────────────────────────

def test_revenue_rising_is_better():
    result = compare(month("2026-06"), month("2026-07", revenue=12_000.0))

    assert result.find("revenue").direction == "better"
    assert result.find("revenue").change == 2_000.0
    assert result.find("revenue").change_pct == 20.0


def test_fuel_rising_is_worse():
    """The one a naive dashboard gets backwards."""
    assert compare(month("2026-06"), month("2026-07", fuel=4_000.0)) \
        .find("fuel").direction == "worse"


def test_cost_per_mile_rising_is_worse_and_revenue_per_mile_rising_is_better():
    result = compare(month("2026-06"),
                     month("2026-07", cost_per_mile=1.9, revenue_per_mile=2.4))

    assert result.find("cost_per_mile").direction == "worse"
    assert result.find("revenue_per_mile").direction == "better"


@pytest.mark.parametrize("revenue, expected", [
    (10_150.0, "flat"),      # 1.5%, under the material threshold
    (10_250.0, "better"),    # 2.5%, over it
])
def test_a_move_under_the_threshold_is_flat_rather_than_dressed_up(revenue, expected):
    assert compare(month("2026-06"), month("2026-07", revenue=revenue)) \
        .find("revenue").direction == expected


def test_the_threshold_is_the_shipped_one():
    """The fixtures above are chosen against this value, so a change to it must
    break something rather than silently reinterpret every comparison."""
    assert MATERIAL_PCT == 2.0


# ── the arithmetic that must not lie ─────────────────────────────────────────

def test_a_percentage_against_zero_is_not_reported():
    """Dividing by a month that earned nothing produces an infinity, and an
    infinity on a dashboard is worse than a blank."""
    movement = compare(month("2026-06", revenue=0.0), month("2026-07")) \
        .find("revenue")

    assert movement.change == 10_000.0
    assert movement.change_pct is None
    assert movement.direction == "unknown"


def test_a_metric_missing_from_either_month_is_unknown_not_zero():
    movement = compare(month("2026-06", cost_per_mile=None), month("2026-07")) \
        .find("cost_per_mile")

    assert movement.direction == "unknown"
    assert movement.change is None


def test_every_tracked_metric_appears_exactly_once():
    result = compare(month("2026-06"), month("2026-07"))
    metrics = [m.metric for m in result.movements]

    assert len(metrics) == len(set(metrics))
    for expected in ("revenue", "operating_expenses", "net_profit", "fuel",
                     "driver_pay", "total_miles", "cost_per_mile", "revenue_per_mile"):
        assert expected in metrics


# ── what gets put in front of the owner ─────────────────────────────────────

def test_what_got_worse_is_listed_before_what_got_better():
    result = compare(month("2026-06"),
                     month("2026-07", revenue=12_000.0, fuel=4_500.0))

    assert result.notable[0].direction == "worse"
    assert result.notable[0].metric == "fuel"


def test_nothing_material_says_so_plainly():
    assert "Nothing moved materially" in narrate(compare(month("2026-06"), month("2026-07")))


def test_the_narrative_names_the_moves_and_the_margin():
    text = narrate(compare(month("2026-06"),
                           month("2026-07", fuel=4_500.0, cost_per_mile=1.9)))

    assert "fuel up 50%" in text
    assert "a mile" in text


def test_the_margin_is_reported_as_widening_or_narrowing():
    narrowing = narrate(compare(month("2026-06"), month("2026-07", cost_per_mile=1.9)))
    widening = narrate(compare(month("2026-06"), month("2026-07", revenue_per_mile=2.6)))

    assert "narrowed" in narrowing
    assert "widened" in widening


def test_the_comparison_serialises_for_the_page():
    payload = compare(month("2026-06"), month("2026-07", fuel=4_000.0)).to_dict()

    assert payload["previous_period"] == "2026-06"
    assert payload["current_period"] == "2026-07"
    assert any(m["metric"] == "fuel" for m in payload["movements"])
    assert payload["notable"][0]["metric"] == "fuel"


# ── against the real corpus ─────────────────────────────────────────────────

def test_june_and_july_compare_on_the_bundled_months():
    """Both months are committed data, so this is a real comparison rather than
    a fixture agreeing with itself."""
    from archon.adapters.store import LocalStore
    from archon.runtime.close import run_close
    from archon.runtime.mailbox import read_period

    closes = {}
    for period in ("2026-06", "2026-07"):
        documents, raw = read_period(period)
        closes[period] = run_close(period=period, documents=documents,
                                   company="Bell Ridge Haulage", store=LocalStore(),
                                   raw_texts=raw)

    result = compare(closes["2026-06"].statements, closes["2026-07"].statements)

    assert result.previous_period == "2026-06"
    assert result.current_period == "2026-07"
    assert result.notable, "two deliberately different months should move something"
    assert narrate(result).startswith("Against 2026-06:")
