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

import contextlib
import io
import json
import os
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
        r = self.comment(f"/received {PAPER} ref:2", actor=ORGANIZER)
        self.assertEqual(self.rec()["state"], "pending")
        self.assertEqual(self.rec()["submission_ref"], "2")
        # a genuine second file: the claimant is told, and the nudge clock restarts
        self.assertIn("newer upload received", r["comment"])

    def test_the_same_ref_received_twice_is_byte_identical(self):
        """A re-run must not claim a newer upload arrived when the same one did.

        `newtest=Y` on the upload link mints a fresh LimeSurvey response id per
        submission, so an equal ref proves the same file. Without this guard a workflow
        re-run, a redelivered webhook or a double paste would restamp `pending_since`,
        wipe the confirm nudges, and tell the claimant their file had been replaced —
        and after the #40 reorder that is the message that gets posted.
        """
        self.claim_paper()
        self.comment(f"/received {PAPER} ref:7", actor=ORGANIZER)
        first = (state.CLAIMS_DIR / f"{ISSUE}.json").read_text()
        r = self.comment(f"/received {PAPER} ref:7", actor=ORGANIZER)
        self.assertEqual((state.CLAIMS_DIR / f"{ISSUE}.json").read_text(), first)
        self.assertIn("already recorded", r["comment"])
        self.assertNotIn("newer upload received", r["comment"])

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


class TestIngestMatcherRealArchive(unittest.TestCase):
    """The archive shape LimeSurvey actually produces.

    ``TestIngestMatcher`` below fixtures a ``428628/101/fu_aaa111`` layout that was
    invented, never observed — and every one of its tests passed while the matcher
    resolved **0 of 8** files in the real 2026-08-17 export of survey 428628. A
    fixture that encodes a guess tests the guess.

    Real members look like ``00010_01-pdf_00-<slugified-original-name>.pdf``: a flat
    archive, zero-padded response id first, and no ``fu_`` name anywhere. The names
    below copy that shape; the slugs are invented, because a participant's own
    filenames do not belong in a public repo.
    """

    MEMBERS = [
        "00007_01-pdf_00-a-review-of-something.pdf",
        "00008_02-pdf_00-notes.pdf",
        "00012_03-pdf_00-notes.pdf",
        "00013_04-pdf_00-notes.pdf",
    ]

    def _u(self, **kw):
        import intake
        base = dict(issue=9, paper=PAPER, gh=AUTHOR, ref="7",
                    # LimeSurvey renames on export, so this is in the response and
                    # nowhere in the archive. Every real upload looks like this.
                    stored="fu_4xz7rek29uqpjt9", orig="A-Review-Of-Something.pdf")
        return intake.Upload(**{**base, **kw})

    def test_the_response_id_prefix_resolves_every_file(self):
        import intake
        for ref, expected in (("7", self.MEMBERS[0]), ("8", self.MEMBERS[1]),
                              ("12", self.MEMBERS[2]), ("13", self.MEMBERS[3])):
            with self.subTest(ref=ref):
                u = self._u(ref=ref, orig="Notes.pdf")
                self.assertEqual(intake.find_member(self.MEMBERS, u)[0], expected)

    def test_an_absent_stored_name_does_not_block_the_match(self):
        """The 0-of-8 case: rule 1 cannot fire, so rule 2 has to carry it."""
        import intake
        u = self._u(stored="fu_nowhere_in_this_archive")
        self.assertEqual(intake.find_member(self.MEMBERS, u)[0], self.MEMBERS[0])

    def test_zero_padding_is_not_part_of_the_key(self):
        """The width is LimeSurvey's business; ids are compared as numbers."""
        import intake
        self.assertEqual(intake._member_response_id("00008_02-pdf_00-notes.pdf"), 8)
        self.assertEqual(intake._member_response_id("8_02-pdf_00-notes.pdf"), 8)
        self.assertIsNone(intake._member_response_id("notes.pdf"))

    def test_response_7_is_not_confused_with_17_or_70(self):
        import intake
        members = ["00007_01-pdf_00-x.pdf", "00017_02-pdf_00-x.pdf",
                   "00070_03-pdf_00-x.pdf"]
        self.assertEqual(intake.find_member(members, self._u(ref="7", orig=""))[0],
                         "00007_01-pdf_00-x.pdf")

    def test_repeat_uploads_of_one_filename_resolve_by_id(self):
        """Three responses, one original filename — exactly the real export's shape.

        The name is useless here; the id is what separates them.
        """
        import intake
        got = [intake.find_member(self.MEMBERS, self._u(ref=r, orig="Notes.pdf"))[0]
               for r in ("8", "12", "13")]
        self.assertEqual(got, self.MEMBERS[1:])
        self.assertEqual(len(set(got)), 3, "two responses were handed the same file")

    def test_without_a_response_id_a_shared_filename_is_refused(self):
        import intake
        member, why = intake.find_member(self.MEMBERS, self._u(ref="", stored="",
                                                               orig="Notes.pdf"))
        self.assertIsNone(member)
        self.assertIn("ambiguous", why)

    def test_the_filename_fallback_is_case_insensitive(self):
        """LimeSurvey lowercases the slug; the response carries the original casing."""
        import intake
        u = self._u(ref="", stored="", orig="A-Review-Of-Something.pdf")
        self.assertEqual(intake.find_member(self.MEMBERS, u)[0], self.MEMBERS[0])

    def test_a_missing_file_names_the_response_id(self):
        """The old message said only stored= and name=, both of which look like junk
        to a reader — the id is the thing an organizer can actually look up."""
        import intake
        member, why = intake.find_member(self.MEMBERS, self._u(ref="99", orig="gone.pdf"))
        self.assertIsNone(member)
        self.assertIn("no archive file matches", why)
        self.assertIn("99", why)

    def test_slug_lowercases_and_leaves_punctuation_alone(self):
        import intake
        self.assertEqual(intake._slug("The-PREP-pipeline.standardized-EEG.pdf"),
                         "the-prep-pipeline.standardized-eeg.pdf")
        self.assertEqual(intake._slug("Pipeline-(HAPPE)-Data.pdf"),
                         "pipeline-(happe)-data.pdf")
        self.assertEqual(intake._slug("  my review.pdf "), "my-review.pdf")


