"""The headline comparative number, asserted rather than written down.

The README says three ordinary reconciliation methods recover none of the money
this product finds. That is the one claim a reader is entitled to disbelieve
hardest, so it is computed from the shipped corpus every time the suite runs,
and the figures in the README are asserted against it here.

The baselines are deliberately implemented at their strongest. A straw man
would prove nothing, and the interesting result is not that a weak method fails
but that the ARITHMETIC A CAREFUL BOOKKEEPER DOES BY HAND reconciles to a zero
residual while two hundred dollars is missing.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from baseline import baselines, month  # noqa: E402


def test_a_bank_feed_matches_nothing():
    """One credit of 18,667.65 against nine invoices, none of which is that."""
    assert baselines(month())["a_matched"] == 0


def test_the_arithmetic_by_hand_reconciles_while_the_money_is_gone():
    """The finding that makes the product worth building.

    Credit plus the fee equals the advice total exactly, so the check a careful
    bookkeeper runs comes back clean. The short payment is inside a line that
    the advice itself reports, and the batch still ties.
    """
    b = baselines(month())

    assert b["b_residual"] == 0.0, "the batch no longer reconciles; the demo month changed"
    assert b["short_total"] == 200.0


def test_the_line_check_flags_the_wrong_load():
    """Comparing each advice line to the agreed linehaul raises one flag, and it
    is a load the broker paid ABOVE the rate. It is not the loss, and chasing it
    would cost the owner a relationship over nothing."""
    b = baselines(month())

    assert b["c_flagged"] == ["L-7102"]
    assert [ref for ref, _ in b["short"]] == ["L-7105"]


def test_the_readme_quotes_these_figures():
    """A comparative number in a README that no command produces is a claim."""
    b = baselines(month())
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "scripts/baseline.py" in readme, "the README must name the command"
    for figure in (f"{b['short_total']:,.2f}", f"{b['credited']:,.2f}", f"{b['fee']:,.2f}"):
        assert figure in readme, f"{figure} is not in the README"
