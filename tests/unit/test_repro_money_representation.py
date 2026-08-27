"""Reproduction for audit claim 1.4: money representation and currency.

The claim bundles four sub-claims. They do not all hold, and the difference
matters more than the headline:

    (a) money is float                      -- TRUE, but see (c)/(d)
    (b) currency is not handled explicitly  -- TRUE, and it reaches the owner
    (c) amounts in different currencies     -- TRUE, silently, end to end
        could be summed
    (d) rounding is not deterministic       -- FALSE. It is deterministic.
                                               It is merely not half-up.

Everything below drives real entry points: `extract_document` on real document
text, the real detectors, the real drafter, the real `Ledger`. Nothing asserts
on a style preference; every failing test asserts that a NUMBER OR A LABEL THE
OWNER READS is wrong.

Test naming: `test_repro_*` fails on the current code because the defect is
real. `test_documents_*` passes and pins behaviour the audit got wrong.
"""
from __future__ import annotations

import dataclasses

from archon.adapters.store import LocalStore
from archon.domain import allocation as allocation_mod
from archon.domain import drafts as drafts_mod
from archon.domain import exceptions as exceptions_mod
from archon.domain.extract import extract_document
from archon.domain.ledger import Ledger
from archon.domain.models import (
    DocType,
    Document,
    ExceptionKind,
)
from archon.runtime.close import run_close
from tests.conftest import PERIOD, expense, load, remittance

# ── the fact: what the domain actually declares ──────────────────────────────

MONEY_FIELDS = (
    "net_amount", "tax_amount", "gross_amount", "accessorial",
    "remittance_total", "factoring_fee",
    "driver_gross", "driver_deductions", "driver_net", "tax_withheld",
)


def test_documents_every_money_field_is_a_binary_float() -> None:
    """Establish the fact the audit asserts, from the dataclass itself.

    Not a defect on its own. It is the premise the consequence tests below
    either do or do not cash out.
    """
    annotations = {f.name: f.type for f in dataclasses.fields(Document)}
    for name in MONEY_FIELDS:
        assert "float" in str(annotations[name]), name
        assert "Decimal" not in str(annotations[name]), name

    # And currency is a bare string with a default, carried alongside the
    # number rather than attached to it.
    assert annotations["currency"] == "str"
    assert Document(doc_type=DocType.UNKNOWN, period=PERIOD).currency == "USD"


# ── reproduced (b): extraction reads a currency the rest of the system drops ──

EUR_INVOICE = """KASTRO GARAGE
Document Type: Maintenance Invoice
Invoice Number: M-2
Date: 2026-07-06
Supplier: Kastro Garage
Currency: EUR
Net amount: 1,200.00
Total: 1,200.00
"""


#: The same invoice as a European supplier actually writes it: the currency
#: lives in the amount, and there is no separate `Currency:` line.
EUR_INVOICE_SYMBOL_ONLY = """KASTRO GARAGE
Document Type: Maintenance Invoice
Invoice Number: M-3
Date: 2026-07-06
Supplier: Kastro Garage
Net amount: 1.200,00 EUR
Total: 1.200,00 EUR
"""


def test_documents_extraction_does_read_a_stated_currency() -> None:
    """The extractor is not always the problem: a `Currency:` label works.

    This passes, and it is what makes the failures below reachable from a real
    document rather than only from a hand-built object.
    """
    doc = extract_document(EUR_INVOICE, "eur-invoice.txt", PERIOD)
    assert doc.doc_type == DocType.MAINTENANCE_INVOICE
    assert doc.currency == "EUR"
    assert doc.net_amount == 1200.00


