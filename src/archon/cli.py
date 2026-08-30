"""Run the close from a terminal.

    python -m archon.cli                 close the bundled month, print the trail
    python -m archon.cli --json          the same close as machine-readable JSON
    python -m archon.cli --agent         let the ADK agent drive the same tools

The first form needs nothing but Python: no key, no credential, no network.
That is deliberate. Anyone who clones this repository can watch the chore
complete before deciding whether to believe the rest of it.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import PERIOD
from .runtime.close import run_close
from .runtime.mailbox import read_period


def _print_close(result) -> None:
    print(result.journal.transcript())
    print()
    print(result.summary)
    print()
    statements = result.statements
    print(f"  revenue          {statements.revenue:>12,.2f}")
    print(f"  operating costs  {statements.operating_expenses:>12,.2f}")
    print(f"  net profit       {statements.net_profit:>12,.2f}")
    if statements.cost_per_mile is not None:
        print(f"  cost per mile    {statements.cost_per_mile:>12,.3f}")
        print(f"  revenue per mile {statements.revenue_per_mile:>12,.3f}")
    print()
    # "being chased" was a lie by one tense. The letters are written and filed
    # unsent; nobody has chased anybody. The figure is what the letters would
    # recover if the owner approved them, which is a different sentence.
    print(f"  {len(result.findings)} exception(s), {len(result.drafts)} draft(s) filed, "
          f"{result.recoverable:,.2f} recoverable once you approve them")
    for draft in result.drafts:
        print(f"    [{draft.status}] {draft.kind.value} to {draft.recipient}: {draft.subject}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archon", description=__doc__)
    parser.add_argument("--period", default=PERIOD, help="period to close, YYYY-MM")
    parser.add_argument("--json", action="store_true", help="emit the close as JSON")
    parser.add_argument("--agent", action="store_true",
                        help="the ADK agent chooses the tool calls (needs a Gemini credential)")
    args = parser.parse_args(argv)

    documents, raw = read_period(args.period)

    # `--agent` used to swap in the narrator and nothing else, while its help
    # text said it drove the close through the ADK agent. It now does what it
    # says: the agent chooses the tool calls, and a failure is reported rather
    # than silently downgraded, because a flag that quietly does something
    # smaller than it claims is how the claim stops being true.
    if args.agent:
        from .adapters.agents import gemini_narrator, run_agent_close

        result, final = run_agent_close(
            period=args.period, company="Bell Ridge Haulage",
            narrator=gemini_narrator(),
        )
        if result is None:
            print("the agent did not produce a close", file=sys.stderr)
            if final:
                print(final, file=sys.stderr)
            return 2
    else:
        result = run_close(period=args.period, documents=documents,
                           company="Bell Ridge Haulage", raw_texts=raw)

    if args.json:
        json.dump(result.to_dict(), sys.stdout, indent=2, default=str)
        print()
    else:
        _print_close(result)

    # A blocked close is a non-zero exit. A pipeline that treats "the books do
    # not add up" as success is a pipeline that will one day ship books that do
    # not add up.
    return 0 if result.closed else 1


if __name__ == "__main__":
    raise SystemExit(main())
