# INDoS Federated Journal Club — tracking & leaderboard

The infrastructure for WG3's [Federated Journal Club](https://indos-costaction.github.io/journal-club/):
claim tracking, deadline reminders, grading ledger, and a live leaderboard — running entirely on
**GitHub Issues + Actions + Pages**. No server, no database, no paid services.

**Git is the database and the audit log.** The truth is a set of small per-entity files; everything
the public reads is a pure, idempotent recompute — so every Action is safe to re-run.

## How it works

| You do | Where | What happens |
|---|---|---|
| Claim ≤3 papers | open a **Claim papers** issue (or `/claim <ID>`) | a bot validates the cap + availability, sets 12-day deadlines, assigns the issue to you |
| Manage a claim | comment `/withdraw <ID>`, `/submit <ID>`, `/extend <ID>` | withdraw frees the slot; submit marks it for grading; extend adds a one-time +7 days |
| Get reminded | — | the daily sweep @-mentions you at day 9 and day 11; day 12 auto-returns the paper (no penalty) |
| Submit a review | upload the annotated PDF to the **private Form** + `/submit <ID>` | organizers grade it against the 5-axis rubric; your score enters the leaderboard |

The **pool + leaderboard** live at <https://indos-costaction.github.io/journal-club/>.

## Layout

```
docs/                 GitHub Pages site (index.html + app.js) and the web-served JSONs:
  data/pool.json      static paper identity (seeded from the curated lit-db)
  data/status.json    DERIVED per-paper: live_claims, completed_reviews, status, outstanding_need
  data/ranking.json   DERIVED leaderboard
claims/<issue>.json   AUTHORITATIVE dynamic truth — one file per claim issue
ledger/<claim>.json   AUTHORITATIVE scores — one file per graded review
scripts/              state.py (engine) · seed_pool.py · issue_ops.py · sweep.py · rank.py · grade.py · params.py
.github/workflows/    issue-ops.yml · daily-sweep.yml · grade.yml
RULES.md · HOWTO-claim.md · CONSENT.md
```

## The rules (defaults in `scripts/params.py`)

≤3 active claims/person · paper closes at 5 claimants · done at 3 reviews · 12-day deadline ·
one +7-day extension · rubric 5 axes (0–5, weights sum to 1) · quality floor 2.0/5.
See [`RULES.md`](RULES.md).

## Setup (one-time, for organizers)

1. Create the public repo `indos-costaction/journal-club`; push this tree.
2. `Settings → Pages → Source → GitHub Actions` (the `pages.yml` workflow builds and
   deploys `docs/`, generating `docs/data/site.json` from the repo slug at build time —
   it is never committed).
3. `Settings → Actions → General → Workflow permissions → Read and write`.
4. Add repo **variable** `ORGANIZERS` = comma-separated GitHub handles (default: `oesteban,guiomarniso`).
5. Re-seed the pool if the lit-db changes: `python scripts/seed_pool.py --source /path/to/references/lit-db`.

Grading, the private annotated-PDF store (Google Form → restricted Drive), and the open-data deposit
are documented in the WG3 initiative folder (`copyright-and-data.md`, `grading-rubric.md`).
