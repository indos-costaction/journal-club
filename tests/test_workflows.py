#!/usr/bin/env python3
"""Structural lint over the workflow YAML — the only guard issue #40 can have.

    python -m unittest discover -s tests -v      # from the repo root

The #40 bug was pure step *ordering*: issue-ops posted its reply, labelled and closed
the thread, and only then tried to push. When the push lost a race the rebase-retry loop
re-applied the intent and regenerated a correct ``comment.md``, which nobody ever
posted. No unit test can reach that, because the ordering lives in YAML that never
executes locally.

So these are text assertions. There is no PyYAML here (stdlib only, by policy), and
parsing is not the point anyway: the properties worth pinning are "the push comes first"
and "no workflow talks to GitHub directly", both of which are visible in the raw file.

``test_no_gh_side_effects_are_inlined_in_a_workflow`` is the strongest of them. It makes
the ordering property *structural* rather than positional: with every participant-facing
call funnelled through ``announce.py``, the bug cannot be reintroduced by dropping a new
step in the wrong place — there is only one step that can speak, and it is after the push.
"""
from __future__ import annotations

import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text()


def code(name: str) -> str:
    """The workflow with whole-line comments dropped.

    These files carry long comments explaining exactly the hazards being linted for
    ("do not fix that with ``if: always()``", "deliberately NOT jc-state-push"), so a
    naive substring search matches the warning rather than the mistake.
    """
    return "\n".join(ln for ln in read(name).splitlines()
                     if not ln.lstrip().startswith("#"))


def step(name: str, marker: str) -> str:
    """The block of one step, from the `- name:` line carrying ``marker`` to the next."""
    lines = code(name).splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.lstrip().startswith("- name:") and marker in ln)
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].lstrip().startswith("- name:")), len(lines))
    return "\n".join(lines[start:end])


# Every workflow that mutates state, pushes, and then speaks. A table rather than a test
# per file: the point of these assertions is that they apply to the *class* of workflow,
# and a fourth one added without a row here would inherit none of them.
#
#   file, announce mode, python entrypoint, shares the push group
STATE_WORKFLOWS = (
    ("issue-ops.yml",   "issue-ops",   "scripts/issue_ops.py",   True),
    ("suggest-ops.yml", "suggest-ops", "scripts/suggest_ops.py", True),
    ("daily-sweep.yml", "sweep",       "scripts/sweep.py",       False),
)


class TestPushBeforeAnnounce(unittest.TestCase):
    def test_every_state_workflow_pushes_before_it_announces(self):
        for f, mode, _entry, _shared in STATE_WORKFLOWS:
            with self.subTest(workflow=f):
                t = code(f)
                self.assertLess(t.index("git push origin"), t.index(f"announce.py {mode}"),
                                f"{f} announces before it pushes — that is issue #40")

    def test_no_gh_side_effects_are_inlined_in_a_workflow(self):
        """Every participant-facing call goes through announce.py, where it is
        orderable and testable. An inline `gh issue comment` can be moved above the
        push by a well-meaning edit and nothing would catch it."""
        for f in sorted(WORKFLOWS.glob("*.yml")):
            t = code(f.name)
            for forbidden in ("gh issue comment", "gh issue close", "--add-label",
                              "--add-assignee"):
                self.assertNotIn(forbidden, t, f"{f.name} performs {forbidden!r} inline")

    def test_the_failure_notice_is_gated_on_the_push_step(self):
        """`if: failure()` alone would apologise for an apply-step crash on somebody's
        unrelated bug report."""
        t = code("issue-ops.yml")
        self.assertIn("id: push", t)
        self.assertIn("steps.push.outcome == 'failure'", t)

    def test_the_announce_step_is_not_forced_to_run(self):
        """The default success() guard is what skips the announcement when the push
        failed. Any `if:` there — `always()` most of all — restores the bug in a new
        costume, so the step must carry no condition at all."""
        block = step("issue-ops.yml", "Announce")
        conditions = [ln.strip() for ln in block.splitlines() if ln.strip().startswith("if:")]
        self.assertEqual(conditions, [], "the announce step must have no `if:`")


