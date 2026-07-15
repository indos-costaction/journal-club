# INDoS Federated Journal Club — tracking & leaderboard

The infrastructure for WG3's [Federated Journal Club](https://indos-costaction.github.io/journal-club/):
claim tracking, deadline reminders, grading ledger, and a live leaderboard — running entirely on
**GitHub Issues + Actions + Pages**. No server, no database, no paid services.

**Git is the database and the audit log.** The truth is a set of small per-entity files; everything
the public reads is a pure, idempotent recompute — so every Action is safe to re-run.

## How it works

| You do | Where | What happens |
|---|---|---|
| Claim ≤3 papers | open a **Claim papers** issue (or `/claim <ID>`) | a bot validates the cap + availability, sets 12-day deadlines, assigns the issue to you, and replies with a per-paper **Upload link** |
| Manage a claim | comment `/withdraw <ID>`, `/extend <ID>` | withdraw frees the slot; extend adds a one-time +7 days |
| Get reminded | — | the daily sweep @-mentions you at day 9 and day 11 (repeating the Upload link); day 12 auto-returns a paper you haven't uploaded (no penalty) |
| Hand in a review | open that paper's **Upload link** and drop the annotated PDF | the link carries the claim key, so organizers' `intake.py` matches it and posts `/received <ID>` on your thread — which **stops the deadline clock** |
| Sign it off | reply `/confirm <ID>` when the bot asks | it's graded against the 5-axis rubric and your score enters the leaderboard |

The upload form is open-access and its per-paper link is published in a public issue, so a file proves
only that *someone* had the link — it cannot assert authorship. The claimant's `/confirm` is what makes
a file a review, and the only place the no-AI declaration acquires an authenticated signatory. It is
therefore **the one command an organizer cannot proxy**; `/received` and `/reject` are organizer-only,
and `/claim`, `/withdraw`, `/extend` work either way. See `CLAUDE.md` for the state machine.

The **pool + leaderboard** live at <https://indos-costaction.github.io/journal-club/>.

## Layout

```
docs/                 GitHub Pages site (index.html + app.js) and the web-served JSONs:
  data/pool.json      static paper identity (seeded from the curated lit-db)
  data/status.json    DERIVED per-paper: live_claims, completed_reviews, status, outstanding_need
  data/ranking.json   DERIVED leaderboard
claims/<issue>.json   AUTHORITATIVE dynamic truth — one file per claim issue
ledger/<claim>.json   AUTHORITATIVE scores — one file per graded review
scripts/              state.py (engine) · messages.py (all participant-facing prose) · seed_pool.py
                      issue_ops.py · sweep.py · intake.py · rank.py · grade.py · params.py
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
6. Build the LimeSurvey submission form (spec + runbook: `submission-form.md` in the WG3 initiative
   folder), then set `SUBMISSION_FORM_URL` in `scripts/params.py` — one constant, one commit, and every
   claim confirmation and deadline reminder starts carrying live per-paper upload links. Until it is
   set, those messages say so honestly rather than linking nowhere.

## Retrieving reviews (organizers)

Export the LimeSurvey responses (CSV) plus the uploaded-files archive, then:

```bash
python scripts/intake.py ingest --zip files.zip --map export.csv   # unpack into the private inbox
python scripts/intake.py reconcile                                 # read-only status report
python scripts/intake.py post --map export.csv                     # posts /received <ID> ref:<n>
```

`ingest` unpacks the archive into the **private** inbox
(`initiatives/2026-07-federated-journal-club/inbox/`, i.e. *outside* this repo) under canonical
`<PAPER>--<handle>--<issue>.pdf` names, matching on the identifiers LimeSurvey records **in the response
itself** — never on where a file sits in the archive. An unmatched file is reported, never assigned by
proximity.

`post` puts each upload into `pending` and asks its claimant to `/confirm`. `intake.py` **posts intent
and stops** — `issue_ops.py` does the mutation, so there is one writer and no double-apply. It must run
**locally as you**: a comment from inside an Action comes from `github-actions[bot]`, which
`issue-ops.yml` ignores by design. It refuses to run if the inbox resolves inside this repo — it is
public, and annotated PDFs are copyrighted derivatives *and* personal data.

A review that breaks the rules is removed with `/reject <ID>` (organizers only), which works even after
grading — `rank.py` follows the claim, so the score leaves the leaderboard with it.

Grading, the annotated-PDF store, and the open-data deposit are documented in the WG3 initiative folder
(`submission-form.md`, `copyright-and-data.md`, `grading-rubric.md`).
