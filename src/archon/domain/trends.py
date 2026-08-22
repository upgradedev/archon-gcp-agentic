"""One month against the one before it.

A single closed month tells an owner what happened. It does not tell them
whether it is getting better or worse, and that is the question they actually
have. Fuel at 6,698 means nothing on its own; fuel at 6,698 after 5,900, on
fewer miles, means something and it means it immediately.

The comparison is deliberately narrow. It compares **closed periods only**, and
it compares figures the ledger produced, never anything re-derived here. A
trend module that recomputes its own totals is a second source of truth, and
two sources of truth for one number is how a product ends up contradicting
itself on two screens.

Pure and deterministic: it takes statements in and returns arithmetic. No
model, no clock, no store.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Statements

#: A change smaller than this is noise from rounding and a different mix of
#: loads, not a signal. Below it, direction is reported as "flat" rather than
#: dressed up as an improvement.
MATERIAL_PCT = 2.0

#: Metrics where going up is bad. Everything not here reads the other way, and
#: getting this backwards is how a dashboard congratulates a firm for burning
#: more diesel.
LOWER_IS_BETTER = frozenset({
    "fuel", "tolls", "maintenance", "insurance", "driver_pay",
    "factoring_fees", "operating_expenses", "cost_per_mile",
})


@dataclass
class Movement:
    """One metric, across two periods."""

    metric: str
    label: str
    previous: float | None
    current: float | None
    change: float | None
    change_pct: float | None
    direction: str            # "better" | "worse" | "flat" | "unknown"
    unit: str                 # "money" | "rate" | "count"

    @property
    def material(self) -> bool:
        return self.change_pct is not None and abs(self.change_pct) >= MATERIAL_PCT


@dataclass
class Comparison:
    """Two closed periods, and what moved between them."""

    previous_period: str
    current_period: str
    movements: list[Movement]

    def find(self, metric: str) -> Movement | None:
        return next((m for m in self.movements if m.metric == metric), None)

    @property
    def notable(self) -> list[Movement]:
        """What is worth a sentence, worst first."""
        moved = [m for m in self.movements if m.material]
        return sorted(moved, key=lambda m: (m.direction != "worse", -abs(m.change_pct or 0)))

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return {
            "previous_period": self.previous_period,
            "current_period": self.current_period,
            "movements": [asdict(m) for m in self.movements],
            "notable": [asdict(m) for m in self.notable],
        }


#: The metrics worth tracking, and what to call them in front of an owner.
TRACKED = [
    ("revenue", "Revenue", "money"),
    ("operating_expenses", "Operating costs", "money"),
    ("net_profit", "Net profit", "money"),
    ("fuel", "Fuel", "money"),
    ("driver_pay", "Driver pay", "money"),
    ("maintenance", "Maintenance", "money"),
    ("factoring_fees", "Factoring fees", "money"),
    ("total_miles", "Miles run", "count"),
    ("revenue_per_mile", "Revenue per mile", "rate"),
    ("cost_per_mile", "Cost per mile", "rate"),
]


def _direction(metric: str, change: float | None, change_pct: float | None) -> str:
    if change is None or change_pct is None:
        return "unknown"
    if abs(change_pct) < MATERIAL_PCT:
        return "flat"
    rose = change > 0
    if metric in LOWER_IS_BETTER:
        return "worse" if rose else "better"
    return "better" if rose else "worse"


def compare(previous: Statements, current: Statements) -> Comparison:
    """What moved between two closed months."""
    movements: list[Movement] = []

    for metric, label, unit in TRACKED:
        before = getattr(previous, metric, None)
        after = getattr(current, metric, None)

        if before is None or after is None:
            movements.append(Movement(metric, label, before, after, None, None,
                                      "unknown", unit))
            continue

        change = round(after - before, 3)
        # A percentage against zero is not a percentage. Report the absolute
        # move and leave the ratio out rather than printing an infinity.
        change_pct = round(change / abs(before) * 100, 1) if before else None
        movements.append(Movement(metric, label, round(before, 3), round(after, 3),
                                  change, change_pct,
                                  _direction(metric, change, change_pct), unit))

    return Comparison(previous_period=previous.period,
                      current_period=current.period,
                      movements=movements)


def narrate(comparison: Comparison) -> str:
    """One or two plain sentences about the direction of travel.

    Deterministic, like every other sentence this product writes without a
    model. It names the biggest real move and says whether the firm is earning
    more per mile than it spends.
    """
    notable = comparison.notable
    if not notable:
        return (f"Nothing moved materially between {comparison.previous_period} and "
                f"{comparison.current_period}.")

    parts = []
    for movement in notable[:3]:
        arrow = "up" if (movement.change or 0) > 0 else "down"
        parts.append(f"{movement.label.lower()} {arrow} {abs(movement.change_pct):.0f}%")

    lead = (f"Against {comparison.previous_period}: " + ", ".join(parts) + ".")

    margin_now = comparison.find("revenue_per_mile"), comparison.find("cost_per_mile")
    if all(m and m.current is not None for m in margin_now):
        earned, spent = margin_now[0].current, margin_now[1].current
        was = (margin_now[0].previous or 0) - (margin_now[1].previous or 0)
        now = round(earned - spent, 3)
        moved = "widened" if now > was else ("narrowed" if now < was else "held")
        lead += (f" The margin {moved} to {now:,.3f} a mile, from {was:,.3f}.")

    return lead
