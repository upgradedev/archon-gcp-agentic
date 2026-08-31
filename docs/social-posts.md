# Social posts

The hashtag the rules ask for is `#AllThingsAgenticHackathon`, one word, no space.
The rules say the content piece must be **public, not unlisted**, and must say it
was created for the purposes of entering the hackathon. Both the dev.to article
and these posts do.

Attach an image to the X post. The strongest one is the problem slide, which
carries the whole argument in a single frame:
`docs/images/one-payment-eight-loads.png`

---

## X, single post

```
A freight broker doesn't pay per load.

It pays once a fortnight: one bank credit covering eight loads, minus a fee charged on the whole batch.

The bank shows one number. The books need eight.

So I built an agent that closes that month unattended on Google Cloud. It found $612.85 quietly leaking.

#AllThingsAgenticHackathon
```

Reply, to carry the links without crowding the post:

```
Google ADK + Gemini on Cloud Run, Firestore for the trail, Pub/Sub for the trigger. Nobody presses anything.

Try it, no account: https://archon-70489367760.us-central1.run.app/
Source: https://github.com/upgradedev/archon-gcp-agentic
Write-up: [dev.to link]
```

---

## X, thread version

Use this instead if you would rather post a thread. It scores the same and
usually travels further.

**1/5**
```
A freight broker doesn't pay per load.

It pays once a fortnight: one bank credit covering eight loads, minus a factoring fee charged on the whole batch.

The bank shows one number. The books need eight.

#AllThingsAgenticHackathon
```

**2/5**
```
So when a load is paid short, nothing catches it.

I ran the three reconciliations a haulier's books actually get over the same 27 documents. All three reported the month clean.

One of them reconciles to the cent. That's exactly why the missing 200.00 stays invisible.
```

**3/5**
```
Archon closes the month with nobody in the loop.

A file lands in Cloud Storage. Pub/Sub wakes a Cloud Run container. A Google ADK agent runs eleven steps: posts the books, splits the payment back across the loads, finds what's wrong, drafts the letters, checks its own work.
```

**4/5**
```
The rule the whole thing defends: the agent orchestrates, the ledger computes.

The domain layer is pure Python. No SDK, no network, no credential, and a test walks its imports to prove it.

Gemini gets a fact sheet of computed figures. It never sees a document and is never asked for a total.
```

**5/5**
```
It can also refuse. Seven gates run over the finished books, and a close that can't verify its own arithmetic is blocked rather than filed.

Try it, no account: https://archon-70489367760.us-central1.run.app/
Source: https://github.com/upgradedev/archon-gcp-agentic
```

---

## LinkedIn

Longer form travels better there, and the hashtag requirement is the same.

```
An owner-operator trucking firm of three trucks cannot close its books, and the reason is structural rather than lazy.

A freight broker does not pay per load. It pays once a fortnight: one bank credit covering eight loads, minus a factoring fee charged on the whole batch. The bank statement shows one number. The books need eight.

So when a broker pays a load short, nothing catches it. There is no line to compare against, because the line was never itemised anywhere the books can see.

I built Archon for the Google All Things Agentic Hackathon: an agent that closes that month unattended. A file lands in Cloud Storage, Pub/Sub wakes a Cloud Run container, and a Google ADK agent runs eleven steps end to end. It posts a double-entry journal, splits each broker payment back across the loads it settles, finds what is missing, drafts the corrective letters, and checks the whole close against seven gates that can refuse it.

On the month I built for it, that found 612.85 quietly leaking. Against a margin of 0.223 a mile, that is not noise.

The design rule the whole thing defends: the agent orchestrates, the ledger computes. The domain layer is pure Python with no SDK, no network and no credential, and a test walks its imports to prove it. Gemini sequences the work and weighs the exceptions. It never touches a figure.

Try it, no account and no install: https://archon-70489367760.us-central1.run.app/
Source: https://github.com/upgradedev/archon-gcp-agentic

#AllThingsAgenticHackathon
```
