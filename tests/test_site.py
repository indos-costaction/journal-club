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
        self.assertEqual(state.paper_progress(live=0, completed=0), "open")

    def test_a_single_claimant_is_already_in_review(self):
        """The whole reason progress is not `status`: `status` would still say open."""
        self.assertEqual(state.paper_progress(live=1, completed=0), "review")
        self.assertEqual(state.paper_status(live=1, completed=0), "open")

    def test_one_graded_review_is_partial(self):
        self.assertEqual(state.paper_progress(live=0, completed=1), "partial")

    def test_one_short_of_the_threshold_is_still_partial(self):
        self.assertEqual(
            state.paper_progress(live=0, completed=params.COMPLETION_THRESHOLD - 1),
            "partial")

    def test_the_threshold_is_done(self):
        self.assertEqual(
            state.paper_progress(live=0, completed=params.COMPLETION_THRESHOLD), "done")

    def test_a_late_extra_claim_does_not_un_finish_a_paper(self):
        self.assertEqual(
            state.paper_progress(live=1, completed=params.COMPLETION_THRESHOLD), "done")

    def test_closed_to_claims_is_not_a_progress_state(self):
        """Five claimants and nothing graded: unclaimable, and visibly not started."""
        live = params.POOL_CLOSE_THRESHOLD
        self.assertEqual(state.paper_status(live, 0), "closed")
        self.assertEqual(state.paper_progress(live, 0), "review")

    def test_withdrawing_the_only_claim_returns_the_tile_to_open(self):
        self.assertEqual(self.paper("active")["progress"], "review")
        self.assertEqual(self.paper("withdrawn")["progress"], "open")

    def test_every_reachable_bucket_is_declared(self):
        """PROGRESS_STATES is what the site lint below enumerates; keep it honest."""
        reachable = {state.paper_progress(live, done)
                     for live in range(0, params.POOL_CLOSE_THRESHOLD + 1)
                     for done in range(0, params.COMPLETION_THRESHOLD + 1)}
        self.assertEqual(reachable, set(state.PROGRESS_STATES))


class TestInFlightCounter(StatusBase):
    """`reviews_in_flight`: uploads in hand, no grade yet."""

    def test_it_counts_uploaded_and_signed_off_work(self):
        self.assertEqual(self.paper("pending", "submitted")["reviews_in_flight"], 2)

    def test_a_claim_with_no_upload_is_not_in_flight(self):
        self.assertEqual(self.paper("active")["reviews_in_flight"], 0)

    def test_a_graded_review_has_landed_and_is_no_longer_in_flight(self):
        p = self.paper("completed")
        self.assertEqual(p["reviews_in_flight"], 0)
        self.assertEqual(p["completed_reviews"], 1)

    def test_freed_claims_count_for_nothing(self):
        for st in sorted(state.FREED):
            self.assertEqual(self.paper(st)["reviews_in_flight"], 0, st)

    def test_it_is_a_subset_of_live_claims_not_an_addition_to_them(self):
        """Both numbers are published; they overlap, and the site says so in words."""
        p = self.paper("active", "pending", "submitted")
        self.assertEqual(p["live_claims"], 3)
        self.assertEqual(p["reviews_in_flight"], 2)


class TestOutstandingNeedIsUnchanged(StatusBase):
    """The public headline number counts graded reviews, and only those.

    `total_outstanding` is on the landing page ("N reviews still needed") and
    `outstanding_need` drives the "still needs reviews" filter. Netting work-in-flight
    off either one would quietly shrink the ask every time somebody uploaded a file.
    """

    def test_work_in_flight_does_not_reduce_the_need(self):
        need = params.COMPLETION_THRESHOLD
        self.assertEqual(self.paper()["outstanding_need"], need)
        self.assertEqual(self.paper("pending", "submitted")["outstanding_need"], need)

    def test_only_a_grade_reduces_it(self):
        self.assertEqual(self.paper("completed")["outstanding_need"],
                         params.COMPLETION_THRESHOLD - 1)

    def test_it_never_goes_negative(self):
        over = ["completed"] * (params.COMPLETION_THRESHOLD + 2)
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
            len(re.findall(r"completed_reviews\s*>", JS)), 1,
            "the progress thresholds belong in state.paper_progress(), not in the client")


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
