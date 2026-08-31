"""Counting things in English.

Every number this product prints is attached to a noun, and the noun was
written as `document(s)` in fourteen places. That form is a note to the reader
that nobody decided; it reads as unfinished on a screen an owner is looking at
and on a terminal a judge is watching. One helper, so the decision is made once
and the plural of an irregular noun has somewhere to live.
"""


def article(count: int) -> str:
    """"an 11-step trail", not "a 11-step trail".

    English takes the article from the SOUND of the next word, and "eleven"
    and "eight" open with a vowel while every other digit under twenty does
    not. The trail line hardcoded "a" beside a number it computed, so the one
    length the close actually has was the one it got wrong.
    """
    head = str(count)
    return "an" if head[0] == "8" or head.startswith("11") or head.startswith("18") else "a"


def plural(count: int, one: str, many: str | None = None) -> str:
    """`plural(1, "document")` -> "1 document"; `plural(3, "document")` -> "3 documents".

    Pass `many` for anything English does not pluralise with an `s`, including
    a whole phrase whose verb has to agree: `plural(n, "line pays", "lines pay")`.
    """
    return f"{count} {one if count == 1 else (many if many is not None else one + 's')}"
