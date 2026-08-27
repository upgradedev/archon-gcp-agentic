"""Independent attempt to REFUTE the reproduction of audit claim 1.4.

The prior agent's repro used hand-built `Document` objects for three of its
four currency sub-claims. The obvious refutation is "those objects are not
reachable from any real intake path". That refutation FAILS, and this file is
the evidence: every currency test below starts from BYTES ON DISK, goes through
`mailbox.read_period` -> `extract.extract_document` -> `runtime.close.run_close`
-> `drafts.draft_for_decisions`, which is the exact production chain (the GCS
mailbox differs only in where the bytes come from; `read_period` takes a `root`).

Not one `Document(...)` is constructed by hand in any `test_repro_*` here.

What survived the refutation attempt:

    (b) currency is not handled explicitly   -- REAL, and STILL real in the one
                                                artifact that leaves the
                                                building; extraction was fixed,
                                                the outbound letter was not
    (c) two currencies can be summed         -- WAS real, silently, end to end;
                                                gate G7 now refuses such a month

What did not:

    (a) money is float                       -- true and inert; see the two
                                                `test_documents_float_*` tests
    (d) rounding is not deterministic        -- FALSE; it is deterministic

Naming follows the repo's convention: `test_repro_*` FAILS on this commit
because the defect is real; `test_documents_*` PASSES and pins behaviour. Exactly
one `test_repro_*` is left. `extract._currency` now reads the currency a document
actually states, so a EUR payment is recorded as EUR, but `drafts._money` still
prints every figure as USD and no call site overrides it. The finding and the
letter drafted from it now disagree inside a single close, which is precisely
what the old accidental agreement between the two halves used to hide.
"""
from __future__ import annotations

from pathlib import Path

from archon.adapters.store import LocalStore
from archon.domain import policy as policy_mod
from archon.domain.ledger import Ledger
from archon.domain.models import DocType, ExceptionKind
from archon.runtime.close import run_close
from archon.runtime.mailbox import read_period
from tests.conftest import PERIOD

# ── real artifacts, in the exact shape the bundled corpus writes them ─────────

#: Copied field-for-field from corpus/2026-07/bank-2026-07-28-5.txt, with two
#: changes a European bookkeeper's bank would make: a `Currency:` line and an
#: amount in continental notation. Nothing else is invented.
BANK_OUT_EUR = """FIRST PLAINS BANK
ACCOUNT ACTIVITY

Document Type: Bank Transaction
Date: 2026-07-28
Direction: out
Currency: EUR
Amount: 1.865,00 EUR
Reference: KG-9001
Paid To: Kastro Garage
"""

#: The same invoice a Greek supplier actually sends: the currency is written
#: inside the amount and there is no separate `Currency:` label anywhere.
MAINTENANCE_EUR_IN_THE_AMOUNT = """KASTRO GARAGE
Document Type: Maintenance Invoice
Invoice Number: M-3
Date: 2026-07-06
Supplier: Kastro Garage
Net amount: 1.200,00 EUR
Total: 1.200,00 EUR
"""

#: Same supplier, same figure, three days earlier, in dollars.
MAINTENANCE_USD = """KASTRO GARAGE
Document Type: Maintenance Invoice
Invoice Number: M-1
Date: 2026-07-03
Supplier: Kastro Garage
Currency: USD
Net amount: 1,200.00
Total: 1,200.00
"""

#: Same supplier, same figure, three days later, in euro -- and this one states
#: its currency on its own label, so the extractor records it correctly.
MAINTENANCE_EUR_LABELLED = """KASTRO GARAGE
Document Type: Maintenance Invoice
Invoice Number: M-2
Date: 2026-07-06
Supplier: Kastro Garage
Currency: EUR
Net amount: 1.200,00
Total: 1.200,00
"""


def mail(tmp_path: Path, **artifacts: str) -> tuple[list, dict]:
    """Write a month of mail to disk and read it back the production way.

    This is `mailbox.read_period`, the same function the CLI, the demo and the
    Cloud Storage pipeline call. The only thing this helper does is choose the
    directory.
    """
    directory = tmp_path / PERIOD
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in artifacts.items():
        (directory / f"{name}.txt").write_text(text, encoding="utf-8")
    return read_period(PERIOD, root=tmp_path)


