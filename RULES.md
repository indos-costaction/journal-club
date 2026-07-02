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

- **Submit** the annotated PDF (via the private Form) and comment `/submit <ID>` before the deadline.
- **Withdraw** (`/withdraw <ID>`) any time before the deadline — no penalty; it returns to the pool.
- **Extend** (`/extend <ID>`) once for **+7 days**. One extension per claim.
- **Expiry:** at day 12 the paper auto-returns to the pool — no penalty, zero points.
- **Partial reviews are not accepted** — withdraw rather than submit an incomplete review.

## What a review is

The paper's **PDF with inline, typed annotations** (no handwriting/scans) marking: (a) what you
did **not understand**; (b) what has been **contested/superseded** since publication; (c) what
**matters for INDoS** (data-sharing, standardization, reproducibility). **No AI** may be used to
read, summarise, or annotate. You confirm this at sign-up.

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
