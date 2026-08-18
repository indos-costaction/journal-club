#!/usr/bin/env python3
"""Paper suggestions: parsing, verdicts, and the organizer's ``/accept-paper``.

    python -m unittest discover -s tests -v      # from the repo root

Same harness idea as ``test_flow.py``: a throwaway pool in a temp dir with ``state``'s
module paths repointed at it, because ``state.py`` resolves paths from ``__file__`` and a
test that forgets would append to the published pool.

The boundaries worth holding here are not the arithmetic, they are the ways this can go
quietly wrong:

* **an id landing in a retired gap** — ids are permanent handles that appear in claim
  records, issue threads and the ledger, so reusing one silently repoints old references;
* **the network being down** — triage runs in CI, and a bot that goes silent exactly when
  someone is waiting is worse than one that says less;
* **a second apply appending twice** — the workflow's rebase-retry loop re-runs the script
  rather than replaying a diff, so accepting has to be idempotent;
* **a titleless record** — undoing one means retiring an id, not editing a line.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import issue_ops  # noqa: E402
import params  # noqa: E402
import state  # noqa: E402
import suggest_ops as so  # noqa: E402

ORGANIZER = "oesteban"
STRANGER = "passer-by"

# A pool with one entry per modality we touch, so id allocation has something to count.
POOL = [
    {"id": "EEG-33", "modality": "EEG", "level": 1, "title": "An EEG paper",
     "first_author": "C. Pernet", "year": 2021, "doi": "10.3389/fninf.2021.610388",
     "url": "https://doi.org/10.3389/fninf.2021.610388", "venue": "Front. Neuroinform.",
     "s2_id": "abc", "citation_count": 10},
    {"id": "FNIRS-16", "modality": "fNIRS", "level": 1, "title": "An fNIRS paper",
     "first_author": "M. Rahman", "year": 2020, "doi": "10.1080/00207454.2020.1738439",
     "url": "https://doi.org/10.1080/00207454.2020.1738439", "venue": "Int. J. Neurosci.",
     "s2_id": "def", "citation_count": 5},
]

NEW_DOI = "10.1016/j.clinph.2014.05.014"
META = {"doi": NEW_DOI, "title": "Reliability of fully automated preprocessing",
        "first_author": "F. Hatz", "year": 2015, "venue": "Clinical Neurophysiology",
        "s2_id": "2b01", "citation_count": 60}


def fetch_ok(_ident):
    return dict(META)


def fetch_down(_ident):
    raise OSError("crossref unreachable")


def fetch_404(_ident):
    raise LookupError("not found")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, value in {
            "REPO": self.tmp,
            "DATA_DIR": self.tmp / "docs" / "data",
            "POOL_FILE": self.tmp / "docs" / "data" / "pool.json",
            "STATUS_FILE": self.tmp / "docs" / "data" / "status.json",
            "CLAIMS_DIR": self.tmp / "claims",
            "LEDGER_DIR": self.tmp / "ledger",
        }.items():
            p = unittest.mock.patch.object(state, name, value)
            p.start()
            self.addCleanup(p.stop)
        (self.tmp / "docs" / "data").mkdir(parents=True)
        (self.tmp / "claims").mkdir()
        state.POOL_FILE.write_text(json.dumps(POOL))

    def by_id(self, pool_list=None):
        return {p["id"]: p for p in (POOL if pool_list is None else pool_list)}

    def do_accept(self, argline, body, declared=None, pool_list=None, fetch=fetch_ok):
        pool_list = list(POOL if pool_list is None else pool_list)
        verdicts = so.triage(body, self.by_id(pool_list), fetch=None)
        return so.accept(argline, verdicts, declared, pool_list, fetch=fetch)


class TestParsing(Base):
    def test_the_shapes_people_actually_paste(self):
        body = ("10.1016/j.clinph.2014.05.014\n"
                "https://doi.org/10.3390/s23083979\n"
                "doi:10.1000/Xyz\n"
                "PMID: 41692284\n")
        got = so.parse_identifiers(body)
        self.assertEqual([i.doi for i in got],
                         ["10.1016/j.clinph.2014.05.014", "10.3390/s23083979",
                          "10.1000/xyz", ""])
        self.assertEqual(got[3].pmid, "41692284")

    def test_a_doi_at_the_end_of_a_sentence_loses_the_full_stop(self):
        self.assertEqual(so.normalise_doi("10.1000/xyz."), "10.1000/xyz")
        self.assertEqual(so.normalise_doi("(10.1000/xyz)"), "10.1000/xyz")

    def test_a_doi_keeps_its_own_balanced_brackets(self):
        """Real DOIs contain them; stripping blindly would corrupt the identifier."""
        self.assertEqual(so.normalise_doi("10.1002/(sici)1097-0193(1999)8:4<272::aid>3.0.co;2"),
                         "10.1002/(sici)1097-0193(1999)8:4<272::aid>3.0.co;2")

    def test_the_same_paper_twice_is_one_identifier(self):
        body = "10.1000/xyz\nhttps://doi.org/10.1000/XYZ\n"
        self.assertEqual(len(so.parse_identifiers(body)), 1)

    def test_only_the_papers_field_is_read(self):
        """The dropdown renders as "not sure" and an empty field as "_No response_", both
        short enough to pass for something someone typed. Reading the whole body reported
        them as unresolvable identifiers and made a clean suggestion look like an error."""
        body = ("### Papers\n\n10.1000/xyz\n\n### Modality\n\nnot sure\n\n"
                "### Why it belongs\n\n_No response_\n")
        got = so.parse_identifiers(body)
        self.assertEqual([i.raw for i in got], ["10.1000/xyz"])

    def test_prose_in_the_papers_field_is_still_dropped(self):
        body = ("### Papers\n\n10.1000/xyz\n"
                "It covers the motion correction case the pool is missing entirely.\n")
        self.assertEqual([i.doi for i in so.parse_identifiers(body)], ["10.1000/xyz"])

    def test_an_issue_filed_by_hand_has_its_whole_body_read(self):
        """No form, no headings — falling back is what keeps that path working."""
        self.assertEqual([i.doi for i in so.parse_identifiers("10.1000/xyz\n")],
                         ["10.1000/xyz"])

    def test_a_short_line_we_cannot_resolve_is_kept_not_dropped(self):
        """Someone who pasted a title deserves an answer, not silence."""
        got = so.parse_identifiers("Delorme 2004 EEGLAB\n")
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0].resolvable)

    def test_modality_is_matched_by_name_or_prefix_and_not_invented(self):
        self.assertEqual(so.parse_modality("eeg"), "EEG")
        self.assertEqual(so.parse_modality("fnirs"), "fNIRS")
        self.assertEqual(so.parse_modality("Cross-modality"), "Cross-modality")
        self.assertIsNone(so.parse_modality("not sure"))
        self.assertIsNone(so.parse_modality("EGG"))

    def test_the_declared_modality_is_read_out_of_the_rendered_form(self):
        body = "### Papers\n\n10.1000/xyz\n\n### Modality\n\nfNIRS\n\n### Why it belongs\n\n_No response_\n"
        self.assertEqual(so.declared_modality(body), "fNIRS")


class TestVerdicts(Base):
    def ident(self, doi):
        return so.Identifier(raw=doi, doi=so.normalise_doi(doi))

    def test_a_paper_we_hold_is_named_by_its_pool_id(self):
        v = so.verdict_for(self.ident("10.3389/fninf.2021.610388"), self.by_id())
        self.assertEqual((v.kind, v.detail), ("known", "EEG-33"))

    def test_the_match_ignores_case_and_resolver_prefix(self):
        v = so.verdict_for(self.ident("HTTPS://doi.org/10.3389/FNINF.2021.610388"), self.by_id())
        self.assertEqual(v.kind, "known")

    def test_a_previously_declined_paper_says_so(self):
        with unittest.mock.patch.dict(params.DECLINED, {"10.1000/xyz": "textbook"}, clear=False):
            v = so.verdict_for(self.ident("10.1000/xyz"), self.by_id())
        self.assertEqual((v.kind, v.detail), ("declined", "textbook"))

    def test_an_unknown_paper_carries_the_citation(self):
        v = so.verdict_for(self.ident(NEW_DOI), self.by_id(), fetch=fetch_ok)
        self.assertEqual(v.kind, "new")
        self.assertIn("Clinical Neurophysiology", v.detail)

    def test_a_line_with_no_identifier_is_unresolvable(self):
        v = so.verdict_for(so.Identifier(raw="Delorme 2004"), self.by_id(), fetch=fetch_ok)
        self.assertEqual(v.kind, "unresolvable")


class TestOfflineDegradation(Base):
    """The path most likely to run in anger: CI has no network, or Crossref is down."""

    def test_the_duplicate_check_still_works(self):
        with contextlib.redirect_stdout(io.StringIO()):
            v = so.verdict_for(so.Identifier(raw="x", doi="10.3389/fninf.2021.610388"),
                               self.by_id(), fetch=fetch_down)
        self.assertEqual((v.kind, v.detail), ("known", "EEG-33"))

    def test_an_unknown_paper_is_still_reported_just_without_a_citation(self):
        with contextlib.redirect_stdout(io.StringIO()):
            v = so.verdict_for(so.Identifier(raw="x", doi=NEW_DOI), self.by_id(),
                               fetch=fetch_down)
        self.assertEqual((v.kind, v.detail), ("new", ""))

    def test_the_reply_admits_the_citation_is_unconfirmed(self):
        with contextlib.redirect_stdout(io.StringIO()):
            body = so.render_triage(so.triage(f"{NEW_DOI}\n", self.by_id(), fetch=fetch_down))
        self.assertIn("could not reach", body.lower())

    def test_a_dead_identifier_is_not_reported_as_an_outage(self):
        """Found live on #46: a DOI that resolves to nothing was answered with "we could
        not reach the metadata service", which sends someone away to wait when what they
        need is to check what they pasted."""
        with contextlib.redirect_stdout(io.StringIO()):
            v = so.verdict_for(so.Identifier(raw="x", doi="10.9999/nope"), self.by_id(),
                               fetch=fetch_404)
        self.assertEqual(v.kind, "unknown_id")
        body = so.render_triage([v])
        self.assertIn("does not resolve", body.lower())
        self.assertNotIn("could not reach", body.lower())

    def test_a_dead_identifier_cannot_be_accepted(self):
        with contextlib.redirect_stdout(io.StringIO()):
            out, pool = self.do_accept(" EEG", "10.9999/nope\n", fetch=fetch_404)
        self.assertEqual(out.ok, [])
        self.assertEqual(pool, POOL)

    def test_the_failure_is_a_warning_not_an_error(self):
        """An annotation on a green run, not a red one: triage did its job."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            so.verdict_for(so.Identifier(raw="x", doi=NEW_DOI), self.by_id(), fetch=fetch_down)
        self.assertTrue(buf.getvalue().startswith("::warning::"), buf.getvalue())

    def test_triage_never_claims_a_paper_is_in_scope(self):
        """in_scope() cannot see a textbook that entered through a review of the book."""
        body = so.render_triage(so.triage(f"{NEW_DOI}\n", self.by_id(), fetch=fetch_ok))
        self.assertNotIn("in scope", body.lower())


