#!/usr/bin/env python3
"""The claim → receive → confirm → grade → reject lifecycle, end to end.

    python -m unittest discover -s tests -v      # from the repo root

Stdlib only, like everything else here. Each test builds a throwaway repo in a temp
dir and repoints ``state``'s module-level paths at it — ``state.py`` derives its paths
from ``__file__``, so without this a test run would rewrite the live ``claims/``.

What is worth testing here is not arithmetic, it is the **boundaries**:

* the per-command ACL (a security boundary — ``/confirm`` must be un-proxyable);
* the auto-close predicate (get it wrong and the handshake strands, see test_close);
* that ``pending`` stops the deadline clock;
* that a rejected review actually leaves the leaderboard.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import grade  # noqa: E402
import issue_ops  # noqa: E402
import params  # noqa: E402
import rank  # noqa: E402
import state  # noqa: E402

AUTHOR = "friedrich-ph-carrle"
ORGANIZER = "oesteban"
STRANGER = "passer-by"
PAPER = "EEG-15"
ISSUE = 9

AXES = {"engagement": 4, "comprehension": 4, "critical": 4, "action": 4, "originality": 4}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # Repoint every path state.py resolved at import time.
        for name, value in {
            "REPO": self.tmp,
            "DATA_DIR": self.tmp / "docs" / "data",
            "POOL_FILE": self.tmp / "docs" / "data" / "pool.json",
            "STATUS_FILE": self.tmp / "docs" / "data" / "status.json",
            "RANKING_FILE": self.tmp / "docs" / "data" / "ranking.json",
            "CLAIMS_DIR": self.tmp / "claims",
            "LEDGER_DIR": self.tmp / "ledger",
        }.items():
            patcher = unittest.mock.patch.object(state, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        (self.tmp / "docs" / "data").mkdir(parents=True)
        (self.tmp / "claims").mkdir()
        state.POOL_FILE.write_text(json.dumps([
            {"id": PAPER, "modality": "EEG", "level": 1, "title": "A paper",
             "first_author": "Mutanen", "year": 2022, "url": "https://doi.org/10.x"},
            {"id": "EEG-22", "modality": "EEG", "level": 1, "title": "Another",
             "first_author": "Smith", "year": 2021, "url": "https://doi.org/10.y"},
        ]))
        issue_ops.ORGANIZERS = {ORGANIZER}

    # --- helpers ---------------------------------------------------------
    def claim_paper(self, pid=PAPER, issue=ISSUE, author=AUTHOR):
        return issue_ops.handle_event({
            "event_name": "issues", "action": "opened",
            "issue": {"number": issue, "user": {"login": author},
                      "labels": [{"name": "claim"}],
                      "body": f"{pid}\n- [x] I consent\nAttributed"},
        })

    def comment(self, body, actor=AUTHOR, issue=ISSUE, author=AUTHOR):
        return issue_ops.handle_event({
            "event_name": "issue_comment", "action": "created",
            "issue": {"number": issue, "user": {"login": author}},
            "comment": {"user": {"login": actor}, "body": body},
        })

    def rec(self, pid=PAPER, issue=ISSUE):
        return state.load_claims()[issue]["papers"][pid]


class TestACL(Base):
    """The security boundary. `/confirm` must be the one command organizers can't proxy."""

    def setUp(self):
        super().setUp()
        self.claim_paper()

    def test_stranger_is_refused_and_writes_nothing(self):
        before = sorted(p.name for p in state.CLAIMS_DIR.iterdir())
        r = self.comment(f"/claim EEG-22", actor=STRANGER, issue=99, author=STRANGER)
        self.assertFalse(r["changed"])
        # A passer-by must not cause a claim file to spring into existence.
        self.assertEqual(sorted(p.name for p in state.CLAIMS_DIR.iterdir()), before)

    def test_organizer_may_receive_but_author_may_not(self):
        self.assertIn("upload received", self.comment(
            f"/received {PAPER} ref:123", actor=ORGANIZER)["comment"])
        self.assertEqual(self.rec()["state"], "pending")
        # the author trying to record their own receipt
        self.claim_paper(pid="EEG-22", issue=10)
        r = self.comment("/received EEG-22", actor=AUTHOR, issue=10)
        self.assertIn("organizers only", r["comment"])
        self.assertEqual(self.rec("EEG-22", 10)["state"], "active")

    def test_organizer_cannot_proxy_confirm(self):
        """The load-bearing one: if this passes, the handshake is theatre."""
        self.comment(f"/received {PAPER} ref:123", actor=ORGANIZER)
        r = self.comment(f"/confirm {PAPER}", actor=ORGANIZER)
        self.assertIn("only @" + AUTHOR, r["comment"])
        self.assertEqual(self.rec()["state"], "pending")   # NOT submitted

        # ...and the real author can.
        r = self.comment(f"/confirm {PAPER}", actor=AUTHOR)
        self.assertEqual(self.rec()["state"], "submitted")

    def test_author_cannot_reject(self):
        r = self.comment(f"/reject {PAPER}", actor=AUTHOR)
        self.assertIn("organizers only", r["comment"])
        self.assertNotEqual(self.rec()["state"], "rejected")

    def test_organizer_on_own_thread_wears_both_hats(self):
        self.claim_paper(issue=11, author=ORGANIZER)
        self.comment(f"/received {PAPER} ref:9", actor=ORGANIZER, issue=11, author=ORGANIZER)
        self.comment(f"/confirm {PAPER}", actor=ORGANIZER, issue=11, author=ORGANIZER)
        self.assertEqual(self.rec(PAPER, 11)["state"], "submitted")

    def test_submit_alias_still_works(self):
        """A stale intake.py must not fail silently."""
        self.comment(f"/submit {PAPER} ref:77", actor=ORGANIZER)
        self.assertEqual(self.rec()["state"], "pending")
        self.assertEqual(self.rec()["submission_ref"], "77")