class TestResponsesWithoutAFile(unittest.TestCase):
    """A response can reach the form and carry no file. It is not a lost upload.

    ~55 such rows came from one participant's 12-13 Aug attempts. Treated as uploads
    they became "no archive file matches this response" a line at a time and buried
    the five real ones under a wall of false alarms.
    """

    def _u(self, ref, at, **kw):
        import intake
        base = dict(issue=9, paper=PAPER, gh=AUTHOR, ref=ref, submitted_at=at,
                    stored="", orig="")
        return intake.Upload(**{**base, **kw})

    def test_a_blank_upload_cell_is_not_an_upload(self):
        import intake
        self.assertFalse(intake.has_file(self._u("1", "2026-08-12 10:00")))
        self.assertTrue(intake.has_file(
            self._u("2", "2026-08-12 10:00", stored="fu_abc")))
        self.assertTrue(intake.has_file(
            self._u("3", "2026-08-12 10:00", orig="review.pdf")))

    def test_a_later_empty_attempt_cannot_discard_an_earlier_real_upload(self):
        """The ordering that makes filtering-before-latest_per_claim necessary.

        Someone uploads successfully, then reopens the form and abandons it. Both are
        responses on the same claim, and the abandoned one is newer — so if it is still
        in the list when the tiebreak runs, it wins and the real review is dropped.
        """
        import intake
        good = self._u("10", "2026-08-12 10:00", stored="fu_real", orig="review.pdf")
        empty = self._u("11", "2026-08-13 09:00")

        naive = intake.latest_per_claim([good, empty])
        self.assertEqual(naive, [empty], "precondition: the empty attempt is newer")

        filtered = intake.latest_per_claim([u for u in (good, empty) if intake.has_file(u)])
        self.assertEqual(filtered, [good])

    def test_a_later_real_upload_still_wins(self):
        import intake
        first = self._u("10", "2026-08-12 10:00", stored="fu_1", orig="a.pdf")
        second = self._u("11", "2026-08-13 09:00", stored="fu_2", orig="b.pdf")
        kept = intake.latest_per_claim([u for u in (first, second) if intake.has_file(u)])
        self.assertEqual(kept, [second])


class TestIngestMatcher(unittest.TestCase):
    """`ingest` must never hand one participant's file to another.

    These fixture a ``fu_``-in-the-path layout that the 2026-08 export does not
    produce (see ``TestIngestMatcherRealArchive``). Kept because rule 1 is still the
    strongest key *if* an export ever carries it, and because they pin the
    never-guess-on-ambiguity contract — but they are not evidence the matcher works.
    """

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


