"""Double-entry posting and the period roll-up. Deterministic, stdlib only.

Every document becomes exactly one balanced journal entry, and every figure the
owner ever sees is a roll-up of those entries. No model call reaches this
module, which is what makes the books auditable: a wrong number here is a bug
with a failing test, not a hallucination with a plausible tone.

Two postings carry the domain and are worth reading closely.

**A load confirmation is revenue before anyone pays.** It debits receivables
and credits linehaul (and accessorial, when the broker booked detention or a
lumper fee separately). The cash arrives later, in a lump, covering nine of
these at once. That gap between earning and being paid is the whole reason a
haulier's books are hard, and it is why `allocation.py` exists.

**A remittance does not settle receivables at its own face value.** What lands
in the bank is the loads' gross less the factoring fee. So the entry debits
bank for what arrived, debits factoring fee for what the factor took, and
credits receivables for the full gross the loads were carrying. Book it any
other way and receivables never clear.
"""
from __future__ import annotations

from .models import (
    Account,
    AllocationResult,
    DocType,
    Document,
    JournalEntry,
    JournalLine,
    Statements,
)
from .periods import belongs_to


def _dr(account: Account, amount: float) -> JournalLine:
    return JournalLine(account, debit=round(amount, 2))


def _cr(account: Account, amount: float) -> JournalLine:
    return JournalLine(account, credit=round(amount, 2))


#: Expense document families that post the same shape: an expense, a recoverable
#: tax where one applies, and a payable for the gross.
_SIMPLE_EXPENSES = {
    DocType.TOLL_INVOICE: (Account.TOLLS_EXPENSE, "Tolls"),
    DocType.MAINTENANCE_INVOICE: (Account.MAINTENANCE_EXPENSE, "Maintenance"),
    DocType.INSURANCE_INVOICE: (Account.INSURANCE_EXPENSE, "Insurance"),
}