class TestConcurrency(unittest.TestCase):
    def test_the_push_group_queues_rather_than_cancelling(self):
        """`queue: single` (the default) cancels a *pending* run when a third arrives —
        a participant's command vanishing with no comment and no error."""
        for f in ("issue-ops.yml", "suggest-ops.yml", "grade.yml"):
            with self.subTest(workflow=f):
                self.assertIn("queue: max", code(f))

    def test_the_sweep_does_not_share_the_push_group(self):
        """Sharing it would give a 06:00 UTC cron a way to cancel a queued command."""
        self.assertNotIn("group: jc-state-push", code("daily-sweep.yml"))

    def test_the_state_workflows_do_share_the_push_group(self):
        for f in ("issue-ops.yml", "suggest-ops.yml", "grade.yml"):
            with self.subTest(workflow=f):
                self.assertIn("group: jc-state-push", code(f))


class TestTheTwoEnginesCannotSeeEachOther(unittest.TestCase):
    """Claims and pool suggestions are routed apart in YAML, not by what people type.

    Until the suggestion form existed the only label check was inside issue_ops.py, so
    every issue in the repo started the claim engine and was saved solely by its body
    carrying no slash-command token. issue_ops scans the whole rendered body — any
    `- [x]` reads as GDPR consent, the word "anonymous" reads as an attribution choice —
    which is how #26 answered a bug report with a consent rejection.
    """

    def test_the_claim_engine_skips_suggestion_threads(self):
        self.assertIn("!contains(github.event.issue.labels.*.name, 'paper-suggestion')",
                      code("issue-ops.yml"))

    def test_the_suggestion_engine_takes_only_suggestion_threads(self):
        self.assertIn("contains(github.event.issue.labels.*.name, 'paper-suggestion')",
                      code("suggest-ops.yml"))

    def test_neither_reacts_to_the_bot(self):
        """Without this the triage comment re-triggers the workflow that wrote it."""
        for f in ("issue-ops.yml", "suggest-ops.yml"):
            with self.subTest(workflow=f):
                self.assertIn("github.actor != 'github-actions[bot]'", code(f))

    def test_the_suggestion_form_cannot_be_read_as_a_claim(self):
        """Belt and braces for the YAML guard above, which is one edit from gone.

        The checkbox rule targets the FIELD TYPE, not a rendered `- [x]`: the form never
        contains one, GitHub renders it from `type: checkboxes` when the issue is filed,
        and it is that rendered body issue_ops scans.
        """
        form = (WORKFLOWS.parent / "ISSUE_TEMPLATE" / "paper-suggestion.yml").read_text()
        body = "\n".join(ln for ln in form.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("type: checkboxes", body,
                         "renders as `- [x]`, which _detect_consent reads as GDPR consent")
        self.assertNotRegex(body, r"-\s*\[x\]", "a ticked box reads as GDPR consent")
        self.assertNotIn("anonymous", body.lower(), "reads as an attribution choice")
        self.assertNotRegex(
            body, r"/(claim|withdraw|submit|received|confirm|reject|extend|decline)\s",
            "a claim command in the form body would be parsed as one")


class TestRetryLoops(unittest.TestCase):
    def test_every_state_workflow_re_applies_intent_on_a_lost_push(self):
        """Re-applying beats replaying a diff, and it is what regenerates comment.md."""
        for f, _mode, entrypoint, _shared in STATE_WORKFLOWS:
            with self.subTest(workflow=f):
                t = read(f)
                self.assertIn("push rejected, rebasing", t)
                # the entrypoint appears twice: the first pass and the re-apply
                self.assertGreaterEqual(t.count(entrypoint), 2, f"{f} never re-applies")

    def test_the_suite_runs_on_a_workflow_only_change(self):
        """Without this the lint above can never fire on the change it guards."""
        self.assertEqual(read("tests.yml").count("'.github/workflows/**'"), 2)


if __name__ == "__main__":
    unittest.main()