def test_repro_a_eur_amount_with_no_currency_label_is_stamped_usd() -> None:
    """The currency marker is stripped off the number and then not recorded.

    `extract._money` runs `_CURRENCY.sub("", value)` (extract.py:96), deleting
    `$ EUR GBP` and friends from the amount before parsing. `extract_document`
    (extract.py:356) then sets the currency ONLY from a separate `currency:`
    label, falling back to "USD".

    So the one document that states its currency the way European suppliers
    actually state it -- inside the amount, with no separate label -- has that
    marker deleted and is then stamped USD. The docstring at extract.py:91-92
    says "the currency is carried on the document, not baked into the number";
    when the number is the only place it was written, it is carried nowhere.

    This is the highest-reachability version of the defect: no hand-built
    object, no unusual label, just ordinary European invoice text through the
    real entry point. The module's own docstring notes that "a Greek invoice is
    the first real document this had to survive", and Greek invoices are EUR.
    """
    doc = extract_document(EUR_INVOICE_SYMBOL_ONLY, "eur-symbol.txt", PERIOD)

    assert doc.net_amount == 1200.00          # the amount parses fine
    assert doc.currency == "EUR", (
        f"an invoice reading '1.200,00 EUR' was recorded as {doc.currency} "
        f"{doc.net_amount:,.2f}; the currency token was stripped by "
        f"extract._money and never recovered."
    )


def test_repro_a_eur_payment_is_reported_as_eur_but_billed_back_as_usd() -> None:
    """The unarguable one: two parts of the codebase disagree about ONE document.

    `exceptions.py:111` formats the finding with `doc.currency`, so the owner's
    exception list correctly reads "EUR 1,200.00". `drafts.py:37` defines
    `_money(amount, currency="USD")` and is called at FIVE sites, none of which
    passes the currency. So the letter drafted FROM that finding, addressed to
    the supplier, demands "USD 1,200.00".

    Same document, same close, two different currencies printed. Whichever is
    right, the pair cannot both be, and the wrong one is on the outbound
    artifact.
    """
    paid_out = Document(
        doc_type=DocType.BANK_TRANSACTION, period=PERIOD,
        source_file="bank-eur.txt", date="2026-07-06", direction="out",
        currency="EUR", net_amount=1200.00, reference="M-2",
        counterparty="Kastro Garage",
    )

    findings = exceptions_mod.find_payments_without_documents([paid_out])
    finding = next(f for f in findings
                   if f.kind == ExceptionKind.PAYMENT_WITHOUT_DOCUMENT)

    # The detector gets it right.
    assert "EUR 1,200.00" in finding.message

    draft = drafts_mod.draft_for(finding)
    assert draft is not None
    printed = draft.subject + "\n" + draft.body

    # The letter built from that very finding does not.
    assert "USD" not in printed, (
        "the draft letter for a EUR document claims USD; "
        f"drafts._money() defaults currency='USD' and no call site overrides it.\n"
        f"finding said: {finding.message}\n"
        f"letter says:  {printed}"
    )


# ── reproduced (c): two currencies summed, and an accusation built on it ─────

def test_repro_two_currencies_are_summed_into_one_revenue_figure() -> None:
    """EUR 1,000 + USD 1,000 posts as 2,000 with no refusal and no note.

    The ledger never reads `doc.currency`. Statements are a roll-up of journal
    lines, and a journal line is a bare float on an account, so the currency is
    gone by the time anything can object to it.
    """
    ledger = Ledger(period=PERIOD, company="Bell Ridge Haulage")
    ledger.add_all([
        Document(doc_type=DocType.SALES_INVOICE, period=PERIOD,
                 source_file="usd.txt", currency="USD", date="2026-07-03",
                 counterparty="A Customer", net_amount=1000.00,
                 tax_amount=0.0, gross_amount=1000.00),
        Document(doc_type=DocType.SALES_INVOICE, period=PERIOD,
                 source_file="eur.txt", currency="EUR", date="2026-07-04",
                 counterparty="Another Customer", net_amount=1000.00,
                 tax_amount=0.0, gross_amount=1000.00),
    ])
    statements = ledger.statements()

    # The sum happens. This part is just the fact.
    assert statements.revenue == 2000.00

    # The product's stated rule is that it raises what it cannot explain rather
    # than absorbing it. Adding two currencies is exactly that, and nothing in
    # the roll-up says so.
    assert any("currenc" in note.lower() for note in statements.notes), (
        "revenue of 2000.00 was produced by adding USD 1000 to EUR 1000 and "
        f"the statements carry no note saying so. notes={statements.notes}"
    )