class Ledger:
    """The month's books: documents in, balanced entries out, statements up.

    The ledger holds no opinion about whether the month is any good. It posts
    what it is given and rolls it up. Judging the result is `exceptions.py`'s
    job, and the separation is deliberate: a detector that could also change
    the numbers it inspects would be marking its own homework.
    """

    def __init__(self, period: str, company: str | None = None):
        self.period = period
        self.company = company
        self.documents: list[Document] = []
        self.entries: list[JournalEntry] = []
        self.allocations: list[AllocationResult] = []

    # ── posting ──────────────────────────────────────────────────────────────

    def add(self, doc: Document) -> JournalEntry:
        """Record one document and post the entry it implies."""
        self.documents.append(doc)
        entry = self._post(doc)
        self.entries.append(entry)
        return entry

    def add_all(self, docs: list[Document]) -> list[JournalEntry]:
        return [self.add(d) for d in docs]

    @property
    def posted(self) -> list[Document]:
        """The documents that belong to this month, and therefore to its figures.

        `self.documents` is everything that ARRIVED, and stays that way because
        G6 has to be able to account for an artifact the owner sent whatever its
        date. This is the narrower list every roll-up must use: revenue, costs,
        miles and the per-truck table are statements about one month, and a June
        load confirmation is not one of them.

        Splitting the two is the whole fix. Before it, the money was rolled up
        from entries (which posted regardless of date) and the miles from
        `self.documents` (which never filtered), so a June load added its
        distance to July's cost-per-mile even after the money stopped leaking.
        """
        return [d for d in self.documents if belongs_to(self.period, d.date)]

    def _post(self, doc: Document) -> JournalEntry:
        entry = JournalEntry(
            date=doc.date, period=doc.period, memo="", source_doc=doc.source_file
        )

        # A document dated outside this month posts NOTHING. `find_out_of_period`
        # has always said so in as many words -- "what it must not do is post it
        # silently into the wrong month" -- and for as long as it said it, the
        # figures went into the books anyway and `balances()` rolled up every
        # entry with no period predicate at all.
        #
        # The check has to read the DATE. `doc.period` is the month being
        # closed, stamped on every document by the extractor, so a June invoice
        # read during a July close carries "2026-07" and the obvious comparison
        # is always false. That is why this was never caught by inspection.
        #
        # Same deliberate-no-op shape as `_post_unreadable`: the entry survives
        # on the trail carrying its reason, and the warning the detector already
        # raises is what tells the owner. The month still closes, because a June
        # invoice arriving in July mail is ordinary. It simply is not June's
        # money counted as July's.
        if not belongs_to(self.period, doc.date):
            entry.memo = (
                f"Dated {doc.date}, outside {self.period}. Recorded, not posted: "
                f"{doc.source_file}"
            )
            return entry

        handler = getattr(self, "_post_" + doc.doc_type.value, None)
        if handler is None:
            entry.memo = f"Not posted: {doc.doc_type.value} {doc.source_file}"
            return entry
        handler(doc, entry)
        return entry

    def _post_load_confirmation(self, doc: Document, entry: JournalEntry) -> None:
        linehaul = doc.net_amount or 0.0
        accessorial = doc.accessorial or 0.0
        entry.memo = f"Load {doc.load_ref} for {doc.broker or doc.counterparty}"
        entry.lines = [_dr(Account.ACCOUNTS_RECEIVABLE, linehaul + accessorial),
                       _cr(Account.REVENUE_LINEHAUL, linehaul)]
        if accessorial:
            entry.lines.append(_cr(Account.REVENUE_ACCESSORIAL, accessorial))

    def _post_broker_remittance(self, doc: Document, entry: JournalEntry) -> None:
        # Cash arrives net of the factor's cut; receivables clear at gross.
        landed = doc.remittance_total or 0.0
        fee = doc.factoring_fee or 0.0
        cleared = round(landed + fee, 2)
        entry.memo = f"Remittance {doc.document_number} from {doc.broker or 'broker'}"
        entry.lines = [_dr(Account.BANK, landed)]
        if fee:
            entry.lines.append(_dr(Account.FACTORING_FEE, fee))
        entry.lines.append(_cr(Account.ACCOUNTS_RECEIVABLE, cleared))

    def _post_fuel_card_statement(self, doc: Document, entry: JournalEntry) -> None:
        # One statement, many fills. Fuel tax is recoverable and is held apart
        # from the fuel cost so the cost-per-mile figure is not inflated by it.
        gross = round(sum(line.gross for line in doc.fuel_lines), 2) or (doc.gross_amount or 0.0)
        tax = round(sum(line.tax for line in doc.fuel_lines), 2) or (doc.tax_amount or 0.0)
        entry.memo = (f"Fuel card {doc.document_number or doc.source_file} "
                      f"({len(doc.fuel_lines)} fills)")
        entry.lines = [_dr(Account.FUEL_EXPENSE, round(gross - tax, 2))]
        if tax:
            entry.lines.append(_dr(Account.FUEL_TAX_RECEIVABLE, tax))
        entry.lines.append(_cr(Account.ACCOUNTS_PAYABLE, gross))

    def _post_driver_settlement(self, doc: Document, entry: JournalEntry) -> None:
        # The expense is what the driver earned; what leaves the bank later is
        # the net. The withholding sits as a payable in between.
        gross = doc.driver_gross or 0.0
        net = doc.driver_net or 0.0
        withheld = doc.tax_withheld or 0.0
        other = round(gross - net - withheld, 2)
        entry.memo = f"Driver settlement {doc.driver or ''}".strip()
        entry.lines = [_dr(Account.DRIVER_PAY_EXPENSE, gross),
                       _cr(Account.DRIVER_PAY_PAYABLE, net)]
        if withheld:
            entry.lines.append(_cr(Account.TAX_WITHHELD_PAYABLE, withheld))
        if abs(other) >= 0.01:
            # Deductions the driver owes back (advances, escrow) reduce payables.
            entry.lines.append(_cr(Account.ACCOUNTS_PAYABLE, other))

    def _post_bank_transaction(self, doc: Document, entry: JournalEntry) -> None:
        amount = doc.net_amount or 0.0
        reference = doc.reference or ""
        if doc.direction == "in":
            entry.memo = f"Receipt {reference}"
            entry.lines = [_dr(Account.BANK, amount),
                           _cr(Account.ACCOUNTS_RECEIVABLE, amount)]
        elif doc.driver or "settlement" in reference.lower():
            entry.memo = f"Driver paid {reference}"
            entry.lines = [_dr(Account.DRIVER_PAY_PAYABLE, amount),
                           _cr(Account.BANK, amount)]
        else:
            entry.memo = f"Payment {reference}"
            entry.lines = [_dr(Account.ACCOUNTS_PAYABLE, amount),
                           _cr(Account.BANK, amount)]

    def _post_unreadable(self, doc: Document, entry: JournalEntry) -> None:
        # Deliberately posts nothing. An artifact nobody could read has no
        # figures, and guessing one would be the single worst thing this
        # product could do. It leaves the close as an exception instead.
        entry.memo = f"Unreadable, not posted: {doc.source_file}"

    def _post_unknown(self, doc: Document, entry: JournalEntry) -> None:
        entry.memo = f"Unclassified, not posted: {doc.source_file}"

    def _post_simple_expense(self, doc: Document, entry: JournalEntry,
                             account: Account, label: str) -> None:
        net = doc.net_amount or 0.0
        tax = doc.tax_amount or 0.0
        gross = doc.gross_amount or round(net + tax, 2)
        entry.memo = f"{label} {doc.document_number or ''} from {doc.counterparty}".strip()
        entry.lines = [_dr(account, net)]
        if tax:
            entry.lines.append(_dr(Account.FUEL_TAX_RECEIVABLE, tax))
        entry.lines.append(_cr(Account.ACCOUNTS_PAYABLE, gross))

    def _post_sales_invoice(self, doc: Document, entry: JournalEntry) -> None:
        """Revenue the firm is owed, plus the VAT it now owes the state.

        Dr Accounts Receivable, Cr Revenue, Cr VAT Payable. The VAT is a
        liability from the moment the invoice is issued, not when it is paid,
        which is why it is a separate credit rather than being folded into
        revenue: netting them here would overstate the P&L by the VAT rate and
        lose the figure the return is built from.
        """
        net = doc.net_amount or 0.0
        tax = doc.tax_amount or 0.0
        gross = doc.gross_amount or round(net + tax, 2)
        entry.memo = (f"Sales invoice {doc.reference or ''} "
                      f"to {doc.counterparty or 'a customer'}").strip()
        entry.lines = [_dr(Account.ACCOUNTS_RECEIVABLE, gross),
                       _cr(Account.REVENUE_INVOICED, net)]
        if tax:
            entry.lines.append(_cr(Account.VAT_PAYABLE, tax))

    def _post_purchase_invoice(self, doc: Document, entry: JournalEntry) -> None:
        """A cost the firm owes, plus the VAT it may reclaim.

        Dr Operating Expense, Dr VAT Receivable, Cr Accounts Payable. The
        mirror of the sales case, and kept separate for the same reason.
        """
        net = doc.net_amount or 0.0
        tax = doc.tax_amount or 0.0
        gross = doc.gross_amount or round(net + tax, 2)
        entry.memo = (f"Purchase invoice {doc.reference or ''} "
                      f"from {doc.counterparty or 'a supplier'}").strip()
        entry.lines = [_dr(Account.OPERATING_EXPENSE, net)]
        if tax:
            entry.lines.append(_dr(Account.VAT_RECEIVABLE, tax))
        entry.lines.append(_cr(Account.ACCOUNTS_PAYABLE, gross))

    def _post_toll_invoice(self, doc: Document, entry: JournalEntry) -> None:
        self._post_simple_expense(doc, entry, *_SIMPLE_EXPENSES[DocType.TOLL_INVOICE])

    def _post_maintenance_invoice(self, doc: Document, entry: JournalEntry) -> None:
        self._post_simple_expense(doc, entry, *_SIMPLE_EXPENSES[DocType.MAINTENANCE_INVOICE])

    def _post_insurance_invoice(self, doc: Document, entry: JournalEntry) -> None:
        self._post_simple_expense(doc, entry, *_SIMPLE_EXPENSES[DocType.INSURANCE_INVOICE])

    # ── roll-up ──────────────────────────────────────────────────────────────

    def balances(self) -> dict[Account, tuple[float, float]]:
        """Total debits and credits per account across every posted entry."""
        totals: dict[Account, list[float]] = {}
        for entry in self.entries:
            for line in entry.lines:
                bucket = totals.setdefault(line.account, [0.0, 0.0])
                bucket[0] = round(bucket[0] + line.debit, 2)
                bucket[1] = round(bucket[1] + line.credit, 2)
        return {account: (d, c) for account, (d, c) in totals.items()}

    def statements(self) -> Statements:
        """Roll the journal up into the period result."""
        balances = self.balances()

        def debit_balance(account: Account) -> float:
            d, c = balances.get(account, (0.0, 0.0))
            return round(d - c, 2)

        def credit_balance(account: Account) -> float:
            d, c = balances.get(account, (0.0, 0.0))
            return round(c - d, 2)

        linehaul = credit_balance(Account.REVENUE_LINEHAUL)
        accessorial = credit_balance(Account.REVENUE_ACCESSORIAL)
        invoiced = credit_balance(Account.REVENUE_INVOICED)
        revenue = round(linehaul + accessorial + invoiced, 2)

        fuel = debit_balance(Account.FUEL_EXPENSE)
        tolls = debit_balance(Account.TOLLS_EXPENSE)
        maintenance = debit_balance(Account.MAINTENANCE_EXPENSE)
        insurance = debit_balance(Account.INSURANCE_EXPENSE)
        driver_pay = debit_balance(Account.DRIVER_PAY_EXPENSE)
        factoring = debit_balance(Account.FACTORING_FEE)
        operating = debit_balance(Account.OPERATING_EXPENSE)
        opex = round(fuel + tolls + maintenance + insurance + driver_pay
                     + factoring + operating, 2)

        cash_in, cash_out = balances.get(Account.BANK, (0.0, 0.0))
        # None, not 0.0, when nothing in the month has a mileage. A hauler that
        # ran no miles and a consultancy that has no trucks are different
        # facts, and 0 says the first. The digest's opening sentence and the
        # per-mile panes read this field, so a zero here is a lie in the first
        # line the owner reads.
        mileage_docs = [d for d in self.posted
                        if d.doc_type == DocType.LOAD_CONFIRMATION]
        miles = (round(sum(d.miles or 0.0 for d in mileage_docs), 2)
                 if mileage_docs else None)

        notes = []
        if factoring:
            notes.append(
                f"Factoring took {factoring:,.2f} of {revenue:,.2f} billed this period; "
                f"the bank only ever saw the net."
            )
        unposted = [d for d in self.documents if d.doc_type == DocType.UNREADABLE]
        if unposted:
            notes.append(
                f"{len(unposted)} document(s) could not be read and were deliberately "
                f"left unposted rather than estimated."
            )

        return Statements(
            period=self.period,
            revenue_linehaul=linehaul,
            revenue_accessorial=accessorial,
            revenue=revenue,
            fuel=fuel,
            tolls=tolls,
            maintenance=maintenance,
            insurance=insurance,
            driver_pay=driver_pay,
            factoring_fees=factoring,
            operating_expenses=opex,
            net_profit=round(revenue - opex, 2),
            cash_in=round(cash_in, 2),
            cash_out=round(cash_out, 2),
            net_cash=round(cash_in - cash_out, 2),
            accounts_receivable=debit_balance(Account.ACCOUNTS_RECEIVABLE),
            accounts_payable=credit_balance(Account.ACCOUNTS_PAYABLE),
            total_miles=miles,
            cost_per_mile=round(opex / miles, 3) if miles else None,
            revenue_per_mile=round(revenue / miles, 3) if miles else None,
            per_truck=self.per_truck(),
            notes=notes,
        )

    def per_truck(self) -> dict:
        """Miles, revenue and direct cost for each truck that ran this month.

        Direct cost is fuel and maintenance, the two the owner can act on per
        truck. Insurance and driver pay are deliberately left at the firm
        level: apportioning them would invent a figure, and an invented figure
        is exactly what this product refuses to produce.
        """
        trucks: dict[str, dict] = {}

        def bucket(name: str | None) -> dict | None:
            if not name:
                return None
            return trucks.setdefault(
                name, {"miles": 0.0, "revenue": 0.0, "fuel": 0.0, "maintenance": 0.0}
            )

        for doc in self.posted:
            if doc.doc_type == DocType.LOAD_CONFIRMATION:
                row = bucket(doc.truck)
                if row is not None:
                    row["miles"] = round(row["miles"] + (doc.miles or 0.0), 2)
                    row["revenue"] = round(
                        row["revenue"] + (doc.net_amount or 0.0) + (doc.accessorial or 0.0), 2
                    )
            elif doc.doc_type == DocType.FUEL_CARD_STATEMENT:
                for line in doc.fuel_lines:
                    row = bucket(line.truck)
                    if row is not None:
                        row["fuel"] = round(row["fuel"] + line.gross - line.tax, 2)
            elif doc.doc_type == DocType.MAINTENANCE_INVOICE:
                row = bucket(doc.truck)
                if row is not None:
                    row["maintenance"] = round(row["maintenance"] + (doc.net_amount or 0.0), 2)

        for row in trucks.values():
            direct = round(row["fuel"] + row["maintenance"], 2)
            row["direct_cost"] = direct
            row["cost_per_mile"] = round(direct / row["miles"], 3) if row["miles"] else None
            row["revenue_per_mile"] = (
                round(row["revenue"] / row["miles"], 3) if row["miles"] else None
            )
        return trucks

    def all_entries_balanced(self) -> bool:
        return all(entry.is_balanced for entry in self.entries)

    def documents_of(self, doc_type: DocType) -> list[Document]:
        return [d for d in self.documents if d.doc_type == doc_type]
