# How to claim and review a paper

No coding needed — just a free GitHub account and a PDF reader.

## 1. Create a GitHub account
If you don't have one: <https://github.com/signup> (free). That's the only account required.

## 2. Browse the pool
Open the [pool + leaderboard](https://indos-costaction.github.io/journal-club/). Filter by modality or
tick **"Needs reviews only"** to find papers still short of 3 reviews. Note the **IDs** (e.g. `EEG-03`).

## 3. Claim up to three
Click **Claim** on any open paper (it opens a pre-filled issue), or open a
[**Claim papers**](https://github.com/indos-costaction/journal-club/issues/new?template=claim.yml) issue
yourself. Fill in:
- the **paper IDs** (up to 3, e.g. `EEG-03 FMRI-11`),
- **attribution** (your handle, or an anonymous pseudonym on the board),
- the **consent** checkbox (required — see [`CONSENT.md`](CONSENT.md)).

Submit. A bot replies within a minute with your papers, their **deadlines** (12 days each), and an
**Upload link for each one**. It also assigns the issue to you, so GitHub emails you reminders.

## 4. Read and annotate
Get each PDF through **your institution's library** (can't access one? contact the organizers).
Use your PDF reader's comment tool (Acrobat, Preview, Okular, Zotero…) to leave **typed** inline
comments. Mark what you didn't understand, what has been contested/superseded, and what matters for
INDoS. A wrap-up comment at the end is encouraged. **No AI.**

## 5. Upload before the deadline
Open the **Upload link** for that paper — it's in the bot's confirmation comment on your claim issue,
and in every deadline reminder. Drop the annotated PDF. **That's the whole step**: the link already
knows which paper is yours, so there is nothing to type and no command to remember. We confirm on your
thread once it lands, then grade it.

> Use the Upload link *for the paper you're submitting* — each is different. A file uploaded without
> one can't be matched to your claim, and won't be graded. There is deliberately no generic link on the
> website.

## Managing your claims (comment on your claim issue)

| Comment | Effect |
|---|---|
| `/claim EEG-05` | claim another paper (if under your 3-claim cap) |
| `/withdraw EEG-03` | return a paper to the pool — no penalty |
| `/extend EEG-03` | one-time **+7 days** on the deadline |

You don't need `/submit` — uploading *is* submitting. (It still exists, and organizers use it to record
your upload once it's in hand.)

## Reminders
The system @-mentions you **3 days** and **1 day** before each deadline (GitHub emails you), and each
nudge repeats that paper's Upload link. At day 12 an unsubmitted paper returns to the pool
automatically — no penalty. Reply `/extend` or `/withdraw` if you need to.