class TestClose(Base):
    """Finding (1): the auto-close must not strand the handshake."""

    def test_receive_keeps_thread_open_confirm_closes_it(self):
        self.claim_paper()
        r = self.comment(f"/received {PAPER} ref:1", actor=ORGANIZER)
        self.assertFalse(r["close"], "thread closed while awaiting /confirm — handshake stranded")

        r = self.comment(f"/confirm {PAPER}", actor=AUTHOR)
        self.assertTrue(r["close"])
        self.assertIn("grading", r["close_comment"])

    def test_close_after_withdraw_promises_no_points(self):
        self.claim_paper()
        r = self.comment(f"/withdraw {PAPER}")
        self.assertTrue(r["close"])
        self.assertNotIn("points", r["close_comment"])


class TestClock(Base):
    def test_pending_never_expires(self):
        import sweep
        self.claim_paper()
        self.comment(f"/received {PAPER} ref:1", actor=ORGANIZER)
        far_future = state.now_utc() + timedelta(days=365)
        with unittest.mock.patch.object(sweep.state, "REPO", self.tmp):
            sweep.run(now=far_future)
        self.assertEqual(self.rec()["state"], "pending", "the clock did not stop at pending")

    def test_receive_accepted_after_expiry(self):
        self.claim_paper()
        c = state.load_claims()[ISSUE]
        c["papers"][PAPER]["state"] = "expired"
        state.save_claim(c)
        r = self.comment(f"/received {PAPER} ref:5", actor=ORGANIZER)
        self.assertIn("after the deadline", r["comment"])
        self.assertEqual(self.rec()["state"], "pending")

    def test_extend_on_pending_is_refused_without_burning_it(self):
        self.claim_paper()
        self.comment(f"/received {PAPER} ref:1", actor=ORGANIZER)
        r = self.comment(f"/extend {PAPER}")
        self.assertIn("clock stopped", r["comment"])
        self.assertFalse(self.rec()["extended"])


class TestIdempotence(Base):
    """The rebase-retry loop re-runs the script, so every op must be re-appliable."""

    def test_confirm_twice_is_stable(self):
        self.claim_paper()
        self.comment(f"/received {PAPER} ref:1", actor=ORGANIZER)
        self.comment(f"/confirm {PAPER}")
        first = (state.CLAIMS_DIR / f"{ISSUE}.json").read_text()
        self.comment(f"/confirm {PAPER}")
        self.assertEqual((state.CLAIMS_DIR / f"{ISSUE}.json").read_text(), first)

    def test_reupload_updates_ref_only(self):
        self.claim_paper()
        self.comment(f"/received {PAPER} ref:1", actor=ORGANIZER)
        self.comment(f"/received {PAPER} ref:2", actor=ORGANIZER)
        self.assertEqual(self.rec()["state"], "pending")
        self.assertEqual(self.rec()["submission_ref"], "2")

    def test_ref_is_not_scraped_as_a_paper_id(self):
        self.claim_paper()
        self.comment(f"/received {PAPER} ref:abc-123", actor=ORGANIZER)
        self.assertEqual(self.rec()["submission_ref"], "abc-123")
        self.assertNotIn("ABC-123", state.load_claims()[ISSUE]["papers"])


