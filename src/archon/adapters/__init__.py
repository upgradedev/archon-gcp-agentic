"""Everything that touches an SDK or the world outside this process.

Firestore, Google ADK, FastAPI, SMTP. Each one sits behind an interface the
layers above are written against, and each has an offline implementation that
is the default: `LocalStore`, `FiledDelivery`, the deterministic narrator.

That is why the whole test suite and the bundled demo run with no key, no
credential and no network, and why the books are identical either way.
"""
