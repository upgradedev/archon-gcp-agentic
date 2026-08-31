"""`--mail` has to reach every path, including the one that costs money.

The flag reads a directory of your own documents. The deterministic path used
them. The agent path did not: `run_agent_close` accepts `documents`, `raw` and
`source`, the CLI passed none of them, and the agent closed the bundled corpus
instead. Nothing failed. You got a close, it looked right, and it was somebody
else's month.
"""
from __future__ import annotations

import pathlib

import pytest

from archon import cli

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: An amount that appears nowhere in the shipped corpus, so a close that
#: reports it can only have read the documents handed in.
SENTINEL = 9_876.54


def a_month(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "books"
    (root / "2026-07").mkdir(parents=True)
    (root / "2026-07" / "load-Z-0001.txt").write_text(
        "SENTINEL HAULAGE\n"
        "RATE CONFIRMATION\n\n"
        "Document Type: Load Confirmation\n"
        "Load Number: Z-0001\n"
        "Date: 2026-07-11\n"
        "Broker: Sentinel Freight\n"
        "Carrier Unit: T-900\n"
        "Origin: Fargo ND\n"
        "Destination: Duluth MN\n"
        "Miles: 240\n"
        f"Linehaul Rate: {SENTINEL:,.2f}\n"
        f"Total Payable: {SENTINEL:,.2f}\n",
        encoding="utf-8")
    return root


def test_the_deterministic_path_reads_the_directory(tmp_path, capsys):
    root = a_month(tmp_path)

    assert cli.main(["--mail", str(root), "--period", "2026-07", "--json"]) == 0

    assert f"{SENTINEL}" in capsys.readouterr().out.replace(",", "")


def test_the_agent_path_is_handed_the_same_documents(tmp_path, monkeypatch):
    """The regression. The agent was given nothing and read the demo month."""
    root = a_month(tmp_path)
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr("archon.adapters.agents.run_agent_close", spy)
    monkeypatch.setattr("archon.adapters.agents.gemini_narrator", lambda: None)

    with pytest.raises(SystemExit):
        cli.main(["--agent", "--mail", str(root), "--period", "2026-07"])

    documents = seen.get("documents")
    assert documents, "the agent was handed no documents and would close the bundled month"
    assert [d.source_file for d in documents] == ["load-Z-0001.txt"]
    assert seen.get("raw"), "the agent was handed no raw text, so lineage cannot be proved"
    assert any(str(SENTINEL) in text.replace(",", "") for text in seen["raw"].values())
    assert (seen.get("source") or {}).get("mailbox") != "bundled-sample", seen.get("source")
