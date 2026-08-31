---
title: "One bank credit, eight loads: building an agent that closes a trucking firm's books while nobody watches"
published: false
description: "A freight broker pays once a fortnight, one credit covering eight loads, minus a fee charged on the whole batch. The bank shows one number and the books need eight. Here is the agent I built to close that month unattended on Google Cloud, and the three bugs that were hiding under a green test suite."
tags: googlecloud, ai, python, showdev
cover_image: https://raw.githubusercontent.com/upgradedev/archon-gcp-agentic/main/docs/images/cover.png
---

*I built this for the Google All Things Agentic Hackathon, and I wrote this article for the purposes of entering that hackathon.*

## The problem is arithmetic nobody does on a Sunday

An owner-operator trucking firm of three trucks cannot close its books, and the reason is structural rather than lazy.

A freight broker does not pay per load. It pays once a fortnight: **one bank credit covering eight loads, minus a factoring fee charged on the whole batch.** The bank statement shows one number. The books need eight.

![One payment, eight loads](https://raw.githubusercontent.com/upgradedev/archon-gcp-agentic/main/docs/images/one-payment-eight-loads.png)

So when a broker pays a load short, nothing catches it. The owner opens the bank statement, sees a credit, and moves on. There is no line to compare against, because the line was never itemised in a place the books can see.

On the month I built for this, that hid **$612.85**. Against a margin of $0.223 a mile, that is not noise.

Here is the part I find most interesting. I wrote three reconciliations that a haulier's books actually get, and ran all three over the same 27 documents:

| the method | the verdict it reaches | recovered |
|---|---|---|
| Match the bank credit to an invoice | No invoice equals the credit, because the credit covers eight. Nothing to compare | 0.00 |
| Credit plus fee against the remittance total | **It reconciles, to the cent.** So the month is reported clean | 0.00 |
| Each remittance line against the agreed rate | One flag, and it is a false one, on a load the broker OVERPAID | 0.00 |
| Each line against what was actually invoiced | L-7105 invoiced 2,460.00, paid 2,260.00 | **200.00** |

The second row is the whole problem. 18,667.65 credited plus 577.35 charged once on the batch equals the remittance exactly. The arithmetic a careful bookkeeper does by hand comes back clean while the money is gone. Nothing that checks the batch can see it. Only something that checks each line against what was invoiced can, and that is the one comparison the paperwork does not hand you.

## What I built

Archon closes the month with nobody in the loop. A file lands in Cloud Storage, the bucket notifies Pub/Sub, its push subscription wakes a Cloud Run container, and a Google ADK agent runs eleven steps: it classifies the month's artifacts, posts a double-entry journal, splits each remittance back across the loads it settles, reconciles those against what was actually paid, finds what is missing, decides what to do about each exception, drafts the corrective letters, checks the whole close against seven gates, writes the summary, files the period, and composes the owner's letter.

Nobody is asked anything at any point. The single human act is approving letters that would leave for a third party.

## The architecture, and the one rule it defends

![Archon architecture](https://raw.githubusercontent.com/upgradedev/archon-gcp-agentic/main/docs/images/architecture.png)

**The agent orchestrates, the ledger computes.**

`archon.domain` is pure Python. No ADK, no Firestore, no FastAPI, no network. A test walks its imports and fails if any of them reaches a credential or a socket. Gemini is handed a fact sheet of already computed figures. It is never handed a document and never asked for a total.

You can check that boundary rather than trust it. Run the month under standing policy, then again under an agent told to chase nothing. Every figure is identical and only the dispositions differ. If the model were doing arithmetic, the figures would move.

That rule is why the whole test suite runs offline with no key, and why every number in the demo reproduces on a laptop.

## It can refuse

Seven gates run over the finished books. An agent may withhold a month it does not trust. It may never grant one the gates refused.

![The gates panel](https://raw.githubusercontent.com/upgradedev/archon-gcp-agentic/main/docs/images/gates.png)

One detail I got wrong and had to fix: a gate whose inputs are absent used to report "passed". So a thin month printed "7/7 gates passed" when two of them had checked nothing. That is a stronger claim than the run can carry. Now a gate with nothing to check reports *skipped*, and the headline reads "5/5 gates passed, 2 skipped".

Each gate is also broken on purpose in the suite and asserted red, because a gate nobody has watched fail is a gate nobody should believe.

## Three bugs that were hiding under a green suite

This is the part worth your time if you are building agents.

### 1. A store that said yes to everything

`get_store()` returned a **new** in-memory store on every call.

Nothing written was ever read back. So the idempotency marker was claimed in one throwaway object and looked for in another, and a redelivered Pub/Sub message closed the month twice. With the agent switched on, that is two model conversations and two owner digests for one event.

Seven hundred tests missed it, because an always-empty store approves every claim. Firestore was never affected, so it was broken exactly where the tests and the demo run, and nowhere a deployment would show it.

### 2. A fence that had never once executed

A superseded worker was supposed to be stopped before it could send the owner's digest. Cloud Run can keep a container running after a request times out, so a worker whose lease expired mid-close is a real thing, not a hypothetical.

The guard implemented `send()`. The protocol declares `deliver()`.

It had never run. And my own test used a test double with the wrong method name, so its call counter stayed at zero and the test passed. I found it by measuring what actually executed rather than by reading the code.

### 3. Two gates contradicting each other on the same screen

The unreadable scan in the month carries a zero-line memo entry, so the trail records that it arrived. Gate five reported it, correctly, as "none posted". Gate four counted that memo among the documents that "each posted once".

So the checks panel said 27 documents posted and, two rows down, that one of them had not. A judge reading both rows is entitled to conclude one of the gates is decorative.

Every one of these was fixed with its failing reproduction committed first.

## Running on Google Cloud, and showing it

![Cloud Run revisions](https://raw.githubusercontent.com/upgradedev/archon-gcp-agentic/main/docs/images/cloud-run-revisions.png)

Cloud Run, Cloud Storage, Pub/Sub, Firestore Native and Vertex AI, declared in Terraform and deployed through Workload Identity Federation, so no service account key exists anywhere. That last part is visible in the screenshot above: the revision was deployed by a service account, not by a key someone pasted into a secret.

`POST /events` verifies a Google signed OIDC token for the service's own audience, minted by one named service account. An anonymous call gets a 403.

## What I would tell my past self

**A test that has never been watched to fail is not evidence.** Two of the three bugs above were sitting under green suites. Both were found by measuring what executed, not by reading code.

**Numbers rot in prose.** Every count this repository states, including the ones spoken in the demo video, is now pinned by a test that reads the code and compares. Several of those tests caught drift before I did.

**Skipped is not passed.** If your system reports a check that did not run, it is telling the reader something untrue in a way that looks like diligence.

## Try it

No account, no install: **https://archon-70489367760.us-central1.run.app/**

Press "Guided tour" for the eight beats in order, or "Watch the agent" to walk the eleven steps of a close that a Cloud Storage object already triggered. There is also "Your own month", which closes a month of your own text documents in memory, stored nowhere and never sent to a model.

Source: **https://github.com/upgradedev/archon-gcp-agentic**

Bell Ridge Haulage is synthetic. Every firm, broker, supplier and figure is invented, and the counterparty names were checked against public carrier registries to make sure none belongs to a real business.