def close(documents, raw):
    return run_close(period=PERIOD, documents=documents,
                     company="Bell Ridge Haulage", raw_texts=raw,
                     store=LocalStore())


# ── the refutation that fails: the EUR document is reachable off disk ─────────

def test_documents_a_eur_document_really_does_come_off_the_real_intake_path() -> None:
    """No hand-built object is needed. This is the refutation, and it dies here.

    A `Currency:` label on corpus-shaped text yields `currency="EUR"` through
    `extract_document`, so every hand-built EUR `Document` in the prior agent's
    file stands for something the real mailbox produces.
    """
    from archon.domain.extract import extract_document

    doc = extract_document(BANK_OUT_EUR, "bank-eur.txt", PERIOD)
    assert doc.doc_type == DocType.BANK_TRANSACTION
    assert doc.currency == "EUR"
    assert doc.net_amount == 1865.00
    assert doc.direction == "out"


def test_documents_the_standing_policy_really_does_draft_this_kind() -> None:
    """The other way sub-claim (b) could have been unreachable: it isn't.

    A letter is only written when `policy.apply_choices` applies DRAFT. If
    PAYMENT_WITHOUT_DOCUMENT were escalated to the owner instead, `drafts._money`
    would never be reached with a non-USD document and the defect would be
    latent. The standing policy drafts it.
    """
    assert ExceptionKind.PAYMENT_WITHOUT_DOCUMENT in \
        __import__("archon.domain.models", fromlist=["x"]).ACTIONABLE_KINDS
    from archon.domain.models import Finding

    finding = Finding(kind=ExceptionKind.PAYMENT_WITHOUT_DOCUMENT, severity="error",
                      reference="KG-9001", amount=1865.00, message="x")
    decisions = policy_mod.apply_choices([finding], None)
    assert decisions[0].applied is policy_mod.Disposition.DRAFT


# ── (b): fixed at extraction, STILL WRONG in the letter that goes out ────────

def test_repro_a_eur_payment_off_disk_is_billed_back_to_the_supplier_in_usd(tmp_path) -> None:
    """The strongest sub-claim, driven from bytes on disk to the outbound letter.

    `exceptions.py:111` formats the finding with `doc.currency`, so the owner's
    exception list reads "EUR 1,865.00". `drafts.py:37` is
    `_money(amount, currency="USD")` and NONE of its five call sites
    (drafts.py:47, 69, 86, 89, 108) passes a currency, so the letter drafted
    from that same finding, addressed to that same supplier, demands
    "USD 1,865.00".

    Same document, same run, two currencies. The wrong one is the artifact that
    leaves the building.
    """
    documents, raw = mail(tmp_path, **{"bank-2026-07-28-eur": BANK_OUT_EUR})
    result = close(documents, raw)

    finding = next(f for f in result.findings
                   if f.kind == ExceptionKind.PAYMENT_WITHOUT_DOCUMENT)
    assert "EUR 1,865.00" in finding.message      # the detector is right

    draft = next(d for d in result.drafts if d.reference == "KG-9001")
    printed = draft.subject + "\n" + draft.body

    assert "USD" not in printed, (
        "a EUR payment read off disk produced a letter to Kastro Garage "
        "demanding USD; drafts._money() defaults currency='USD' and no call "
        "site overrides it.\n"
        f"finding: {finding.message}\n"
        f"letter:  {printed}"
    )


def test_documents_currency_written_only_inside_the_amount_is_recovered(tmp_path) -> None:
    """`_money` deleted the EUR token and `extract_document` then defaulted to USD.

    extract.py ran `_CURRENCY.sub("", value)` before parsing and read the
    currency ONLY from a separate `currency:` label. An invoice that writes its
    currency the way most of Europe writes it -- inside the amount -- had that
    marker deleted and was then stamped USD.

    Reachability was total: ordinary supplier text, the real mailbox, no label a
    document would not carry, which is why the default was never a safe one.
    `extract._currency` now reads the explicit label first and otherwise takes
    the symbol or code off the amount itself, so the unit survives the parse
    that used to eat it.
    """
    documents, _ = mail(tmp_path, **{"expense-mnt-M3": MAINTENANCE_EUR_IN_THE_AMOUNT})
    doc = documents[0]

    assert doc.doc_type == DocType.MAINTENANCE_INVOICE
    assert doc.net_amount == 1200.00              # the amount parses fine
    assert doc.currency == "EUR", (
        f"an invoice reading 'Total: 1.200,00 EUR' was recorded as "
        f"{doc.currency} {doc.net_amount:,.2f}; the currency token was stripped "
        f"by extract._money and never recovered."
    )


