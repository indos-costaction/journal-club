#!/usr/bin/env python3
"""What the public site is told, and whether it can still draw it.

    python -m unittest discover -s tests -v      # from the repo root

Two halves, both of which were unguarded until the mosaic grew a fourth state.

**The derived data.** ``paper_status()`` and ``compute_status()`` had no test at all,
which is a strange gap for the two functions that produce every number on the landing
page. The boundaries that matter are the ones where the two ladders disagree: claimability
(``status``) closes at five claimants and knows nothing about reviews; progress
(``progress``) moves only on graded reviews and knows nothing about the cap. A paper can
be closed to new claims and still show as untouched, and that is correct.

**The rendering.** The progress ladder is spelled out in four places that no compiler
checks against each other: ``state.py`` names the buckets, ``app.js`` words them,
``style.css`` colours them, ``index.html`` legends them. Adding a fifth bucket and
forgetting one of the four produces a tile with no colour, or a colour with no legend,
and nothing fails. So the vocabulary is linted as text, in the spirit of
``test_workflows.py``.

``TestMosaicContrast`` is the one that earns its keep. The mosaic sits on
``--logo-blue``, and the four logo colours measure 6.2:1, 7.5:1, 3.5:1 and 1.4:1 against
it. Those are measurements, not opinions, and they decided the design: three tiles that
must be spottable alone clear the bar, the completed tile does not and is allowed not to
(a rim to rescue it reads as a hollow checkbox, and any purple light enough to pass stops
being distinguishable from the lavender beside it), and the whole palette is checked for
pairwise separability instead — which is the property that actually matters in a grid.
"""
from __future__ import annotations

import itertools
import json
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import params  # noqa: E402
import state  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "docs"
CSS = (DOCS / "style.css").read_text()
JS = (DOCS / "app.js").read_text()
HTML = (DOCS / "index.html").read_text()