class TestIdAllocation(Base):
    def test_it_continues_from_the_highest_used(self):
        self.assertEqual(so.next_id(self.by_id(), "EEG"), "EEG-34")
        self.assertEqual(so.next_id(self.by_id(), "fNIRS"), "FNIRS-17")

    def test_it_never_lands_in_a_retired_gap(self):
        """EEG-03/-04/-07/-08/-17 are free-looking and permanently spoken for."""
        pool = self.by_id()
        for _ in range(3):
            pid = so.next_id(pool, "EEG")
            self.assertNotIn(pid, params.RETIRED)
            pool[pid] = {"id": pid, "modality": "EEG", "doi": f"10.1/{pid}"}

    def test_a_retired_id_above_the_top_still_pushes_the_next_one_past_it(self):
        with unittest.mock.patch.dict(params.RETIRED, {"EEG-40": "…"}, clear=False):
            self.assertEqual(so.next_id(self.by_id(), "EEG"), "EEG-41")

    def test_an_empty_modality_starts_at_one(self):
        self.assertEqual(so.next_id(self.by_id(), "PET"), "PET-01")

    def test_seed_ids_are_a_separate_series_and_are_not_counted(self):
        pool = self.by_id() | {"EEG-R9": {"id": "EEG-R9", "modality": "EEG", "doi": "10.1/r9"}}
        self.assertEqual(so.next_id(pool, "EEG"), "EEG-34")


