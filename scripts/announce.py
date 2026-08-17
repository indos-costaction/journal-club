#!/usr/bin/env python3
"""Perform the participant-facing side effects of one state run.

    python scripts/announce.py issue-ops   # reads actions.json + comment.md
    python scripts/announce.py sweep       # reads notifications.json
    python scripts/announce.py failed      # "nothing was recorded" (push exhausted)

**This runs AFTER the state is on origin, never before** — issue #40.

``issue-ops.yml``'s rebase-retry loop re-applies the intent on top of the freshly
fetched tip, which *rewrites* ``comment.md`` and ``actions.json``. A comment posted
before the push therefore announces a decision that may never have been persisted. On
2026-07-31 issue #37 was told "``EEG-03`` claimed — due 2026-08-12" by the attempt that
lost the race; the attempt that landed had rejected the claim, and ``claims/37.json``
reads ``"papers": {}`` to this day.

Two invariants make post-after-push correct, and both are easy to break by accident:

* ``issue_ops.main()`` rewrites **both** artifacts on every invocation, including each
  of its early returns — so the loop's re-run always replaces them;
* both are git-ignored, so ``git reset --hard`` leaves them in place for the re-run to
  overwrite rather than deleting them.

Everything above ``main()`` is pure: ``plan()`` turns the on-disk artifacts plus the
current issue states into an ordered list of ``gh`` argv, and ``tests/test_announce.py``
asserts that list directly. No prose is authored here — it comes from ``comment.md``,
``notifications.json`` or ``messages.py``, the same discipline as everywhere else.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import messages
import state

COMMENT_FILE = state.REPO / "comment.md"
ACTIONS_FILE = state.REPO / "actions.json"
NOTIFICATIONS_FILE = state.REPO / "notifications.json"


@dataclass(frozen=True)
class Intent:
    """One thread's side effects — the normalised shape both feeds produce.

    ``body`` is an inline comment body; ``body_file`` posts from a file instead (the
    issue-ops reply is long markdown and goes through ``--body-file``). At most one of
    the two is ever set.
    """
    issue: int
    body: str = ""
    body_file: str | None = None
    add_labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    close: bool = False
    close_comment: str = ""


def from_issue_ops(actions: dict, comment: str,
                   body_file: str = "comment.md") -> list[Intent]:
    """``actions.json`` + ``comment.md`` → at most one Intent.

    Keyed off ``actions["issue"]``, **not** ``github.event.issue.number``: the workflow
    context is deliberately not an input to this module, which is what makes it
    testable. They are always equal for the triggers issue-ops listens on, and a null
    issue means an event we do not handle.

    Labels and assignees describe an *applied* change, so a pure refusal must not
    relabel the thread — that reproduces the ``changed`` guard the workflow used to
    carry inline.
    """
    issue = actions.get("issue")
    if issue is None:
        return []
    changed = bool(actions.get("changed"))
    return [Intent(
        issue=int(issue),
        body_file=body_file if comment.strip() else None,
        add_labels=tuple(actions.get("add_labels") or ()) if changed else (),
        assignees=tuple(actions.get("assignees") or ()) if changed else (),
        close=bool(actions.get("close")),
        close_comment=actions.get("close_comment") or "",
    )]


def from_sweep(notifications: list[dict]) -> list[Intent]:
    """``notifications.json`` → one Intent per notification.

    ``sweep.py`` already appends the closing prose to the last notification of a
    closing thread, so ``close`` carries no separate comment here.
    """
    return [Intent(issue=int(n["issue"]), body=n["body"], close=bool(n.get("close")))
            for n in notifications]


def plan(intents: list[Intent], closed: frozenset[int] = frozenset()) -> list[list[str]]:
    """Pure: intents + already-closed issues → ordered ``gh`` argv.

    Order matters — the reply has to land before the closing note or the timeline reads
    backwards. ``closed`` suppresses a re-close on a duplicate delivery: ``gh`` does
    return 0 on an already-closed issue and skips the ``--comment``, but that is an
    implementation detail of the CLI, not a contract we should lean on.
    """
    calls: list[list[str]] = []
    for it in intents:
        n = str(it.issue)
        if it.body_file:
            calls.append(["gh", "issue", "comment", n, "--body-file", it.body_file])
        elif it.body.strip():
            calls.append(["gh", "issue", "comment", n, "--body", it.body])
        for label in it.add_labels:
            calls.append(["gh", "issue", "edit", n, "--add-label", label])
        for who in it.assignees:
            calls.append(["gh", "issue", "edit", n, "--add-assignee", who])
        if it.close and it.issue not in closed:
            call = ["gh", "issue", "close", n]
            if it.close_comment:
                call += ["--comment", it.close_comment]
            calls.append(call)
    return calls


def _run(argv: list[str]) -> int:
    print("+ " + " ".join(argv), flush=True)
    return subprocess.run(argv, cwd=state.REPO, check=False).returncode


def _capture(argv: list[str]) -> str:
    r = subprocess.run(argv, cwd=state.REPO, capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else ""


def closed_issues(issues: set[int], run=_capture) -> frozenset[int]:
    """Which of ``issues`` GitHub currently reports as CLOSED.

    Queried only for the issues we actually intend to close — normally zero or one, so
    no extra API call in the common case. **Fails open**: an unreadable state means we
    attempt the close, which is what happened before this guard existed.
    """
    return frozenset(n for n in sorted(issues)
                     if run(["gh", "issue", "view", str(n),
                             "--json", "state", "-q", ".state"]).strip().upper() == "CLOSED")


def execute(calls: list[list[str]], run=_run) -> int:
    """Run the plan. A failed comment or close is fatal; a failed label is not.

    The comment is the only thing the participant ever sees, and after the reorder the
    state is already durable — nothing else will retry it. A dropped sweep nudge is
    suppressed forever by its ``reminded`` marker, so silence here is not acceptable.
    Labels are an organizer convenience and the next command on the thread reapplies
    them, so they only warn.
    """
    fatal = False
    for argv in calls:
        rc = run(argv)
        if rc == 0:
            continue
        if argv[2] in ("comment", "close"):
            print(f"::error::gh {' '.join(argv[1:4])} failed (exit {rc})")
            fatal = True
        else:
            print(f"::warning::gh {' '.join(argv[1:])} failed (exit {rc}) — ignored")
    return 1 if fatal else 0


def _read(path: Path, default: str = "") -> str:
    return path.read_text() if path.exists() else default


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=("issue-ops", "sweep", "failed"))
    args = ap.parse_args(argv)

    actions = json.loads(_read(ACTIONS_FILE, "{}") or "{}")

    if args.mode == "failed":
        # Reached only when the push step exhausted its retries. `changed` was true by
        # construction to get that far, so actions.json exists and names a real claim
        # thread; reading the issue from there rather than from the event keeps this
        # off unrelated issues.
        issue = actions.get("issue")
        if issue is None:
            print("::warning::no issue in actions.json — nothing to apologise for")
            return 0
        return execute([["gh", "issue", "comment", str(issue),
                         "--body", messages.not_recorded()]])

    if args.mode == "issue-ops":
        intents = from_issue_ops(actions, _read(COMMENT_FILE))
    else:
        intents = from_sweep(json.loads(_read(NOTIFICATIONS_FILE, "[]") or "[]"))

    return execute(plan(intents, closed_issues({i.issue for i in intents if i.close})))


if __name__ == "__main__":
    sys.exit(main())
