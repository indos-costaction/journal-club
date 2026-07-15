# Rules

The federation rules, as the tracking system enforces them. Every number here is a
WG3-ratifiable parameter set in [`scripts/params.py`](scripts/params.py); the authoritative
rationale is the initiative's `mechanics.md` and `grading-rubric.md`.

## Claiming

- You may hold at most **3 active claims** at a time (across all your claim threads).
- A paper **closes to new claims once 5 people hold it** — enough to expect ≥3 completed reviews.
- Claiming is first-come; there is no queue. A closed paper re-opens if a claim is withdrawn or
  expires and its live-claim count drops below 5 while it still needs reviews.
- Each claim runs for **12 days**.

## Turning a paper around

- **Upload** the annotated PDF before the deadline, through that paper's **Upload link** (in the bot's
  comment on your claim issue, and in every reminder). We check it is readable and matches your claim,
  then post `/received <ID>` on your thread.
- **Sign it off** (`/confirm <ID>`) on that thread. This is the one command **only you** can send —
  organizers cannot do it for you, and that is exactly the point. The upload form is open to anyone
  holding the link, so it cannot prove *who* sent a file; your `/confirm`, from your GitHub account,
  is what puts your name on the review and on the **no-AI declaration**. The bot @-mentions you and
  GitHub emails you, so there is nothing to remember — you reply.
- **The clock stops the moment we receive your upload.** A received paper never expires, so taking
  your time over the sign-off costs you nothing and puts nothing at risk.
- **Withdraw** (`/withdraw <ID>`) any time before the deadline — no penalty; it returns to the pool.
- **Extend** (`/extend <ID>`) once for **+7 days**. One extension per claim.
- **Expiry:** at day 12 a paper you have not uploaded auto-returns to the pool — no penalty, zero points.
- **Partial reviews are not accepted** — withdraw rather than hand in an incomplete review.
- **Reviews that break the rules are removed** (`/reject <ID>`, organizers only) — including after
  grading. The rules are the ones on this page; the one that matters most is no AI.

## What a review is

The paper's **PDF with inline, typed annotations** (no handwriting/scans) marking: (a) what you
did **not understand**; (b) what has been **contested/superseded** since publication; (c) what
**matters for INDoS** (data-sharing, standardization, reproducibility). **No AI** may be used to
read, summarise, or annotate. You declare this per review, by signing it off with `/confirm <ID>`
on your claim thread — that declaration is made from your GitHub account, in your name.

## Grading — the 5-axis rubric

Each completed review is scored **0–5 per axis**, combined with fixed weights (sum = 1.0), giving a
weighted score on the same 0–5 scale:

| Axis | Weight | Rewards |
|---|---|---|
| Engagement / coverage | 0.15 | annotation count + spread across the whole paper |
| Comprehension | 0.20 | specific, located "what I didn't understand" notes |
| Critical appraisal | 0.30 | contesting claims, flagging superseded/corrected work, method critique |
| Action relevance | 0.20 | the data-sharing / standardization / reproducibility angle |
| Originality / non-AI | 0.15 | your own voice and judgement |

`weighted = 0.15·engagement + 0.20·comprehension + 0.30·critical + 0.20·action + 0.15·originality`

- **Quality floor 2.0/5.** Below it, a review is returned with feedback, earns **zero points**, and
  does not count toward the paper's completion.
- Grading is AI-assisted and human-calibrated (organizers spot-check a daily sample, everything near
  the floor, AI-flagged anomalies, and appeals). Organizers grade with AI; **participants may not use
  AI to review** — the one asymmetry, stated openly.

## The leaderboard

```
participant_points = Σ over your floor-passing completed reviews ( weighted )
```

Only floor-passing completed reviews count; withdrawals, expiries, and below-floor reviews earn nothing —
so finishing **more good reviews** is the only way to climb. Tie-breakers, in order: (1) number of
completed reviews, (2) mean score, (3) earliest to reach your current total. The board refreshes daily.
Your standing in early August is a **major input (not the sole gate)** to Training-School invitations.