class TestAccept(Base):
    def test_it_adds_the_paper_and_names_the_id(self):
        out, pool = self.do_accept(" EEG", f"{NEW_DOI}\n")
        self.assertEqual(out.rejected, [])
        self.assertIn("EEG-34", out.ok[0])
        self.assertEqual(pool[-1]["id"], "FNIRS-16")        # inserted in its own block
        added = next(p for p in pool if p["id"] == "EEG-34")
        self.assertEqual((added["doi"], added["level"]), (NEW_DOI, 1))

    def test_the_record_lands_in_its_modality_block(self):
        _out, pool = self.do_accept(" fNIRS", f"{NEW_DOI}\n")
        ids = [p["id"] for p in pool]
        self.assertEqual(ids, ["EEG-33", "FNIRS-16", "FNIRS-17"])

    def test_the_proposers_modality_is_used_when_the_command_omits_one(self):
        out, _pool = self.do_accept("", f"{NEW_DOI}\n", declared="fNIRS")
        self.assertIn("FNIRS-17", out.ok[0])

    def test_the_command_overrides_the_proposer(self):
        out, _pool = self.do_accept(" EEG", f"{NEW_DOI}\n", declared="fNIRS")
        self.assertIn("EEG-34", out.ok[0])

    def test_a_doi_argument_singles_one_out(self):
        body = f"{NEW_DOI}\n10.3390/s23083979\n"
        out, pool = self.do_accept(f" {NEW_DOI} EEG", body)
        self.assertEqual(len(out.ok), 1)
        self.assertEqual(len(pool), len(POOL) + 1)

    def test_argument_order_does_not_matter(self):
        out, _pool = self.do_accept(f" EEG {NEW_DOI}", f"{NEW_DOI}\n")
        self.assertIn("EEG-34", out.ok[0])

    def test_a_paper_without_a_modality_is_refused_by_name_while_others_land(self):
        """A partial accept has to be explicit; a silent skip is how a paper gets lost."""
        body = f"{NEW_DOI}\n10.3390/s23083979\n"
        with unittest.mock.patch.object(
                so, "lookup", lambda i: dict(META, doi=i.doi, title="T")):
            out, pool = self.do_accept("", body, declared=None,
                                       fetch=lambda i: dict(META, doi=i.doi, title="T"))
        self.assertEqual(out.ok, [])
        self.assertEqual(len(out.rejected), 2)
        self.assertIn("no modality", out.rejected[0].lower())
        self.assertEqual(pool, POOL)

    def test_a_paper_we_already_hold_is_an_accepted_no_op(self):
        """The organizer asked for an end state that already holds; that is not a failure,
        and treating it as one is what would make the retry loop announce a false alarm."""
        out, pool = self.do_accept(" EEG", "10.3389/fninf.2021.610388\n")
        self.assertEqual(out.rejected, [])
        self.assertIn("EEG-33", out.ok[0])
        self.assertEqual(pool, POOL)

    def test_a_previously_declined_paper_is_refused(self):
        with unittest.mock.patch.dict(params.DECLINED, {NEW_DOI: "textbook"}, clear=False):
            out, pool = self.do_accept(" EEG", f"{NEW_DOI}\n")
        self.assertIn("textbook", out.rejected[0])
        self.assertEqual(pool, POOL)

    def test_a_typo_for_a_modality_stops_the_command_rather_than_accepting_everything(self):
        out, pool = self.do_accept(" EGG", f"{NEW_DOI}\n")
        self.assertEqual(out.ok, [])
        self.assertIn("EGG", out.rejected[0])
        self.assertEqual(pool, POOL)

    def test_a_doi_not_on_the_thread_is_refused(self):
        out, pool = self.do_accept(" 10.9999/elsewhere EEG", f"{NEW_DOI}\n")
        self.assertIn("not one of the identifiers", out.rejected[0])
        self.assertEqual(pool, POOL)

    def test_a_failed_lookup_refuses_rather_than_writing_a_titleless_record(self):
        """Undoing a bad pool entry means retiring an id, not editing a line."""
        with contextlib.redirect_stdout(io.StringIO()):
            out, pool = self.do_accept(" EEG", f"{NEW_DOI}\n", fetch=fetch_down)
        self.assertEqual(out.ok, [])
        self.assertEqual(pool, POOL)

    def test_metadata_with_no_title_is_treated_as_a_failed_lookup(self):
        out, pool = self.do_accept(" EEG", f"{NEW_DOI}\n",
                                   fetch=lambda i: dict(META, title="  "))
        self.assertEqual(pool, POOL)
        self.assertIn("could not fetch", out.rejected[0].lower())


