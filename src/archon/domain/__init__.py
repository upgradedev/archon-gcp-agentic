r"""The business core. Pure, deterministic, and free of every SDK.

Nothing in this package imports Google ADK, Firestore, FastAPI or the network.
That is not a style preference, it is the property the whole product rests on:
every figure the owner ever sees is computed here, so a wrong number is a bug
with a failing test rather than a hallucination with a plausible tone.

The rule is checkable rather than promised. `grep -rl "google\.\|fastapi\|smtplib"
src/archon/domain/` returns nothing, and a test asserts it.
"""
