"""Counting things in English.

Every number this product prints is attached to a noun, and the noun was
written as `document(s)` in fourteen places. That form is a note to the reader
that nobody decided; it reads as unfinished on a screen an owner is looking at
and on a terminal a judge is watching. One helper, so the decision is made once
and the plural of an irregular noun has somewhere to live.
"""


def plural(count: int, one: str, many: str | None = None) -> str:
    """`plural(1, "document")` -> "1 document"; `plural(3, "document")` -> "3 documents".

    Pass `many` for anything English does not pluralise with an `s`, including
    a whole phrase whose verb has to agree: `plural(n, "line pays", "lines pay")`.
    """
    return f"{count} {one if count == 1 else (many if many is not None else one + 's')}"