class TestAcceptIsIdempotent(Base):
    """The rebase-retry loop re-runs the script; a second apply must not append twice."""

    def test_applying_twice_leaves_the_pool_byte_identical(self):
        _out, once = self.do_accept(" EEG", f"{NEW_DOI}\n")
        out, twice = self.do_accept(" EEG", f"{NEW_DOI}\n", pool_list=once)
        self.assertEqual(json.dumps(twice), json.dumps(once))
        self.assertEqual(out.rejected, [])
        self.assertIn("nothing to do", out.ok[0].lower())

    def test_the_id_is_not_consumed_by_the_second_run(self):
        _out, once = self.do_accept(" EEG", f"{NEW_DOI}\n")
        _out, twice = self.do_accept(" EEG", f"{NEW_DOI}\n", pool_list=once)
        self.assertEqual([p["id"] for p in twice].count("EEG-34"), 1)


class TestTheOrganizerGate(Base):
    def event(self, actor, body="/accept-paper EEG"):
        return {"event_name": "issue_comment",
                "issue": {"number": 52, "labels": [{"name": "paper-suggestion"}],
                          "body": f"{NEW_DOI}\n"},
                "comment": {"user": {"login": actor}, "body": body}}

    def test_a_stranger_is_refused_and_writes_nothing(self):
        got = so.handle_event(self.event(STRANGER), fetch=fetch_ok,
                              organizers={ORGANIZER}, pool_list=list(POOL))
        self.assertFalse(got["changed"])
        self.assertIsNone(got.get("pool"))
        self.assertIn("organizers only", got["comment"].lower())

    def test_an_organizer_may_run_it(self):
        got = so.handle_event(self.event(ORGANIZER), fetch=fetch_ok,
                              organizers={ORGANIZER}, pool_list=list(POOL))
        self.assertTrue(got["changed"])
        self.assertIn("EEG-34", got["comment"])

    def test_the_roster_comes_from_issue_ops_rather_than_a_second_copy(self):
        """One roster, one env variable, one place to get wrong."""
        with unittest.mock.patch.object(issue_ops, "ORGANIZERS", {ORGANIZER}):
            got = so.handle_event(self.event(ORGANIZER), fetch=fetch_ok,
                                  pool_list=list(POOL))
        self.assertTrue(got["changed"])

    def test_an_empty_roster_refuses_everyone(self):
        got = so.handle_event(self.event(ORGANIZER), fetch=fetch_ok, organizers=set(),
                              pool_list=list(POOL))
        self.assertFalse(got["changed"])


