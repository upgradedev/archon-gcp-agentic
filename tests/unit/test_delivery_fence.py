"""The fence on the one action that leaves the process, tested on the method
the product actually calls.

`FencedDelivery` implemented `send()`. The `Deliverer` protocol declares
`deliver()`, and `run_close` calls `deliver()`. A `__getattr__` forwarded
everything else to the wrapped deliverer, so the real delivery path went
straight through the fence without touching it.

The test that was supposed to cover this asserted a counter on a double whose
method was also `send()`, so the counter stayed at zero because nothing was
ever called at all. It passed for the wrong reason, which is worse than
failing: it reported a guard that did not exist.
"""
from __future__ import annotations

import pytest

from archon.adapters.store import ClaimLost, FencedDelivery, FencedStore, LocalStore

COMPANY = "acme"
KEY = "2026-07#event-mail_2026-07__READY@1"


class Spy:
    """A deliverer with the protocol's own method name."""

    channel = "spy"

    def __init__(self) -> None:
        self.delivered = 0

    def deliver(self, digest):
        self.delivered += 1
        return {"channel": "spy", "delivered": True}


def held(attempt: int) -> dict:
    return {"period": "2026-07", "attempt": attempt, "status": "processing"}


@pytest.fixture
def rig():
    store = LocalStore()
    store.claim(COMPANY, KEY, held(1))
    fence = FencedStore(store, COMPANY, KEY, 1)
    spy = Spy()
    return store, fence, FencedDelivery(spy, fence), spy


def test_the_holder_delivers(rig):
    """The fence has to let the product work."""
    _store, fence, guarded, spy = rig

    guarded.deliver("digest")

    assert spy.delivered == 1
    assert fence.checks > 0, "delivery went out without the fence being consulted"


def test_a_superseded_worker_delivers_nothing(rig):
    """The regression. Ownership is checked on `deliver`, not on a method
    nothing calls, and a lost claim stops the message before it leaves."""
    store, fence, guarded, spy = rig
    store.save_close(COMPANY, KEY, held(2))          # somebody else took it over

    with pytest.raises(ClaimLost):
        guarded.deliver("digest")

    assert spy.delivered == 0, "a superseded worker delivered to the owner"
    assert fence.checks > 0


def test_the_wrapper_still_answers_for_the_channel(rig):
    """The receipt reads `channel` off the deliverer, and a wrapper that hides
    it made every fenced close report `channel: unknown`."""
    _store, _fence, guarded, _spy = rig

    assert guarded.channel == "spy"


def test_the_wrapper_implements_the_protocol_it_claims():
    """Named rather than inferred, because the defect WAS the method name.

    A wrapper that satisfies a protocol by accident, through `__getattr__`,
    satisfies it for reads and bypasses it for calls.
    """
    from archon.adapters.delivery import Deliverer

    protocol_methods = {name for name in vars(Deliverer)
                        if not name.startswith("_") and callable(getattr(Deliverer, name, None))}

    for name in protocol_methods:
        assert name in vars(FencedDelivery), (
            f"FencedDelivery does not implement {name!r}; __getattr__ would forward it "
            f"past the fence")