class TestRejectAndLedger(Base):
    """Finding (2): rejecting a *graded* review must remove its points."""

    def _grade_it(self):
        self.claim_paper()
        self.comment(f"/received {PAPER} ref:1", actor=ORGANIZER)
        self.comment(f"/confirm {PAPER}")
        grade.grade(ISSUE, PAPER, AXES, ORGANIZER)

    def test_points_leave_the_board_but_the_ledger_survives(self):
        self._grade_it()
        board = json.loads(state.RANKING_FILE.read_text())
        self.assertEqual(len(board["participants"]), 1)
        self.assertGreater(board["participants"][0]["points"], 0)

        r = self.comment(f"/reject {PAPER}", actor=ORGANIZER)
        self.assertIn("withdrawn by the organizers", r["comment"])

        claims = state.load_claims()
        rank.write_ranking(claims, state.load_ledger(),
                           state.write_status(state.load_pool(), claims))
        board = json.loads(state.RANKING_FILE.read_text())
        self.assertEqual(board["participants"], [], "rejected review still on the leaderboard")

        # The audit trail must remain: what was scored, by whom, when.
        entry = json.loads((state.LEDGER_DIR / f"{state.claim_id(PAPER, AUTHOR, ISSUE)}.json"
                            ).read_text())
        self.assertTrue(entry["floor_ok"])
        self.assertEqual(entry["grader"], ORGANIZER)
        self.assertEqual(self.rec()["rejected_by"], ORGANIZER)
        self.assertIn("rejected_at", self.rec())

    def test_reject_frees_the_slot_and_reopens_the_paper(self):
        self._grade_it()
        self.comment(f"/reject {PAPER}", actor=ORGANIZER)
        claims = state.load_claims()
        self.assertEqual(state.active_cap_count(claims, AUTHOR), 0)
        self.assertEqual(state.completed_count(claims, PAPER), 0)

    def test_bot_never_alleges_misconduct(self):
        """A public comment naming a real person states mechanics, not accusations."""
        import messages
        body = messages.reject_notice(AUTHOR, [PAPER]).lower()
        for word in ("ai", "cheat", "violat", "misconduct", "fraud", "plagiar"):
            self.assertNotIn(word, body.replace("claim", ""))


class TestGradeGuard(Base):
    def test_cannot_grade_an_unconfirmed_upload(self):
        self.claim_paper()
        self.comment(f"/received {PAPER} ref:1", actor=ORGANIZER)
        with self.assertRaises(SystemExit):
            grade.grade(ISSUE, PAPER, AXES, ORGANIZER)


class TestIngestMatcher(unittest.TestCase):
    """`ingest` must never hand one participant's file to another."""

    def _u(self, **kw):
        import intake
        base = dict(issue=9, paper=PAPER, gh=AUTHOR, ref="101",
                    stored="fu_aaa111", orig="review.pdf")
        return intake.Upload(**{**base, **kw})

    def test_stored_name_wins_over_a_shared_filename(self):
        """Two people both uploading `review.pdf` is entirely likely. It must not collide."""
        import intake
        members = ["428628/101/fu_aaa111", "428628/102/fu_bbb222"]
        a = self._u(stored="fu_aaa111")
        b = self._u(stored="fu_bbb222", ref="102", gh="someone-else")
        self.assertEqual(intake.find_member(members, a)[0], "428628/101/fu_aaa111")
        self.assertEqual(intake.find_member(members, b)[0], "428628/102/fu_bbb222")

    def test_layout_is_not_a_contract(self):
        """Match on identifiers, not on where the file happens to sit."""
        import intake
        for members in (["fu_aaa111"], ["a/b/c/d/fu_aaa111"], ["survey/resp/fu_aaa111.pdf"]):
            self.assertIsNotNone(intake.find_member(members, self._u())[0], members)

    def test_ambiguity_is_reported_not_guessed(self):
        import intake
        u = self._u(stored="", ref="")   # only the original filename to go on
        member, why = intake.find_member(["x/review.pdf", "y/review.pdf"], u)
        self.assertIsNone(member)
        self.assertIn("ambiguous", why)

    def test_missing_file_is_reported(self):
        import intake
        member, why = intake.find_member(["428628/999/fu_zzz"], self._u())
        self.assertIsNone(member)
        self.assertIn("no archive file matches", why)

    def test_file_meta_survives_junk(self):
        import intake
        self.assertEqual(intake._file_meta(""), ("", ""))
        self.assertEqual(intake._file_meta("not json"), ("", ""))
        self.assertEqual(intake._file_meta("[]"), ("", ""))
        self.assertEqual(
            intake._file_meta('[{"name":"my review.pdf","filename":"fu_x1","ext":"pdf"}]'),
            ("fu_x1", "my review.pdf"))


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