def test_documents_the_two_currency_defects_no_longer_cancel_each_other(tmp_path) -> None:
    """An honest qualifier the audit's bundling hid, and the half of it left open.

    When the currency was written only inside the amount, extract lost it and
    stamped USD -- and `drafts._money`'s USD default was then ACCIDENTALLY
    consistent with it. The finding and the letter agreed, and both were wrong
    in the same direction. Sub-claims 1 and 2 were two faces of one missing
    currency channel, not two independent defects, and fixing either one alone
    was always going to make the pair visibly disagree.

    Extraction is the half that was fixed, and this is where the cancellation
    dies. The same figure from the same supplier, written in the two notations a
    real mailbox actually receives, now comes back as two different currencies
    instead of collapsing into one. Nothing downstream can lean on the units
    agreeing any more, which is exactly why
    `test_repro_a_eur_payment_off_disk_is_billed_back_to_the_supplier_in_usd`
    still fails: the letter is the half nobody has fixed yet.
    """
    documents, _ = mail(tmp_path, **{
        "expense-mnt-M3": MAINTENANCE_EUR_IN_THE_AMOUNT,   # unit inside the amount
        "expense-mnt-M1": MAINTENANCE_USD,                 # unit on its own label
    })
    by_file = {d.source_file: d for d in documents}
    euro, dollar = by_file["expense-mnt-M3.txt"], by_file["expense-mnt-M1.txt"]

    assert euro.currency == "EUR"
    assert dollar.currency == "USD"
    # Identical figures, different money. Under the old default both read USD
    # 1,200.00 and were indistinguishable to every consumer of a Document.
    assert euro.net_amount == dollar.net_amount == 1200.00
    assert euro.currency != dollar.currency


# ── (c): fixed. Two currencies are refused, not summed and accused ───────────

def test_documents_two_currencies_off_disk_block_the_month_and_are_named(tmp_path) -> None:
    """A month holding USD and EUR used to close, sum them, and never mention it.

    No gate in `validation.py` asked whether the figures being compared shared a
    unit, `ledger.py` never read `doc.currency`, and `_dr`/`_cr` put a bare float
    on an account, so the unit was gone before anything could object. The month
    closed on a maintenance figure that was not money in any currency, and no
    gate, finding, note or line of the summary said so.

    G7 is the gate that was missing. The addition still happens, because the
    ledger has no exchange rate and inventing one would produce a number the
    owner cannot check; refusing is the honest answer while there is no rate
    source. What changed is that the month is now blocked and the gate names
    both currencies, so that meaningless total never reaches anyone as a closed
    book.
    """
    documents, raw = mail(tmp_path, **{
        "expense-mnt-M1": MAINTENANCE_USD,
        "expense-mnt-M2": MAINTENANCE_EUR_LABELLED,
    })
    assert {d.currency for d in documents} == {"USD", "EUR"}

    result = close(documents, raw)

    # The sum still happens: 1,200 USD + 1,200 EUR = "2,400.00" of maintenance.
    # Nothing teaches the ledger arithmetic it cannot do; the gate is what stops
    # the answer counting.
    assert result.statements.maintenance == 2400.00
    assert result.outcome == "blocked"

    currency_gate = next(g for g in result.gates if g.rule.startswith("G7"))
    assert currency_gate.passed is False
    assert currency_gate.severity == "error"
    # Naming both units is the difference between a refusal the owner can act on
    # and one they have to go hunting for.
    assert "EUR" in currency_gate.message
    assert "USD" in currency_gate.message

    spoken = "\n".join(
        [g.message for g in result.gates]
        + [f.message for f in result.findings]
        + list(result.statements.notes)
        + [result.summary]
    )
    assert "currenc" in spoken.lower(), (
        "the close added a EUR invoice to a USD invoice, reported "
        f"{result.statements.maintenance:,.2f} of maintenance, and said nothing "
        "about the unit in any gate, finding, note or "
        f"summary.\noutcome={result.outcome}\n{spoken}"
    )