def test_repro_the_close_is_silent_about_a_month_holding_two_currencies() -> None:
    """End to end through `run_close`: no gate, no finding, no note mentions it.

    This is the "does the code LET it happen end to end" question. It does.
    The six validation gates check that entries balance, that remittances
    reconcile, that the bank agrees, that nothing posted twice -- and not one
    of them asks whether the figures being compared are in the same unit.
    """
    documents = [
        expense(DocType.MAINTENANCE_INVOICE, "M-1", 1200.00, 0.0,
                supplier="Kastro Garage", date="2026-07-03"),
        Document(doc_type=DocType.MAINTENANCE_INVOICE, period=PERIOD,
                 source_file="eur-invoice.txt", date="2026-07-06",
                 document_number="M-2", counterparty="Kastro Garage",
                 currency="EUR", net_amount=1200.00, tax_amount=0.0,
                 gross_amount=1200.00),
    ]

    result = run_close(period=PERIOD, documents=documents,
                       company="Bell Ridge Haulage", store=LocalStore())

    currencies = {d.currency for d in documents}
    assert currencies == {"USD", "EUR"}      # the month really does mix them

    spoken = "\n".join(
        [g.message for g in result.gates]
        + [f.message for f in result.findings]
        + list(result.statements.notes)
        + [result.summary]
    )
    assert "currenc" in spoken.lower(), (
        "the close ran a month containing both USD and EUR documents, summed "
        "them, and said nothing about it in any gate, finding, note or "
        f"summary.\noutcome={result.outcome}\n{spoken}"
    )


def test_repro_same_amount_in_two_currencies_is_accused_of_being_a_duplicate() -> None:
    """A EUR 1,200 invoice and a USD 1,200 invoice become a duplicate charge.

    `find_duplicate_charges` keys on `(counterparty, place, amount)`. The
    amount is a bare float, so two genuinely different invoices in two
    different currencies collide on the key, and the second one is reported as
    an error and a `DUPLICATE_REFUND` letter is drafted to the supplier.

    This is the version that hurts: the wrong number is not a rounding cent,
    it is a false accusation addressed to a named counterparty.

    The control below matters, because without it this looks like the detector
    working as designed. Two SAME-currency 1,200.00 invoices three days apart
    are a plausible duplicate, and the detector is deliberately fuzzy about
    them -- it says "probably" and the letter asks the supplier to confirm. A
    EUR/USD pair is a different thing: not a fuzzy call that happened to be
    wrong, but a guaranteed false positive, because the key it collides on
    omits the unit that makes the two charges different.
    """
    usd = expense(DocType.MAINTENANCE_INVOICE, "M-1", 1200.00, 0.0,
                  supplier="Kastro Garage", date="2026-07-03")
    same_currency_twin = expense(DocType.MAINTENANCE_INVOICE, "M-2", 1200.00, 0.0,
                                 supplier="Kastro Garage", date="2026-07-06")
    eur = Document(doc_type=DocType.MAINTENANCE_INVOICE, period=PERIOD,
                   source_file="eur-invoice.txt", date="2026-07-06",
                   document_number="M-2", counterparty="Kastro Garage",
                   currency="EUR", net_amount=1200.00, tax_amount=0.0,
                   gross_amount=1200.00)

    # Control: same currency, same amount, same supplier -> flagged BY DESIGN.
    assert len(exceptions_mod.find_duplicate_charges([usd, same_currency_twin])) == 1

    findings = exceptions_mod.find_duplicate_charges([usd, eur])

    assert findings == [], (
        "two invoices in DIFFERENT currencies were reported as the same charge "
        "billed twice, because exceptions.py:293 keys duplicates on "
        "(counterparty, place, amount) with no currency: "
        + "; ".join(f.message for f in findings)
    )


# ── NOT reproduced (d): rounding IS deterministic ────────────────────────────

