

def test_a_clean_euro_month_is_reported_in_euro():
    """The regression the page could not survive: a month with NOTHING wrong.

    The browser took its currency from the first finding that carried one, so a
    clean month in euros had nothing to read it from and rendered as dollars.
    G7 passes on that month, which is the point -- the one close whose figures
    need no explanation was the one shown in the wrong currency.
    """
    import pathlib

    from archon.adapters.store import LocalStore
    from archon.domain.extract import extract_document
    from archon.runtime.close import run_close

    root = pathlib.Path(__file__).resolve().parents[2] / "corpus" / "2026-07"
    documents = []
    for path in sorted(root.glob("load-*.txt")):
        text = path.read_text(encoding="utf-8").replace("Linehaul Rate:", "Currency: EUR\nLinehaul Rate:")
        documents.append(extract_document(text, source_file=path.name, period="2026-07"))

    result = run_close(period="2026-07", documents=documents, company="X",
                       store=LocalStore(), commit=False)

    g7 = next(g for g in result.gates if g.rule.startswith("G7"))
    assert g7.passed, g7.message
    assert result.currency == "EUR", "the month is in euros and the close does not say so"
    assert result.to_dict()["currency"] == "EUR", "the payload the page reads has no currency"