class TestRouting(Base):
    def test_an_issue_without_the_label_is_ignored_entirely(self):
        got = so.handle_event({"event_name": "issues",
                               "issue": {"number": 9, "labels": [{"name": "claim"}],
                                         "body": f"{NEW_DOI}\n"}})
        self.assertEqual((got["comment"], got["changed"]), ("", False))

    def test_a_comment_without_the_command_is_a_silent_no_op(self):
        got = so.handle_event({"event_name": "issue_comment",
                               "issue": {"number": 52, "body": "",
                                         "labels": [{"name": "paper-suggestion"}]},
                               "comment": {"user": {"login": ORGANIZER},
                                           "body": "looks good to me"}})
        self.assertEqual((got["comment"], got["changed"]), ("", False))

    def test_a_suggestion_with_nothing_resolvable_says_that_and_not_something_else(self):
        """"Nothing new here" would be wrong: nothing was READ, let alone looked up."""
        got = so.handle_event({"event_name": "issues",
                               "issue": {"number": 52,
                                         "labels": [{"name": "paper-suggestion"}],
                                         "body": "the Luck book"}})
        self.assertIn("none of these resolved", got["comment"].lower())
        self.assertNotIn("already hold", got["comment"].lower())

    def test_a_body_with_no_candidate_lines_at_all_says_so(self):
        got = so.handle_event({"event_name": "issues",
                               "issue": {"number": 52,
                                         "labels": [{"name": "paper-suggestion"}],
                                         "body": "please add the Luck ERP book to the pool"}})
        self.assertIn("could not find", got["comment"].lower())

    def test_an_all_duplicates_thread_is_labelled_duplicate(self):
        got = so.handle_event({"event_name": "issues",
                               "issue": {"number": 52,
                                         "labels": [{"name": "paper-suggestion"}],
                                         "body": "10.3389/fninf.2021.610388\n"}})
        self.assertEqual(got["add_labels"], ["duplicate"])

    def test_a_thread_with_something_new_is_not(self):
        got = so.handle_event({"event_name": "issues",
                               "issue": {"number": 52,
                                         "labels": [{"name": "paper-suggestion"}],
                                         "body": f"{NEW_DOI}\n"}}, fetch=fetch_ok)
        self.assertEqual(got["add_labels"], [])

    def test_the_thread_closes_once_nothing_is_left_to_add(self):
        got = so.handle_event(
            {"event_name": "issue_comment",
             "issue": {"number": 52, "labels": [{"name": "paper-suggestion"}],
                       "body": f"{NEW_DOI}\n"},
             "comment": {"user": {"login": ORGANIZER}, "body": "/accept-paper EEG"}},
            fetch=fetch_ok, organizers={ORGANIZER}, pool_list=list(POOL))
        self.assertTrue(got["close"])

    def test_a_thread_with_a_paper_still_pending_stays_open(self):
        body = f"{NEW_DOI}\n10.3390/s23083979\n"
        got = so.handle_event(
            {"event_name": "issue_comment",
             "issue": {"number": 52, "labels": [{"name": "paper-suggestion"}], "body": body},
             "comment": {"user": {"login": ORGANIZER},
                         "body": f"/accept-paper {NEW_DOI} EEG"}},
            fetch=fetch_ok, organizers={ORGANIZER}, pool_list=list(POOL))
        self.assertTrue(got["changed"])
        self.assertFalse(got["close"])


class TestDeclinedRegistry(unittest.TestCase):
    def test_every_entry_carries_a_reason(self):
        for doi, why in params.DECLINED.items():
            self.assertTrue((why or "").strip(), f"{doi} is declined with no reason")

    def test_keys_are_normalised_dois(self):
        """Lookups normalise before matching, so an unnormalised key never fires."""
        for doi in params.DECLINED:
            self.assertEqual(doi, so.normalise_doi(doi))


if __name__ == "__main__":
    unittest.main()