def test_documents_the_close_is_byte_stable_across_runs() -> None:
    """The audit's word "deterministic" is wrong, and it is the word that matters.

    `round()` on a float is a pure function: same input, same output, forever.
    Re-running the close on the same documents reproduces the same books, which
    is the property the product actually claims ("a judge can re-run the close
    and get the same books"). Sub-claim (d) does not reproduce.
    """
    documents = [
        load("L-1", 2400.00, accessorial=125.10, miles=1180.3),
        load("L-2", 1875.55, miles=903.7),
        expense(DocType.TOLL_INVOICE, "T-1", 88.33, 7.07),
    ]
    first = Ledger(period=PERIOD)
    first.add_all(documents)
    second = Ledger(period=PERIOD)
    second.add_all(documents)

    assert first.statements() == second.statements()
    assert first.balances() == second.balances()


def test_documents_rounding_is_half_even_on_the_binary_value_not_half_up() -> None:
    """The true, narrower version of sub-claim (d), pinned as behaviour.

    An accountant expects half-up. Python's `round` is half-even, and on a
    float the decision is made on the BINARY value, so the direction taken at
    an exact-looking decimal half is not predictable from the decimal:

        round(0.125, 2) -> 0.12   (down)
        round(0.135, 2) -> 0.14   (up)

    Deterministic, reproducible, and not what a bookkeeper would write. It is a
    correctness nit worth one line of fix, not a P0: it needs a half-cent to
    exist in the first place, and every amount entering the ledger has already
    been rounded to two places by `extract._money`.
    """
    assert round(0.125, 2) == 0.12
    assert round(0.135, 2) == 0.14
    assert round(2.675, 2) == 2.67

    # And the ledger inherits exactly that, with no separate rounding policy.
    ledger = Ledger(period=PERIOD)
    ledger.add(expense(DocType.TOLL_INVOICE, "T-1", 2.675, 0.0))
    line = ledger.entries[0].lines[0]
    assert line.debit == 2.67      # a half-up policy would say 2.68


# ── NOT reproduced (a-as-drift): float accumulation is neutralised ───────────

def test_documents_a_thousand_small_charges_still_total_exactly() -> None:
    """Float drift does not reach the owner, and the design is why.

    `Ledger.balances()` re-rounds to two places at EVERY accumulation step
    rather than summing raw and rounding once, so error cannot compound. A
    naive sum of 1,000 x 0.01 is 9.999999999999831; the ledger's is 10.00.

    An honest negative result: for the amounts and volumes this product
    handles, float is harmless here.
    """
    fills = [(f"2026-07-{(i % 28) + 1:02d}", "T-1", "Stop", 1.0, 0.01, 0.0)
             for i in range(1000)]
    from tests.conftest import fuel
    ledger = Ledger(period=PERIOD)
    ledger.add(fuel("FC-1", fills))

    naive = sum(0.01 for _ in range(1000))
    assert naive != 10.00                      # the drift is real in the abstract
    assert ledger.statements().fuel == 10.00   # and absent from the books


def test_documents_the_allocation_identity_holds_on_binary_inexact_parts() -> None:
    """0.10 + 0.20 != 0.30 in binary, and the remittance still reconciles.

    Nine loads paid at amounts that are exact in decimal and inexact in binary,
    less a fee. The identity `landed == lines - fee` is checked on values that
    were rounded to two places on the way in, so the residual is exactly 0.00 --
    not "within tolerance", exactly zero.
    """
    parts = [1000.10, 2000.20, 3000.30, 1500.15, 2500.25,
             1750.35, 1250.45, 3250.55, 875.65]
    loads = [load(f"L-{i}", amount, miles=100) for i, amount in enumerate(parts)]
    fee = 261.40
    remit = remittance(
        "RA-1",
        [(f"L-{i}", amount, 0.0, None) for i, amount in enumerate(parts)],
        fee=fee,
    )

    result = allocation_mod.allocate_remittance(remit, loads + [remit])

    assert result.residual == 0.0
    assert result.reconciles
    assert result.allocated_gross == round(sum(parts), 2)
