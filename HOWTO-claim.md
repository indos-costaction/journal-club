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

Submit. A bot replies within a minute confirming your claims and **deadlines** (12 days each), and
assigns the issue to you so GitHub emails you reminders.

## 4. Read and annotate
Get each PDF through **your institution's library** (can't access one? contact the organizers).
Use your PDF reader's comment tool (Acrobat, Preview, Okular, Zotero…) to leave **typed** inline
comments. Mark what you didn't understand, what has been contested/superseded, and what matters for
INDoS. A wrap-up comment at the end is encouraged. **No AI.**

## 5. Submit before the deadline
Upload your annotated PDF via the **private submission Form** (link on the site — it's not public),
then comment **`/submit <ID>`** on your claim issue. Organizers verify the upload and grade it.

## Managing your claims (comment on your claim issue)

| Comment | Effect |
|---|---|
| `/claim EEG-05` | claim another paper (if under your 3-claim cap) |
| `/withdraw EEG-03` | return a paper to the pool — no penalty |
| `/submit EEG-03` | mark it submitted for grading (after uploading via the Form) |
| `/extend EEG-03` | one-time **+7 days** on the deadline |

## Reminders
The system @-mentions you **3 days** and **1 day** before each deadline (GitHub emails you). At day 12
an unsubmitted paper returns to the pool automatically — no penalty. Reply `/extend` or `/withdraw` if
you need to.
