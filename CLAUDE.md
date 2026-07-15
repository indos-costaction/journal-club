# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The tracking + leaderboard backend for WG3's **Federated Journal Club** (COST Action
CA24161 "INDoS"). It runs entirely on **GitHub Issues + Actions + Pages** — no server, no
database, no paid services. This repo is a submodule of `grant-2025-CA-INDoS`
(`indos-costaction/journal-club` on GitHub); the surrounding WG3 initiative folder holds the
non-code docs (`mechanics.md`, `grading-rubric.md`, `copyright-and-data.md`).

## The load-bearing idea: Git is the database

The truth is a set of small per-entity JSON files. Everything the public site reads is a
**pure, idempotent recompute** from those files, so **every Action is safe to re-run**.

- **Authoritative state** (hand/bot-edited, the source of truth):
  - `docs/data/pool.json` — static paper identity, seeded once, IDs frozen on first publish.
  - `claims/<issue>.json` — one file per claim issue; the dynamic truth (who holds what, deadlines, states).
  - `ledger/<claim-id>.json` — one file per graded review; the only thing that crosses from the private grading workspace into the public repo (5 axis scores, never the PDF).
- **Derived, never hand-edited** (recomputed by `state.py` / `rank.py`):
  - `docs/data/status.json` — per-paper live claims / completed reviews / status / outstanding need.
  - `docs/data/ranking.json` — the leaderboard.
- **Build-time only, never committed** (`.gitignore`d): `docs/data/site.json` (repo slug),
  and the per-run workflow artifacts `comment.md`, `actions.json`, `notifications.json`.

The three web-served JSONs live under `docs/data/` specifically so the GitHub Pages `/docs`
deploy serves them same-origin. `claims/` and `ledger/` stay at repo root (the site never reads them).

## Architecture

`scripts/state.py` is the **pure engine** and the single place that enforces mechanics and derives
`status.json`. It has **no GitHub knowledge** — the three workflow entrypoints call into it:

- `issue_ops.py` — parses one GitHub `issues` (claim form) or `issue_comment` (`/claim` `/withdraw`
  `/extend` `/confirm`, plus organizer-only `/received` `/reject`) event, validates + applies against
  freshly-loaded state, writes the updated claim file + `status.json`, plus `comment.md` /
  `actions.json` for the workflow to act on. Unit-testable with a plain dict (`handle_event`).
  Enforces the identity barrier in two tiers: a coarse gate (thread owner or organizer) and then
  `COMMAND_ACL` per command.
- `intake.py` — organizer-side, run **locally** (a comment from a GHA is `github-actions[bot]`, which
  `issue-ops.yml` ignores by design). `ingest` unpacks a LimeSurvey file archive into the private
  inbox under canonical `claim_id` filenames; `reconcile` reports; `post` comments `/received`.
  It **posts intent and never writes `claims/`** — one writer, or issue-ops would double-apply.
- `sweep.py` — daily: expire overdue claims (auto-withdraw, no penalty), fire day-9/day-11 reminders,
  refresh `status.json` + `ranking.json`. Reads **absolute timestamps**, so a skipped day self-heals.
  Per-paper `reminded` markers guarantee each nudge fires once.
- `grade.py` — organizer-only: enter the 5 rubric axis scores for one review into the ledger, flip the
  claim `submitted → completed` (floor passed) or `submitted → returned` (below floor), refresh aggregates.
- `rank.py` — recompute `ranking.json` as a pure function of ledger + claims (`write_ranking`).
- `seed_pool.py` — one-time (or re-seed): build `pool.json` from the curated `references/lit-db/` in the
  surrounding my-grants monorepo. IDs are deterministic — sort key is `(citationCount desc, paperId)` —
  so re-running reproduces the same IDs; a backfill only appends, never re-sorts.

`scripts/params.py` is the **single source of truth for every tunable rule** (caps, thresholds,
deadlines, rubric weights, modality→ID-prefix map). These mirror the WG3-ratifiable defaults in
`mechanics.md` / `grading-rubric.md`. **Change a value here and every script follows** — do not
hardcode a mechanics number anywhere else.

`docs/` is the static GitHub Pages site (`index.html` + `app.js` + `style.css`) that fetches the
three JSONs. `.github/ISSUE_TEMPLATE/claim.yml` is the claim form (paper IDs + attribution + GDPR consent).

## Claim state semantics (in `state.py`)

    active --/received (organizer)--> pending --/confirm (author)--> submitted --> completed

