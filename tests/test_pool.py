#!/usr/bin/env python3
"""Pool scope and id stability.

    python -m unittest discover -s tests -v      # from the repo root

These run against the **published** ``docs/data/pool.json``, not a fixture. That is
the point: the pool is hand-maintained between generations (``seed_pool.py`` no
longer reproduces it), so the only thing standing between a stray edit and a
textbook back in the reading list is an assertion that reads the real file.

Two invariants, and they pull in opposite directions:

* **scope** — every entry is a paper-shaped unit (``params.in_scope``); books,
  textbooks and handbooks are out (``params.RETIRED``);
* **id stability** — a retired id is never reused and never renumbered onto another
  paper. Ids appear in claim records, issue threads and the ledger, so an id that
  changes meaning silently re-points somebody's finished review.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import params  # noqa: E402
import seed_pool  # noqa: E402

POOL_FILE = Path(__file__).resolve().parent.parent / "docs" / "data" / "pool.json"


class TestPublishedPool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pool = json.loads(POOL_FILE.read_text())

    def test_every_entry_is_in_scope(self):
        """No book, handbook or phantom record survives in the published pool."""
        bad = [(p["id"], p["title"], params.in_scope(p)[1])
               for p in self.pool if not params.in_scope(p)[0]]
        self.assertEqual(bad, [], "out-of-scope entries in the published pool")

    def test_no_retired_id_is_present(self):
        present = sorted({p["id"] for p in self.pool} & set(params.RETIRED))
        self.assertEqual(present, [], "retired ids are back in the pool")

    def test_every_retired_id_carries_a_reason(self):
        """A bare id set rots into folklore; the reason is the record."""
        for pid, why in params.RETIRED.items():
            self.assertTrue(why and why.strip(), f"{pid} retired without a reason")

    def test_ids_are_unique(self):
        ids = [p["id"] for p in self.pool]
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_two_entries_are_the_same_work(self):
        """EEG-03 and EEG-07 were the same textbook under two S2 records.

        Dedup keys on ``doi or paperId`` (consolidate.py), so two records of one
        work with no DOI never collide. Normalised title is the check that would
        have caught it.
        """
        import re
        seen = {}
        for p in self.pool:
            key = " ".join(re.sub(r"[^a-z0-9]+", " ", p["title"].lower()).split())
            self.assertNotIn(key, seen,
                             f"{p['id']} duplicates {seen.get(key)}: {p['title']}")
            seen[key] = p["id"]

    def test_a_real_pre_doi_paper_is_not_screened_out(self):
        """The scope rule must separate books from old journal papers.

        Several legitimate entries predate DOIs (the Journal of Nuclear Medicine
        run). They keep a venue, which is exactly what distinguishes them from the
        MAG book records — so a rule of 'no DOI' alone would be wrong.
        """
        pre_doi = [p for p in self.pool
                   if not (p.get("doi") or "").strip() and (p.get("venue") or "").strip()]
        self.assertTrue(pre_doi, "expected pre-DOI journal papers to still be in the pool")
        for p in pre_doi:
            self.assertTrue(params.in_scope(p)[0], f"{p['id']} wrongly screened out")


class TestScopeRule(unittest.TestCase):
    def test_book_signature_is_rejected(self):
        ok, why = params.in_scope({"doi": None, "venue": ""})
        self.assertFalse(ok)
        self.assertIn("no DOI and no venue", why)

    def test_venue_alone_is_enough(self):
        self.assertTrue(params.in_scope({"doi": "", "venue": "Journal of Nuclear Medicine"})[0])

    def test_doi_alone_is_enough(self):
        self.assertTrue(params.in_scope({"doi": "10.1016/j.neuroimage.2011.09.015", "venue": ""})[0])

    def test_whitespace_is_not_a_venue(self):
        self.assertFalse(params.in_scope({"doi": "  ", "venue": "   "})[0])


class TestScreenPreservesIds(unittest.TestCase):
    """``screen()`` must filter *after* numbering, never before."""

    # A synthetic prefix on purpose: real ids would collide with params.RETIRED and
    # the test would pass for the wrong reason.
    def _pool(self):
        return [{"id": f"FAKE-{i:02d}", "modality": "EEG", "level": 1, "title": f"t{i}",
                 "doi": f"10.0/{i}", "venue": "Journal"} for i in range(1, 6)]

    def test_surviving_ids_do_not_shift(self):
        pool = self._pool()
        pool[1]["doi"], pool[1]["venue"] = None, ""      # FAKE-02 is a book
        kept, dropped = seed_pool.screen(pool)
        self.assertEqual([p["id"] for p in kept],
                         ["FAKE-01", "FAKE-03", "FAKE-04", "FAKE-05"])
        self.assertEqual([d[0] for d in dropped], ["FAKE-02"])

    def test_retired_ids_are_dropped_with_their_reason(self):
        pool = self._pool()
        retired_id = next(iter(params.RETIRED))
        pool[0]["id"] = retired_id
        kept, dropped = seed_pool.screen(pool)
        self.assertEqual(dropped, [(retired_id, params.RETIRED[retired_id])])
        self.assertNotIn(retired_id, [p["id"] for p in kept])

    def test_nothing_is_dropped_silently(self):
        """Every drop is reported, so a caller cannot mistake a filtered pool for a full one."""
        pool = self._pool()
        pool[2]["doi"], pool[2]["venue"] = "", None
        kept, dropped = seed_pool.screen(pool)
        self.assertEqual(len(kept) + len(dropped), len(pool))


if __name__ == "__main__":
    unittest.main()
