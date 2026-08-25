"""Domain model for a small haulage firm's month.

Archon closes the month for an owner-operator trucking firm running three to
twelve trucks. That firm's month is not a tidy stack of invoices: it is a fuel
card statement with forty lines, a broker remittance that pays nine loads at
once minus a factoring fee, toll bills that arrive twice, a driver settlement
sheet, and one scan nobody can read.

Everything here is data. No arithmetic lives in this module and no model call
touches it. The rule the whole product rests on: the agent orchestrates, the
ledger computes. A figure that reaches the owner was produced by `ledger.py`,
`allocation.py` or `exceptions.py`, never phrased into existence by Gemini.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DocType(str, Enum):
    """The document families a haulier actually receives in a month."""

    LOAD_CONFIRMATION = "load_confirmation"      # a broker books a load at a rate
    BROKER_REMITTANCE = "broker_remittance"      # one payment settling many loads
    FUEL_CARD_STATEMENT = "fuel_card_statement"  # one statement, many fills
    TOLL_INVOICE = "toll_invoice"
    MAINTENANCE_INVOICE = "maintenance_invoice"
    INSURANCE_INVOICE = "insurance_invoice"
    DRIVER_SETTLEMENT = "driver_settlement"      # pay per mile, minus deductions
    BANK_TRANSACTION = "bank_transaction"
    #: The two families every business has and a haulier's document set did
    #: not cover. Added because the honest answer to "will my own invoices
    #: work?" was no, and the close said nothing. A sales invoice is revenue
    #: the firm is owed; a purchase invoice is a cost the firm owes.
    SALES_INVOICE = "sales_invoice"
    PURCHASE_INVOICE = "purchase_invoice"
    UNREADABLE = "unreadable"                    # arrived, could not be read
    UNKNOWN = "unknown"


class Account(str, Enum):
    """A haulage chart of accounts, deliberately small enough to audit by eye."""

    REVENUE_LINEHAUL = "Revenue - Linehaul"
    REVENUE_ACCESSORIAL = "Revenue - Accessorial"
    #: Invoiced revenue that is not freight. A sales invoice posts here rather
    #: than to linehaul, because calling a consulting fee "linehaul" would put
    #: a lie in the chart of accounts to save adding a line to it.
    REVENUE_INVOICED = "Revenue - Invoiced"
    FACTORING_FEE = "Factoring Fee"
    FUEL_EXPENSE = "Fuel"
    TOLLS_EXPENSE = "Tolls"
    MAINTENANCE_EXPENSE = "Maintenance"
    INSURANCE_EXPENSE = "Insurance"
    DRIVER_PAY_EXPENSE = "Driver Pay"
    ACCOUNTS_RECEIVABLE = "Accounts Receivable"
    ACCOUNTS_PAYABLE = "Accounts Payable"
    BANK = "Bank"
    DRIVER_PAY_PAYABLE = "Driver Pay Payable"
    TAX_WITHHELD_PAYABLE = "Withheld Tax Payable"
    FUEL_TAX_RECEIVABLE = "Fuel Tax Receivable"
    #: VAT is a liability on what you invoice and an asset on what you are
    #: billed. Both sides are kept because netting them at posting time loses
    #: the audit trail the return is built from.
    VAT_PAYABLE = "VAT Payable"
    VAT_RECEIVABLE = "VAT Receivable"
    #: Costs invoiced to the firm that are not one of the haulage categories.
    OPERATING_EXPENSE = "Operating Expense"


#: Accounts that belong on the profit and loss.
REVENUE_ACCOUNTS = (Account.REVENUE_LINEHAUL, Account.REVENUE_ACCESSORIAL,
                    Account.REVENUE_INVOICED)
EXPENSE_ACCOUNTS = (
    Account.FACTORING_FEE,
    Account.FUEL_EXPENSE,
    Account.TOLLS_EXPENSE,
    Account.MAINTENANCE_EXPENSE,
    Account.INSURANCE_EXPENSE,
    Account.DRIVER_PAY_EXPENSE,
    Account.OPERATING_EXPENSE,
)


@dataclass
class FuelLine:
    """One fill on a fuel card statement."""

    date: str | None = None
    truck: str | None = None
    location: str | None = None
    gallons: float | None = None
    gross: float = 0.0
    tax: float = 0.0
    reference: str | None = None


@dataclass
class RemittanceLine:
    """One load inside a broker's remittance advice.

    `gross` is what the broker says it is paying for that load, before the
    factoring fee that is charged once on the whole remittance. `deduction` is
    anything the broker held back on this specific load, with its stated
    reason. A load the broker short-paid without saying so shows up as `gross`
    below the load confirmation's rate with no deduction to explain it, and
    that is one of the exceptions the close is looking for.
    """

    load_ref: str
    gross: float = 0.0
    deduction: float = 0.0
    reason: str | None = None


@dataclass
class Document:
    """One classified, structured artifact from the month's mail.

    Amount fields mean different things per family, which is why each is named
    for what it is rather than reused. `source_file` is the traceability
    anchor: every journal line, every exception and every draft carries back to
    the artifact it came from.
    """

    doc_type: DocType
    period: str                                  # YYYY-MM
    source_file: str | None = None
    date: str | None = None
    counterparty: str | None = None
    document_number: str | None = None
    currency: str = "USD"

    # invoices and loads
    net_amount: float | None = None              # ex-tax charge, or the load rate
    tax_amount: float | None = None
    gross_amount: float | None = None            # net + tax
    truck: str | None = None

    # load confirmation
    load_ref: str | None = None
    miles: float | None = None
    accessorial: float | None = None             # detention, layover, lumper

    # broker remittance
    broker: str | None = None
    remittance_total: float | None = None        # what actually hits the bank
    factoring_fee: float | None = None
    lines: list[RemittanceLine] = field(default_factory=list)

    # fuel card statement
    fuel_lines: list[FuelLine] = field(default_factory=list)

    # driver settlement
    driver: str | None = None
    driver_gross: float | None = None
    driver_deductions: float | None = None
    driver_net: float | None = None
    tax_withheld: float | None = None

    # bank transaction
    direction: str | None = None                 # "in" | "out"
    reference: str | None = None

    # unreadable
    failure_reason: str | None = None


@dataclass
class JournalLine:
    account: Account
    debit: float = 0.0
    credit: float = 0.0


@dataclass
class JournalEntry:
    """A balanced double-entry posting derived from exactly one document."""

    date: str | None
    period: str
    memo: str
    lines: list[JournalLine] = field(default_factory=list)
    source_doc: str | None = None

    @property
    def is_balanced(self) -> bool:
        debits = sum(line.debit for line in self.lines)
        credits = sum(line.credit for line in self.lines)
        return abs(debits - credits) < 0.01


@dataclass
class Allocation:
    """One load settled out of a broker remittance.

    This is the beat that makes the month closeable. A remittance is a single
    bank credit; the books need it split back across the loads it settles, at
    the amount the broker actually paid each one, with the factoring fee booked
    once as an expense rather than smeared across the loads.
    """

    remittance_ref: str
    load_ref: str
    invoiced: float | None          # what the load confirmation said
    paid: float                     # what the remittance line pays
    deduction: float
    reason: str | None
    matched: bool                   # a load confirmation was found for this line
    settled_in_full: bool           # paid + deduction reconciles to invoiced


@dataclass
class AllocationResult:
    """The whole split of one remittance, plus the identity that proves it."""

    remittance_ref: str
    broker: str | None
    remittance_total: float
    factoring_fee: float
    allocations: list[Allocation] = field(default_factory=list)
    unmatched_load_refs: list[str] = field(default_factory=list)

    @property
    def allocated_gross(self) -> float:
        return round(sum(a.paid for a in self.allocations), 2)

    @property
    def residual(self) -> float:
        """Money in the remittance that no load line accounts for.

        The identity a remittance must satisfy: what landed in the bank equals
        what the lines pay, less the fee charged once. A non-zero residual is
        not a rounding curiosity, it is a real exception, and the close raises
        it rather than absorbing it into a suspense account.
        """
        return round(self.remittance_total - (self.allocated_gross - self.factoring_fee), 2)

    @property
    def reconciles(self) -> bool:
        return abs(self.residual) < 0.01


class ExceptionKind(str, Enum):
    """What the close found wrong. Every kind has a deterministic detector."""

    PAYMENT_WITHOUT_DOCUMENT = "payment_without_document"
    SHORT_PAY = "short_pay"
    DUPLICATE_CHARGE = "duplicate_charge"
    AMOUNT_OUTLIER = "amount_outlier"
    TAX_INCONSISTENCY = "tax_inconsistency"
    LOAD_UNPAID = "load_unpaid"
    OUT_OF_PERIOD = "out_of_period"
    UNREADABLE_DOCUMENT = "unreadable_document"
    #: Text came through fine; no rule recognised what KIND of document
    #: it is. Different failure from UNREADABLE and a more dangerous one,
    #: because the document looks healthy and posts nothing.
    UNRECOGNISED_DOCUMENT = "unrecognised_document"
    REMITTANCE_UNRECONCILED = "remittance_unreconciled"


@dataclass
class Finding:
    """One thing the books say is wrong, with the evidence that says so.

    `amount` and every figure inside `message` are computed by the detector.
    `likely_cause` and `suggested_action` are the only fields a model may
    phrase, and even those have a deterministic fallback in `narrator.py`, so
    the close never depends on a key.
    """

    kind: ExceptionKind
    severity: str                   # "error" | "warning" | "info"
    reference: str
    amount: float
    message: str
    counterparty: str | None = None   # who a corrective document would go to
    source_file: str | None = None
    likely_cause: str | None = None
    suggested_action: str | None = None
    confidence: float | None = None

    @property
    def actionable(self) -> bool:
        """Whether a corrective document can be drafted for this finding."""
        return self.kind in ACTIONABLE_KINDS


#: Kinds a corrective document exists for. An outlier, an out-of-period date
#: and an unreadable scan are reported and left to the owner: there is no
#: honest letter to write about them, and inventing one would be theatre.
ACTIONABLE_KINDS = frozenset({
    ExceptionKind.PAYMENT_WITHOUT_DOCUMENT,
    ExceptionKind.SHORT_PAY,
    ExceptionKind.DUPLICATE_CHARGE,
    ExceptionKind.LOAD_UNPAID,
})


class DraftKind(str, Enum):
    """The corrective artifact that fixes a finding."""

    DOCUMENT_REQUEST = "document_request"     # supplier, send us the missing bill
    SHORT_PAY_DISPUTE = "short_pay_dispute"   # broker, you underpaid this load
    DUPLICATE_REFUND = "duplicate_refund"     # supplier, you billed this twice
    PAYMENT_REMINDER = "payment_reminder"     # broker, this load is unpaid


@dataclass
class Draft:
    """A corrective document the close generated, filed and did not send.

    Filed is the operative word. The close writes this into Archon's own state
    without asking. What it never does is put it in front of a third party: the
    one human gate in the product sits on the outbound edge, and it is the only
    step of the chore a person performs.
    """

    kind: DraftKind
    recipient: str
    subject: str
    body: str
    amount: float
    reference: str
    finding_kind: ExceptionKind
    source_file: str | None = None
    status: str = "filed"           # never "sent"; there is no send path here


@dataclass
class ValidationResult:
    """One cross-document gate over the closed month."""

    rule: str
    passed: bool
    severity: str                   # "info" | "warning" | "error"
    message: str


@dataclass
class Statements:
    """The period result. Every field is a roll-up of the journal, nothing else."""

    period: str
    revenue_linehaul: float
    revenue_accessorial: float
    revenue: float
    fuel: float
    tolls: float
    maintenance: float
    insurance: float
    driver_pay: float
    factoring_fees: float
    operating_expenses: float
    net_profit: float
    cash_in: float
    cash_out: float
    net_cash: float
    accounts_receivable: float
    accounts_payable: float
    #: None when the month contains nothing with a mileage. See the note
    #: in Ledger.statements: 0 and "not applicable" are different facts.
    total_miles: float | None
    cost_per_mile: float | None
    revenue_per_mile: float | None
    per_truck: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