- `IN_FLIGHT = {active, pending, submitted}` — occupies a slot, counts against the 3-claim cap and toward a paper's live-claim count.
- `DONE = {completed}` — a floor-passing review; counts toward the completion threshold.
- `FREED = {withdrawn, recalled, expired, returned, rejected}` — slot released, contributes nothing. (`recalled` is legacy pre-rename data; `rejected` is an organizer removing a review for a rules violation.)
- `NEEDS_PARTICIPANT = {active, pending}` — the auto-close predicate in `issue_ops.py` and `sweep.py`. A thread closes when nothing on it needs the participant; `pending` is waiting on their `/confirm`, so closing it would strand the handshake.

**Why `pending` exists.** The LimeSurvey upload form is open-access and its per-paper link is
published in a public issue, so an upload proves only that *someone* had the link — it cannot assert
authorship. GitHub identity is the anchor: the claimant's `/confirm` is what makes a file a review,
and it is the only place the no-AI declaration acquires an authenticated signatory. Two consequences
that are load-bearing rather than incidental:

- **`pending` cannot expire** — `sweep.py` only expires `active`, so receiving an upload stops the
  deadline clock. Someone who uploads on day 11 must not be expired on day 12 waiting on us.
- **`/confirm` is the one command an organizer cannot proxy** (`issue_ops.COMMAND_ACL`). Everything
  else on any thread is organizer-drivable; that exception is the whole point of the handshake.

Paper status is derived: `done` at `COMPLETION_THRESHOLD` completed reviews, else `closed` at
`POOL_CLOSE_THRESHOLD` live claimants, else `open`.

`rank.py` counts a ledger entry only while its claim is still `completed` — the board follows the
claim, not the ledger alone. Without that, `/reject`ing an already-graded review would remove it
everywhere except the leaderboard.

## Working on this codebase

- **Python 3.10, standard library only** — no third-party deps, no `requirements.txt`, no build step.
- **Tests:** `python -m unittest discover -s tests` from the repo root. `tests/test_flow.py` covers the
  lifecycle boundaries — the per-command ACL, the auto-close predicate, that `pending` stops the clock,
  and that a rejected review leaves the leaderboard. Each test builds a throwaway repo in a temp dir and
  repoints `state`'s module-level paths at it; **do not skip that** — `state.py` resolves its paths from
  `__file__`, so a test that forgets rewrites the live `claims/`.
- Time-dependent functions all take an explicit `now` for determinism. Keep it that way.
- Run any entrypoint locally against the checked-in state: `python scripts/sweep.py`,
  `python scripts/rank.py`, `python scripts/grade.py --issue N --paper ID --engagement .. --grader you`.
  Scripts resolve paths relative to `scripts/`, so run from anywhere in the tree.
- **Determinism is a hard requirement**, not a nicety — it is what makes the apply-intent / re-run model
  correct. Keep functions pure given their inputs + `now`; never introduce wall-clock reads or ordering
  that isn't a stable sort.

## Concurrency & the workflows (`.github/workflows/`)

- `issue-ops.yml`, `daily-sweep.yml`, `grade.yml` all follow **mutate-and-push**: run Python → commit the
  updated state → push back. `issue-ops` has a **rebase-retry loop**: on push rejection it `reset --hard`
  to the fetched tip and **re-applies the intent** (re-runs `issue_ops.py`) rather than replaying a diff —
  this is why every operation must be idempotent and re-derivable.
- The `jc-state-push` concurrency group serialises pushes across `issue-ops` and `grade`; runs are queued,
  **not** cancelled (each carries a distinct user action).
- `issue-ops` ignores `github-actions[bot]` events to avoid reacting to its own comments/auto-close.
- `pages.yml` regenerates `site.json` at build time and redeploys. Because `GITHUB_TOKEN` commits can't
  trigger a `push` event (GitHub loop-prevention), it **also** listens on `workflow_run: completed` of the
  three state workflows — that's how a bot state-commit reaches the site.

## Guardrails

- **Never hand-edit `status.json` or `ranking.json`** — they are derived; edit the authoritative source
  (`pool.json` / `claims/` / `ledger/`) and let the scripts recompute.
- **Never commit** `comment.md`, `actions.json`, `notifications.json`, or `docs/data/site.json`.
- Pool IDs are **frozen once published**. Do not re-run `seed_pool.py` in a way that re-sorts existing IDs;
  a growth pass appends only.
- Grading crosses only the numeric scores into this repo — never the annotated PDF, never participant email.
