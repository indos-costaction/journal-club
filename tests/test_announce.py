#!/usr/bin/env python3
"""The side-effect layer: what ``announce.py`` tells GitHub to do, and when.

    python -m unittest discover -s tests -v      # from the repo root

This is the half of issue #40 that a test can actually reach. The *ordering* fix lives
in the workflow YAML (guarded by ``test_workflows.py``); what lives here is the decision
of which ``gh`` calls to make, in what order, and which failures matter.

Nothing in this module shells out. ``plan()`` is pure and returns argv, and ``execute()``
/ ``closed_issues()`` take an injectable runner — so the whole surface is assertable
without a network, a token, or a repo.
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import announce  # noqa: E402

ISSUE = 37


def actions(**kw) -> dict:
    """An ``actions.json`` payload with issue_ops.main()'s six keys."""
    base = {"issue": ISSUE, "add_labels": [], "assignees": [],
            "changed": True, "close": False, "close_comment": ""}
    base.update(kw)
    return base


class TestFromIssueOps(unittest.TestCase):
    def test_a_nonempty_comment_is_posted_from_the_file(self):
        calls = announce.plan(announce.from_issue_ops(actions(), "you're in"))
        self.assertEqual(calls, [["gh", "issue", "comment", "37",
                                  "--body-file", "comment.md"]])

    def test_an_empty_comment_posts_nothing(self):
        # main() writes a zero-byte comment.md for a silent no-op, but it also writes a
        # bare newline in some paths. Neither is a message.
        for body in ("", "\n", "   \n"):
            with self.subTest(body=repr(body)):
                self.assertEqual(announce.plan(announce.from_issue_ops(actions(), body)), [])

    def test_labels_are_skipped_when_nothing_changed(self):
        """A refusal must not relabel or assign the thread."""
        a = actions(changed=False, add_labels=["claim", "mod:EEG"], assignees=["someone"])
        calls = announce.plan(announce.from_issue_ops(a, "no."))
        self.assertEqual(calls, [["gh", "issue", "comment", "37",
                                  "--body-file", "comment.md"]])

    def test_labels_and_assignees_are_applied_when_something_changed(self):
        a = actions(add_labels=["claim", "mod:EEG"], assignees=["carrle"])
        calls = announce.plan(announce.from_issue_ops(a, "ok"))
        self.assertIn(["gh", "issue", "edit", "37", "--add-label", "mod:EEG"], calls)
        self.assertIn(["gh", "issue", "edit", "37", "--add-assignee", "carrle"], calls)

    def test_a_null_issue_produces_no_calls(self):
        """handle_event returns issue=None for an event we do not handle."""
        self.assertEqual(announce.from_issue_ops(actions(issue=None), "hi"), [])

    def test_every_call_names_the_issue_from_actions_json(self):
        """Not github.event.issue.number — no workflow context leaks into this module."""
        a = actions(issue=1234, add_labels=["claim"], close=True, close_comment="bye")
        for call in announce.plan(announce.from_issue_ops(a, "hi")):
            self.assertIn("1234", call)


class TestClosing(unittest.TestCase):
    def test_the_reply_precedes_the_closing_note(self):
        """Otherwise the timeline reads backwards."""
        a = actions(close=True, close_comment="nothing needs you here")
        calls = announce.plan(announce.from_issue_ops(a, "withdrawn"))
        self.assertEqual([c[2] for c in calls], ["comment", "close"])

    def test_close_fires_when_the_issue_is_open(self):
        a = actions(close=True, close_comment="bye")
        calls = announce.plan(announce.from_issue_ops(a, ""))
        self.assertEqual(calls, [["gh", "issue", "close", "37", "--comment", "bye"]])

    def test_an_already_closed_issue_is_not_re_closed(self):
        """A duplicate delivery re-derives close=True; the thread is already shut.

        gh happens to return 0 and skip the --comment on a closed issue, but that is an
        implementation detail of the CLI, not a contract.
        """
        a = actions(close=True, close_comment="bye")
        calls = announce.plan(announce.from_issue_ops(a, ""), closed=frozenset({ISSUE}))
        self.assertEqual(calls, [])

    def test_closed_issues_fails_open(self):
        """An unreadable state means we attempt the close — the pre-guard behaviour."""
        self.assertEqual(announce.closed_issues({1, 2}, run=lambda argv: ""), frozenset())

    def test_closed_issues_reads_the_state_field(self):
        seen = []

        def fake(argv):
            seen.append(argv)
            return "CLOSED\n" if argv[3] == "37" else "OPEN\n"

        self.assertEqual(announce.closed_issues({37, 38}, run=fake), frozenset({37}))
        self.assertEqual(len(seen), 2)


class TestFromSweep(unittest.TestCase):
    def test_one_intent_per_notification_and_close_is_carried(self):
        notes = [{"issue": 9, "body": "day 9"},
                 {"issue": 12, "body": "expired", "close": True}]
        calls = announce.plan(announce.from_sweep(notes))
        self.assertEqual(calls, [
            ["gh", "issue", "comment", "9", "--body", "day 9"],
            ["gh", "issue", "comment", "12", "--body", "expired"],
            # sweep.py already appended the closing prose to that body, so the close
            # deliberately carries no second comment
            ["gh", "issue", "close", "12"],
        ])

    def test_no_notifications_is_no_calls(self):
        self.assertEqual(announce.plan(announce.from_sweep([])), [])


class TestExecute(unittest.TestCase):
    def failing(self, calls) -> tuple[int, str]:
        """Run a plan whose every call fails, capturing the workflow annotations.

        Capturing rather than letting them print is not tidiness: GitHub Actions turns
        any ``::error::`` on stdout into an annotation, so a green test run would
        decorate itself with two failures that never happened.
        """
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = announce.execute(calls, run=lambda argv: 1)
        return rc, buf.getvalue()

    def test_the_plan_runs_in_order(self):
        seen = []
        calls = [["gh", "issue", "comment", "1", "--body", "a"],
                 ["gh", "issue", "close", "1"]]
        rc = announce.execute(calls, run=lambda argv: seen.append(argv) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, calls)

    def test_a_failed_comment_is_fatal(self):
        """The comment is the only thing the participant sees, and after the reorder
        the state is already durable — nothing else will retry it."""
        rc, out = self.failing([["gh", "issue", "comment", "1", "--body", "a"]])
        self.assertEqual(rc, 1)
        self.assertIn("::error::", out)

    def test_a_failed_close_is_fatal(self):
        rc, out = self.failing([["gh", "issue", "close", "1"]])
        self.assertEqual(rc, 1)
        self.assertIn("::error::", out)

    def test_a_failed_label_is_not_fatal(self):
        """An organizer convenience; the next command on the thread reapplies it."""
        rc, out = self.failing([["gh", "issue", "edit", "1", "--add-label", "claim"]])
        self.assertEqual(rc, 0)
        self.assertIn("::warning::", out)
        self.assertNotIn("::error::", out)


if __name__ == "__main__":
    unittest.main()
