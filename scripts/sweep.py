#!/usr/bin/env python3
"""Daily sweep: expire overdue claims, send reminders, recompute aggregates.

Mirrors the website repo's scheduled-Action pattern (checkout → run → commit-back).
Idempotent: it reads absolute timestamps, not "what changed since yesterday", so a
skipped day self-heals on the next run. Per-paper ``reminded`` markers guarantee
each nudge fires once.

Writes ``notifications.json`` (git-ignored) — a list of {issue, body, close?} the
workflow posts as issue comments (GitHub then emails the @-mentioned assignee); an
entry carrying ``close: true`` also closes its thread. No SMTP.
"""
from __future__ import annotations

import json
from datetime import datetime

import messages
import params
import rank
import state

THRESHOLDS = sorted(params.REMIND_BEFORE_DAYS, reverse=True)  # e.g. [3, 1]


def _remaining_days(due: datetime, now: datetime) -> float:
    return (due - now).total_seconds() / 86400.0


def run(now: datetime | None = None) -> list[dict]:
    now = now or state.now_utc()
    pool = state.load_pool()
    claims = state.load_claims()
    notifications: list[dict] = []
    touched: set[int] = set()
    to_close: set[int] = set()

    for issue, claim in claims.items():
        who = claim["participant"]
        expired_any = False
        for pid, rec in claim["papers"].items():
            if rec["state"] == "pending":
                # Uploaded, not yet signed off. The deadline clock stopped here (that is
                # the point of `pending`), so this never expires — we only nudge. Someone
                # who did the work must not lose it for missing a notification; a
                # genuinely abandoned upload is an organizer's /reject, not a timer's.
                since = rec.get("pending_since")
                if not since:
                    continue    # pre-handshake record; nothing to measure from
                waited = (now - state.parse(since)).total_seconds() / 86400.0
                to_fire = [d for d in sorted(params.CONFIRM_NUDGE_DAYS)
                           if waited >= d and f"conf{d}" not in rec["reminded"]]
                if to_fire:
                    rec["reminded"].extend(f"conf{d}" for d in to_fire)
                    touched.add(issue)
                    notifications.append({"issue": issue, "body": messages.confirm_nudge(
                        who, pid, pool, max(1, int(waited)))})
                continue
            if rec["state"] != "active":
                continue
            due = state.parse(rec["due_at"])
            remaining = _remaining_days(due, now)

            if remaining <= 0:  # expire = auto-withdraw, no penalty, zero points
                rec["state"] = "expired"
                expired_any = True
                touched.add(issue)
                notifications.append(
                    {"issue": issue, "body": messages.expiry(who, pid, rec, pool)})
                continue

            to_fire = [d for d in THRESHOLDS
                       if remaining <= d and f"pre{d}" not in rec["reminded"]]
            if to_fire:
                n = max(1, int(remaining + 0.999))  # ceil, ≥1
                rec["reminded"].extend(f"pre{d}" for d in to_fire)
                touched.add(issue)
                notifications.append({"issue": issue, "body":
                    messages.reminder(who, pid, rec, pool, issue, n)})

        # An expiry can empty a thread: nothing left here needs the participant (#24).
        # Same predicate issue_ops applies after a command — `submitted` papers are with
        # the organizers, and a `pending` one is still waiting on their /confirm.
        if expired_any and not any(r["state"] in state.NEEDS_PARTICIPANT
                                   for r in claim["papers"].values()):
            to_close.add(issue)

    # Flag the last notification of each closing thread: the workflow posts it and
    # then closes, so the expiry message stays the reason shown on the timeline.
    for note in reversed(notifications):
        if note["issue"] in to_close:
            note["close"] = True
            note["body"] += "\n\n" + messages.thread_done_after_expiry()
            to_close.discard(note["issue"])

    for issue in touched:
        state.save_claim(claims[issue])

    # guaranteed daily refresh of the public aggregates
    claims = state.load_claims()
    status = state.write_status(state.load_pool(), claims)
    rank.write_ranking(claims, state.load_ledger(), status)

    (state.REPO / "notifications.json").write_text(
        json.dumps(notifications, indent=2, ensure_ascii=False) + "\n")
    return notifications


if __name__ == "__main__":
    notes = run()
    print(f"sweep: {len(notes)} notification(s) queued")