class TestOrganizerRoster(unittest.TestCase):
    """The roster comes from the ORGANIZERS env var, set from a **repo variable**.

    `issue-ops.yml` passes `ORGANIZERS: ${{ vars.ORGANIZERS }}`, which renders as an
    empty string when the variable does not exist — and an empty roster means no one
    can run `/received`, `/reject` or an organizer-driven `/withdraw`. Production ran
    that way until 2026-08-17, silently, because the refusal reads like an ordinary
    permissions decision.

    The deployment fix is the repo variable, and there is deliberately no hard-coded
    fallback: one used to name a single person as an organizer of any repo running this
    code, while protecting nothing in CI. These tests pin that. If one starts failing,
    someone has reintroduced a default — a real decision, not a tidy-up.
    """

    def _roster(self, env):
        import importlib
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            return importlib.reload(issue_ops).ORGANIZERS

    def tearDown(self):
        import importlib
        importlib.reload(issue_ops)

    def test_empty_means_nobody(self):
        """An unset repo variable disables every organizer command. Documented, not liked."""
        self.assertEqual(self._roster({"ORGANIZERS": ""}), set())

    def test_absent_also_means_nobody(self):
        env = {k: v for k, v in os.environ.items() if k != "ORGANIZERS"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            import importlib
            self.assertEqual(importlib.reload(issue_ops).ORGANIZERS, set())

    def test_an_explicit_roster_wins(self):
        self.assertEqual(self._roster({"ORGANIZERS": "Alice,bob"}), {"alice", "bob"})

    def test_no_handle_is_baked_into_the_source(self):
        """A roster in the source is configuration in the wrong place."""
        src = (Path(issue_ops.__file__)).read_text()
        line = [ln for ln in src.splitlines() if ln.startswith("ORGANIZERS")][0]
        self.assertNotIn("oesteban", line)


class TestRetiredPaperOnALiveThread(Base):
    """A thread may hold a claim on a paper since retired from the pool.

    Retirement removes the entry from ``pool.json`` but never rewrites history, so
    ``claim["papers"]`` can name an id the pool no longer has (this is live: #36 and
    #38 still hold EEG-03 / EEG-08). Every command on that thread must keep working —
    a lookup that assumes the pool still has it locks the participant out of
    withdrawing, extending or confirming anything else on the same thread.
    """

    def _retire(self, pid):
        pool = json.loads(state.POOL_FILE.read_text())
        state.POOL_FILE.write_text(json.dumps([p for p in pool if p["id"] != pid]))

    def test_claiming_another_paper_still_works(self):
        self.claim_paper(PAPER)
        self._retire(PAPER)
        out = self.comment(f"/claim EEG-22")          # would KeyError on pool[PAPER]
        self.assertEqual(self.rec("EEG-22")["state"], "active")

    def test_withdrawing_the_retired_paper_still_works(self):
        self.claim_paper(PAPER)
        self._retire(PAPER)
        self.comment(f"/withdraw {PAPER}")
        self.assertEqual(self.rec(PAPER)["state"], "withdrawn")

    def test_a_retired_paper_cannot_be_claimed_again(self):
        self._retire(PAPER)
        out = self.claim_paper(PAPER)
        self.assertNotIn(PAPER, state.load_claims().get(ISSUE, {}).get("papers", {}))
        self.assertIn("not a paper id in the pool", out["comment"])


class TestMainArtifacts(Base):
    """``main()``'s on-disk contract — the two files ``announce.py`` consumes.

    Nothing exercised this before. It matters now: after the #40 reorder, ``comment.md``
    and ``actions.json`` are read *after* the push, and the rebase-retry loop depends on
    every invocation rewriting both of them. A path that quietly left a stale artifact
    behind would put the old bug straight back.
    """

    def run_main(self, payload, event_name):
        p = self.tmp / "event.json"
        p.write_text(json.dumps(payload))
        env = {"GITHUB_EVENT_PATH": str(p), "GITHUB_EVENT_NAME": event_name}
        # main() logs a one-line summary for the workflow log; swallow it so the test
        # run stays quiet.
        with unittest.mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(io.StringIO()):
            issue_ops.main()
        return ((state.REPO / "comment.md").read_text(),
                json.loads((state.REPO / "actions.json").read_text()))

    def claim_payload(self, pid=PAPER, issue=ISSUE, author=AUTHOR):
        # No "event_name" key: that is the real GitHub payload shape, and main() has to
        # fill it from GITHUB_EVENT_NAME.
        return {"action": "opened",
                "issue": {"number": issue, "user": {"login": author},
                          "labels": [{"name": "claim"}],
                          "body": f"{pid}\n- [x] I consent\nAttributed"}}

    def test_a_claim_writes_both_artifacts(self):
        comment, actions = self.run_main(self.claim_payload(), "issues")
        self.assertIn("claimed", comment)
        self.assertEqual(set(actions), {"issue", "add_labels", "assignees",
                                        "changed", "close", "close_comment"})
        self.assertEqual(actions["issue"], ISSUE)
        self.assertTrue(actions["changed"])
        self.assertIn("mod:EEG", actions["add_labels"])

    def test_event_name_comes_from_the_environment(self):
        """The payload carries no event_name; only GITHUB_EVENT_NAME says what this is."""
        _c, actions = self.run_main(self.claim_payload(), "")
        self.assertIsNone(actions["issue"])      # unknown event -> handled, silently

    def test_a_no_op_event_still_writes_both_artifacts(self):
        """The contract announce.py relies on: it always reads both files."""
        payload = {"action": "opened",
                   "issue": {"number": 77, "user": {"login": STRANGER},
                             "labels": [], "body": "just a bug report, no ids"}}
        comment, actions = self.run_main(payload, "issues")
        self.assertEqual(comment, "")
        self.assertFalse(actions["changed"])
        self.assertEqual(actions["issue"], 77)

    def test_the_artifacts_land_at_the_repo_root(self):
        """Where the workflow's working directory expects them."""
        self.run_main(self.claim_payload(), "issues")
        self.assertTrue((state.REPO / "comment.md").exists())
        self.assertTrue((state.REPO / "actions.json").exists())


class TestLostPushRace(Base):
    """Issue #40: the loser of a push race must be told it lost.

    Reproduces what run 30667267903 did on 2026-07-31. Two threads claim EEG-15 within
    seconds; the second computes an acceptance against pre-race state, loses the push,
    resets to the winner's tip and re-applies. ``shutil.copytree`` stands in for
    ``git reset --hard``.

    What the workflow used to do was post the *first*, discarded comment. The fix posts
    the *second*, and these tests pin that the second one is right — the whole reorder
    is worthless if the re-run cannot tell the loser apart from the winner.
    """
    WINNER, LOSER = 36, 37

    def _snapshot(self) -> Path:
        snap = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, snap, True)
        shutil.rmtree(snap)
        shutil.copytree(self.tmp, snap)
        return snap

    def _restore(self, snap: Path):
        shutil.rmtree(self.tmp)
        shutil.copytree(snap, self.tmp)

    def test_the_losing_thread_is_not_greeted_as_a_winner(self):
        """The delta was already right; the greeting above it was not.

        #43 opened with "you're in. Your reading starts now." and then listed the
        refusal underneath — the reply contradicting itself in its first two lines.
        """
        self.claim_paper(PAPER, issue=self.WINNER)
        losing = self.claim_paper(PAPER, issue=self.LOSER)["comment"]
        self.assertNotIn("you're in", losing)
        self.assertIn("didn't go through", losing)

    def test_the_winning_thread_is_still_welcomed(self):
        self.assertIn("you're in", self.claim_paper(PAPER, issue=self.WINNER)["comment"])

    def test_a_partial_success_still_counts_as_being_in(self):
        """One accepted, one refused: they did claim something, so greet them."""
        self.claim_paper(PAPER, issue=self.WINNER)
        mixed = issue_ops.handle_event({
            "event_name": "issues", "action": "opened",
            "issue": {"number": self.LOSER, "user": {"login": AUTHOR},
                      "labels": [{"name": "claim"}],
                      "body": f"{PAPER} EEG-22\n- [x] I consent\nAttributed"},
        })["comment"]
        self.assertIn("you're in", mixed)
        self.assertIn("Not applied", mixed)

    def test_the_losing_thread_is_told_it_lost(self):
        origin = self._snapshot()

        # attempt 1 — both threads see empty state, both would accept
        optimistic = self.claim_paper(PAPER, issue=self.LOSER)
        self.assertIn("claimed — due", optimistic["comment"])

        self._restore(origin)                        # git reset --hard origin/main
        self.claim_paper(PAPER, issue=self.WINNER)   # the commit that landed first

        # the re-apply inside the retry loop
        retried = self.claim_paper(PAPER, issue=self.LOSER)
        self.assertIn("you already hold this paper", retried["comment"])
        self.assertNotIn("claimed — due", retried["comment"])
        self.assertEqual(state.load_claims()[self.LOSER]["papers"], {})

    def test_the_winning_thread_keeps_its_claim(self):
        self.claim_paper(PAPER, issue=self.WINNER)
        self.claim_paper(PAPER, issue=self.LOSER)
        self.assertEqual(self.rec(PAPER, self.WINNER)["state"], "active")

    def test_reapplying_the_winners_event_leaves_its_claim_untouched(self):
        """`status.json` will differ by `generated_at` — that is deliberate, see the
        comment at issue_ops.py's save_claim. The *claim* must not move."""
        self.claim_paper(PAPER, issue=self.WINNER)
        before = (state.CLAIMS_DIR / f"{self.WINNER}.json").read_text()
        self.claim_paper(PAPER, issue=self.WINNER)
        self.assertEqual((state.CLAIMS_DIR / f"{self.WINNER}.json").read_text(), before)


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