# --- WCAG 2.1 relative luminance + contrast, per the spec's own formulas ---------
def _luminance(hexcolour: str) -> float:
    r, g, b = (int(hexcolour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _lab(hexcolour: str) -> tuple[float, float, float]:
    """sRGB -> CIE L*a*b* (D65). Enough for "are these two tiles the same colour?",
    which luminance contrast cannot answer: turquoise and orange are within 1.2:1 of
    each other and nobody would confuse them."""
    r, g, b = (int(hexcolour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b)
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)  # noqa: E731
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    """CIE76 colour difference. ~2.3 is the just-noticeable step; tiles need far more."""
    return sum((p - q) ** 2 for p, q in zip(_lab(a), _lab(b))) ** 0.5


def css_var(name: str) -> str:
    """Resolve a custom property to a literal hex, following one level of var()."""
    seen = set()
    while name not in seen:
        seen.add(name)
        m = re.search(rf"^\s*{re.escape(name)}:\s*([^;]+);", CSS, re.MULTILINE)
        if not m:
            raise AssertionError(f"style.css declares no {name}")
        value = m.group(1).strip()
        if value.startswith("#"):
            return value
        inner = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value)
        if not inner:
            raise AssertionError(f"{name} is {value!r}, neither a hex nor a plain var()")
        name = inner.group(1)
    raise AssertionError(f"circular var() chain at {name}")


class StatusBase(unittest.TestCase):
    """A throwaway pool, so nothing here can read or rewrite the published one."""

    PAPER = "EEG-15"

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
            patcher = unittest.mock.patch.object(state, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.tmp / "docs" / "data").mkdir(parents=True)
        state.POOL_FILE.write_text(json.dumps([
            {"id": self.PAPER, "modality": "EEG", "level": 1, "title": "A paper",
             "first_author": "Mutanen", "year": 2022, "url": "https://doi.org/10.x"},
        ]))

    def claims_in(self, *states: str) -> dict:
        """One claim file per state, all on the same paper, each by a different person."""
        return {i: {"issue": i, "participant": f"reader{i}", "attribution": "attributed",
                    "papers": {self.PAPER: {"state": st}}}
                for i, st in enumerate(states, start=1)}

    def paper(self, *states: str) -> dict:
        return state.compute_status(state.load_pool(),
                                    self.claims_in(*states))["papers"][self.PAPER]


class TestPaperProgress(StatusBase):
    """The four buckets, at their boundaries."""

    def test_untouched_is_open(self):
        self.assertEqual(state.paper_progress(live=0, delivered=0), "open")

    def test_a_single_claimant_is_already_in_review(self):
        """The whole reason progress is not `status`: `status` would still say open."""
        self.assertEqual(state.paper_progress(live=1, delivered=0), "review")
        self.assertEqual(state.paper_status(live=1, delivered=0), "open")

    def test_one_confirmed_review_is_partial(self):
        self.assertEqual(state.paper_progress(live=0, delivered=1), "partial")

    def test_one_short_of_the_threshold_is_still_partial(self):
        self.assertEqual(
            state.paper_progress(live=0, delivered=params.COMPLETION_THRESHOLD - 1),
            "partial")

    def test_the_threshold_is_done(self):
        self.assertEqual(
            state.paper_progress(live=0, delivered=params.COMPLETION_THRESHOLD), "done")

    def test_a_late_extra_claim_does_not_un_finish_a_paper(self):
        self.assertEqual(
            state.paper_progress(live=1, delivered=params.COMPLETION_THRESHOLD), "done")

    def test_closed_to_claims_is_not_a_progress_state(self):
        """Five claimants and nothing delivered: unclaimable, and visibly not started."""
        live = params.POOL_CLOSE_THRESHOLD
        self.assertEqual(state.paper_status(live, 0), "closed")
        self.assertEqual(state.paper_progress(live, 0), "review")

    def test_withdrawing_the_only_claim_returns_the_tile_to_open(self):
        self.assertEqual(self.paper("active")["progress"], "review")
        self.assertEqual(self.paper("withdrawn")["progress"], "open")

    def test_a_confirmed_review_moves_the_tile_without_waiting_for_a_grade(self):
        """The change of 2026-08-18, at the level the visitor sees it.

        Before it, `submitted` was in flight and this tile read `review` — which with an
        empty ledger meant six delivered reviews rendered as an untouched pool.
        """
        self.assertEqual(self.paper("submitted")["progress"], "partial")
        self.assertEqual(self.paper(*(["submitted"] * params.COMPLETION_THRESHOLD)
                                    )["progress"], "done")

    def test_every_reachable_bucket_is_declared(self):
        """PROGRESS_STATES is what the site lint below enumerates; keep it honest."""
        reachable = {state.paper_progress(live, done)
                     for live in range(0, params.POOL_CLOSE_THRESHOLD + 1)
                     for done in range(0, params.COMPLETION_THRESHOLD + 1)}
        self.assertEqual(reachable, set(state.PROGRESS_STATES))


class TestGradingDoesNotMoveTheBoard(StatusBase):
    """Grading decides points. It is not what makes a review count.

    One assertion carries the whole change: the board a paper shows with three confirmed
    reviews is the board it shows once those same three are scored.
    """

    BOARD = ("live_claims", "reviews_confirmed", "reviews_awaiting_signoff",
             "status", "progress", "outstanding_need")

    def test_scoring_a_review_changes_nothing_a_visitor_can_see(self):
        for n in range(1, params.COMPLETION_THRESHOLD + 1):
            with self.subTest(reviews=n):
                confirmed = self.paper(*(["submitted"] * n))
                graded = self.paper(*(["completed"] * n))
                self.assertEqual({k: confirmed[k] for k in self.BOARD},
                                 {k: graded[k] for k in self.BOARD})

    def test_the_graded_count_is_the_one_thing_that_does_move(self):
        """Published for the hover card and the under-served bonus; drives nothing."""
        self.assertEqual(self.paper("submitted")["reviews_graded"], 0)
        self.assertEqual(self.paper("completed")["reviews_graded"], 1)

    def test_a_paper_closes_to_new_claims_on_confirmed_reviews(self):
        """Claimability moves with progress, or the mosaic would say `complete` over a
        live Claim button in the table below it."""
        done = ["submitted"] * params.COMPLETION_THRESHOLD
        self.assertEqual(self.paper(*done)["status"], "done")


class TestAwaitingSignoff(StatusBase):
    """`reviews_awaiting_signoff`: uploaded, and nobody has put their name on it yet."""

    def test_it_counts_uploads_nobody_has_confirmed(self):
        self.assertEqual(self.paper("pending", "pending")["reviews_awaiting_signoff"], 2)

    def test_a_claim_with_no_upload_is_not_awaiting_anything(self):
        self.assertEqual(self.paper("active")["reviews_awaiting_signoff"], 0)

    def test_confirming_moves_a_review_out_of_it_and_into_the_count(self):
        p = self.paper("submitted")
        self.assertEqual(p["reviews_awaiting_signoff"], 0)
        self.assertEqual(p["reviews_confirmed"], 1)

    def test_freed_claims_count_for_nothing(self):
        for st in sorted(state.FREED):
            self.assertEqual(self.paper(st)["reviews_awaiting_signoff"], 0, st)

    def test_it_is_a_subset_of_live_claims_not_an_addition_to_them(self):
        """A pending paper is still out, and both numbers are published."""
        p = self.paper("active", "pending", "submitted")
        self.assertEqual(p["live_claims"], 2, "a confirmed review is not still out")
        self.assertEqual(p["reviews_awaiting_signoff"], 1)
        self.assertEqual(p["reviews_confirmed"], 1)


class TestOutstandingNeedFollowsConfirmedReviews(StatusBase):
    """The public headline number counts finished reviews, and only those.

    `total_outstanding` is on the landing page ("N reviews still needed") and
    `outstanding_need` drives the "still needs reviews" filter. An upload nobody has
    signed off must not shrink the ask — the form is a public link, so a file on its own
    does not yet make a review — and a review that *has* been signed off must.
    """

    def test_an_unconfirmed_upload_does_not_reduce_the_need(self):
        need = params.COMPLETION_THRESHOLD
        self.assertEqual(self.paper()["outstanding_need"], need)
        self.assertEqual(self.paper("pending", "pending")["outstanding_need"], need)

    def test_confirming_reduces_it_and_grading_does_not_reduce_it_again(self):
        self.assertEqual(self.paper("submitted")["outstanding_need"],
                         params.COMPLETION_THRESHOLD - 1)
        self.assertEqual(self.paper("completed")["outstanding_need"],
                         params.COMPLETION_THRESHOLD - 1)

    def test_a_review_returned_below_the_floor_puts_the_need_back(self):
        """The one place grading still moves the board, asserted rather than assumed.

        A tile can go from `done` back to `partial`, and the paper reopens with it. That
        is correct: at 2.0/5 the floor is a "did you actually annotate it" bar.
        """
        done = ["submitted"] * params.COMPLETION_THRESHOLD
        regressed = done[:-1] + ["returned"]
        self.assertEqual(self.paper(*done)["outstanding_need"], 0)
        p = self.paper(*regressed)
        self.assertEqual(p["outstanding_need"], 1)
        self.assertEqual(p["progress"], "partial")
        self.assertEqual(p["status"], "open", "the paper has to be claimable again")

    def test_an_organizer_rejection_puts_it_back_too(self):
        done = ["submitted"] * params.COMPLETION_THRESHOLD
        self.assertEqual(self.paper(*(done[:-1] + ["rejected"]))["outstanding_need"], 1)

    def test_it_never_goes_negative(self):
        over = ["submitted"] * (params.COMPLETION_THRESHOLD + 2)
        self.assertEqual(self.paper(*over)["outstanding_need"], 0)


class TestSiteVocabulary(unittest.TestCase):
    """One ladder, spelled out in four files that nothing else cross-checks."""

    # assertTrue rather than assertIn: a failing assertIn prints the whole 18 KB
    # stylesheet as the haystack, which buries the one line that says what is wrong.
    def test_every_bucket_has_a_tile_rule(self):
        for st in state.PROGRESS_STATES:
            self.assertTrue(f".mosaic a.s-{st}" in CSS,
                            f"style.css has no .mosaic a.s-{st} — a `{st}` paper would "
                            f"fall back to the base tile colour")

    def test_every_bucket_has_a_swatch_rule(self):
        for st in state.PROGRESS_STATES:
            self.assertTrue(re.search(rf"^\.k-{st}\b", CSS, re.MULTILINE),
                            f"style.css has no .k-{st} — the legend and hover swatch "
                            f"for `{st}` would render blank")

    def test_every_bucket_is_in_the_legend(self):
        for st in state.PROGRESS_STATES:
            self.assertRegex(HTML, rf'class="key k-{st}"',
                             f"`{st}` tiles would appear with nothing explaining them")

    def test_every_bucket_has_a_word(self):
        words = re.search(r"const WORD = \{([^}]*)\}", JS).group(1)
        for st in state.PROGRESS_STATES:
            self.assertRegex(words, rf"\b{st}\s*:",
                             f"the hover card would print `undefined` for a `{st}` paper")

    def test_the_thresholds_are_not_re_derived_in_the_javascript(self):
        """app.js reads `progress` from status.json; it must not recompute the ladder.

        The one comparison left is the compatibility fallback for a status.json older
        than the script, which is why this counts rather than forbids.
        """
        self.assertLessEqual(
            len(re.findall(r"(?:confirmedReviews\(s\)|completed_reviews)\s*>", JS)), 1,
            "the progress thresholds belong in state.paper_progress(), not in the client")


class TestTheSiteReadsFieldsThatExist(unittest.TestCase):
    """Every `status.json` field app.js reads is one compute_status() actually writes.

    The counters were renamed on 2026-08-18 (`completed_reviews` → `reviews_confirmed` +
    `reviews_graded`, `reviews_in_flight` → `reviews_awaiting_signoff`) when a confirmed
    review became a finished one. A missed rename in the client is silent: JavaScript
    reads an absent key as `undefined`, and the page renders "undefined/3 reviews" or
    quietly drops a tally — no error anywhere, and nothing else cross-checks the two
    files. This is the four-file vocabulary lint above, extended from words to data.
    """

    # Accepted as *fallbacks only*: a visitor's cached app.js and a freshly deployed
    # status.json cross in the air, so each accessor accepts either vintage. Delete
    # these once no pre-rename status.json can still be served.
    LEGACY = {"completed_reviews", "reviews_in_flight", "reviews_completed"}

    def published(self) -> tuple[set, set]:
        """(per-paper keys, totals keys) as compute_status actually emits them."""
        pool = {"EEG-01": {"modality": "EEG", "level": 1}}
        status = state.compute_status(pool, {})
        return set(status["papers"]["EEG-01"]), set(status["totals"])

    def test_every_per_paper_field_is_published(self):
        papers, _ = self.published()
        read = set(re.findall(r"\bs\.([a-z_]+)", JS))
        self.assertEqual(read - papers - self.LEGACY, set(),
                         "app.js reads a per-paper field state.py does not write")

    def test_every_totals_field_is_published(self):
        _, totals = self.published()
        read = set(re.findall(r"\bt\.([a-z_]+)", JS))
        self.assertEqual(read - totals - self.LEGACY, set(),
                         "app.js reads a totals field state.py does not write")

    def test_the_lint_is_reading_something(self):
        """A canary. If `s` or `t` were renamed in app.js the two tests above would
        pass against an empty set and guard nothing."""
        for var in ("s", "t"):
            self.assertGreaterEqual(len(set(re.findall(rf"\b{var}\.([a-z_]+)", JS))), 3,
                                    f"nothing in app.js reads fields off `{var}` any more")


class TestMosaicContrast(unittest.TestCase):
    """The mosaic sits on the navy hero. What is drawn there has to survive it.

    WCAG 1.4.11 asks 3:1 of a graphical object carrying information, and every tile
    carries information. Three of the four clear it outright. The fourth, the completed
    tile, is the brand's dark purple at 1.4:1, and that is a decision rather than an
    oversight: a rim to rescue it makes the tile read as a hollow checkbox, the opposite
    of "finished", and no purple light enough to pass is still distinguishable from the
    lavender next to it. A completed paper is read against its bright neighbours in a
    dense grid, and the text equivalent is the pool table, not the tile.
    """

    MIN = 3.0
    TILES = ("--m-open", "--m-review", "--m-partial", "--m-done")

    def setUp(self):
        self.ground = css_var("--logo-blue")

    def test_a_tile_you_must_be_able_to_spot_alone_clears_the_hero_ground(self):
        """The three that mean "this paper still wants something from you"."""
        for token in ("--m-open", "--m-review", "--m-partial"):
            with self.subTest(token=token):
                self.assertGreaterEqual(contrast(css_var(token), self.ground), self.MIN)

    def test_the_mosaic_has_a_text_equivalent(self):
        """What makes the dark completed tile defensible, so it is asserted, not assumed."""
        self.assertRegex(HTML, r'id="mosaic"[^>]*role="img"[^>]*aria-label=')
        self.assertRegex(JS, r'setAttribute\(\s*"aria-label"',
                         "the tally has to reach the label, or role=img describes nothing")
        self.assertIn('id="poolTable"', HTML)

    def test_no_two_tiles_are_confusable(self):
        """The property luminance contrast cannot express, and the reason it is not used.

        Turquoise and orange are within 1.2:1 of each other and nobody would confuse
        them; lavender and purple share a hue and separate by lightness alone. Only a
        distance carrying both says anything useful. The tightest pair here is
        lavender/purple at dE 34, against a just-noticeable step of ~2.3 — the bar below
        is set to catch a palette collapsing, not to police taste.
        """
        for a, b in itertools.combinations(self.TILES, 2):
            with self.subTest(pair=(a, b)):
                self.assertGreater(delta_e(css_var(a), css_var(b)), 25,
                                   f"{a} and {b} would read as the same tile")

    def test_the_palette_is_the_brand_palette(self):
        """Every mosaic colour resolves into the logo tokens, with no strays."""
        brand = {css_var(f"--logo-{c}")
                 for c in ("orange", "turquoise", "lavender", "purple")}
        for token in self.TILES:
            with self.subTest(token=token):
                self.assertIn(css_var(token), brand)


if __name__ == "__main__":
    unittest.main()