def test_documents_two_currencies_off_disk_are_not_a_duplicate_charge(tmp_path) -> None:
    """The false accusation that used to be built end to end from real files.

    The duplicate detector keyed on `(counterparty, place, amount)` with no
    currency, so EUR 1,200 and USD 1,200 from the same supplier collided. The
    close raised a DUPLICATE_CHARGE error and drafted a DUPLICATE_REFUND letter
    asking Kastro Garage to credit money back that it had never been paid twice.

    The detector is deliberately fuzzy about two same-currency charges of the
    same size -- it says "probably" and the letter asks for confirmation. That
    was different in kind: not a fuzzy call that went wrong, but a guaranteed
    false positive from a key that omitted the unit. The key now carries the
    currency, so two figures in two currencies are two charges.
    """
    documents, raw = mail(tmp_path, **{
        "expense-mnt-M1": MAINTENANCE_USD,
        "expense-mnt-M2": MAINTENANCE_EUR_LABELLED,
    })
    result = close(documents, raw)

    duplicates = [f for f in result.findings
                  if f.kind == ExceptionKind.DUPLICATE_CHARGE]
    letters = [d for d in result.drafts if d.finding_kind == ExceptionKind.DUPLICATE_CHARGE]

    assert duplicates == [], (
        "a EUR 1,200.00 invoice and a USD 1,200.00 invoice from the same "
        "supplier were reported as one charge billed twice, and "
        f"{len(letters)} refund letter(s) drafted to them: "
        + "; ".join(f.message for f in duplicates)
        + ("\nletter: " + letters[0].body if letters else "")
    )


# ── NOT REPRODUCED (d) and (a-as-drift): pinned as passing behaviour ─────────

def test_documents_rounding_is_deterministic_it_is_merely_half_even() -> None:
    """Sub-claim (d) is wrong on the word that matters.

    "Not deterministic" is the accusation, and it is false: `round()` is pure,
    and two ledgers over the same documents produce identical books. The true,
    narrower defect is that half-even on a BINARY value is not half-up, so the
    direction at an apparent decimal half is unpredictable from the decimal.
    That is a one-line nit, not a P0, and it needs a half-cent to exist at all
    when `extract._money` already rounds every incoming amount to two places.
    """
    assert round(0.125, 2) == 0.12      # down
    assert round(0.135, 2) == 0.14      # up
    assert round(2.675, 2) == 2.67      # a bookkeeper would write 2.68

    from tests.conftest import expense, load

    documents = [load("L-1", 2400.00, accessorial=125.10, miles=1180.3),
                 expense(DocType.TOLL_INVOICE, "T-1", 88.33, 7.07)]
    first, second = Ledger(period=PERIOD), Ledger(period=PERIOD)
    first.add_all(documents)
    second.add_all(documents)
    assert first.statements() == second.statements()
    assert first.balances() == second.balances()


def test_documents_float_drift_is_neutralised_by_step_rounding(tmp_path) -> None:
    """"Money is float" does not reach the owner as a wrong figure.

    `Ledger.balances()` re-rounds to two places at every accumulation step, so
    error cannot compound. Driven off disk here rather than from a fixture: a
    real fuel card statement with forty fills at binary-inexact prices totals
    exactly, through the real extractor and the real ledger.
    """
    fills = "\n".join(
        f"2026-07-{(i % 28) + 1:02d} T-1 Truck Stop {i} Gallons 10.0 "
        f"Gross 0.10 Tax 0.00"
        for i in range(40)
    )
    statement = ("ROADWAY FUEL NETWORK\n"
                 "Document Type: Fuel Card Statement\n"
                 "Statement Number: FCN-2026-07\n"
                 "Date: 2026-07-31\n"
                 "Supplier: Roadway Fuel Network\n" + fills + "\n")

    documents, _ = mail(tmp_path, **{"fuelcard-FCN": statement})
    assert len(documents[0].fuel_lines) == 40

    # Deliberately no assertion that the naive sum drifts first. Whether it
    # does depends on the platform: the same line passed on Windows and CPython
    # 3.11 and failed on Linux and 3.12. What is being tested is that the
    # books land on the figure, whatever the arithmetic underneath did.
    ledger = Ledger(period=PERIOD)
    ledger.add_all(documents)
    assert ledger.statements().fuel == 4.00      # and absent from the books
