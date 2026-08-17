"""Ratifiable parameters for the Federated Journal Club.

Single source of truth for every rule the tracking + grading layer enforces.
These mirror the WG3-ratifiable defaults in
``initiatives/2026-07-federated-journal-club/mechanics.md`` and
``grading-rubric.md``. Change a value here and every script follows.
"""

# --- Federation mechanics (mechanics.md) -----------------------------------
ACTIVE_CLAIM_CAP = 3        # max papers a participant may hold in parallel
POOL_CLOSE_THRESHOLD = 5    # a paper stops accepting claims at this many live claimants
COMPLETION_THRESHOLD = 3    # a paper is "done" at this many floor-passing reviews
DEADLINE_DAYS = 12          # days from claim to due date
EXTENSION_DAYS = 7          # one-time extension length
REMIND_BEFORE_DAYS = (3, 1)  # nudge when due is this many days away (day 9, day 11)

# Grace applied when deciding whether an upload actually missed its deadline
# (intake.upload_was_late). LimeSurvey stamps `submitdate` in the survey's own
# timezone and the export does not say which, while `due_at` is UTC — so a naive
# comparison carries a couple of hours of slack in an unknown direction. Rather than
# hardcode an offset that a DST change or a server move would silently invalidate,
# absorb the whole uncertainty. The bias is deliberate: missing a deadline here costs
# someone their slot, no penalty attaches to being an hour over, and an organizer sees
# every case anyway.
SUBMITDATE_GRACE_HOURS = 3

# Days after an upload is received to nudge a participant who hasn't confirmed it yet.
# There is no auto-expiry for a pending paper: the deadline clock stops at `pending`
# (sweep only expires `active`), and the file already exists — binning someone's
# finished work for missing a notification would be the worst outcome in the system.
# The incentive is aligned anyway: confirming is what earns the points and frees the
# slot. A genuinely abandoned upload is an organizer's `/reject`, not a timer's.
CONFIRM_NUDGE_DAYS = (3, 7)

# --- Submission intake (submission-form.md) --------------------------------
SITE_URL = "https://indos-costaction.github.io/journal-club/"

# The LimeSurvey "Submit a review" survey. messages.upload_url() appends the join key
# (issue/paper/gh) as query params, so it correctly extends the existing `?lang=en`.
# `lang=en` is deliberate: the instance's default UI language is French.
# Empty is a safe state, not a broken one — messages.py renders an honest "form coming"
# line rather than a dead link. Build spec + runbook: ../../submission-form.md
SUBMISSION_FORM_URL = "https://limesurvey.hes-so.ch/index.php/428628?lang=en"

# --- Grading rubric (grading-rubric.md) ------------------------------------
# Five axes, each scored 0-5, combined with these weights (sum == 1.0).
RUBRIC_WEIGHTS = {
    "engagement": 0.15,     # annotation count + spread across the paper
    "comprehension": 0.20,  # located "what I didn't understand" signals
    "critical": 0.30,       # contesting claims, post-publication critique
    "action": 0.20,         # data-sharing / standardization / reproducibility angle
    "originality": 0.15,    # personal voice / non-AI signal
}
QUALITY_FLOOR = 2.0         # min weighted score (0-5) to earn points and count as complete

# Optional under-served-modality bonus (WG3-toggleable). When enabled, a review
# on a paper still far from COMPLETION_THRESHOLD earns a small increment.
UNDERSERVED_BONUS_ENABLED = False
UNDERSERVED_BONUS = 0.10

# --- Modality → ID prefix ---------------------------------------------------
# Stable, human-readable id prefixes. IDs are FROZEN once published; a later
# backfill toward the ~270 target appends new ids, it never re-sorts existing ones.
MODALITY_PREFIX = {
    "EEG": "EEG",
    "MEG": "MEG",
    "fMRI": "FMRI",
    "dMRI": "DMRI",
    "anatMRI": "ANAT",
    "PET": "PET",
    "SPECT": "SPECT",
    "CT": "CT",
    "fNIRS": "FNIRS",
    # 10th category: modality-agnostic methods (registration, segmentation, denoising,
    # tooling) that apply across modalities. Directly populated — no seed reviews.
    "Cross-modality": "CROSS",
}

# Canonical modality display order (for the pool browser + docs).
MODALITY_ORDER = ["EEG", "MEG", "fMRI", "dMRI", "anatMRI", "PET", "SPECT", "CT", "fNIRS",
                  "Cross-modality"]

# Categories with no level-0 seed reviews — pooled flat from their members.
NO_SEED_MODALITIES = {"Cross-modality"}

# --- Pool scope: what may be a pool entry ----------------------------------
# A pool entry must be a *paper-shaped* unit: one argument a trainee can read and
# annotate in a sitting, and that three reviewers can score against the same rubric.
#
# **Books, textbooks and handbooks are out of scope.** Not a quality judgement —
# they are excellent, and several are field-defining. They are the wrong *unit*:
# a 400-page textbook has no single claim to contest, cannot be annotated end to
# end against a 12-day clock, and does not fit any upload ceiling. Asking someone
# to "review" one is asking for work the rubric cannot read.
#
# The pool is harvested by ranking cited works by citation count (seed_pool.py),
# and textbooks are cited enormously, so they float to the top of every modality
# unless something stops them. This is that something.
#
# Machine signature: Semantic Scholar carries books as MAG-derived records with
# **neither a DOI nor a venue**. Real pre-DOI journal papers keep their venue
# (e.g. the Journal of Nuclear Medicine entries in this pool), so requiring
# "a DOI or a venue" separates the two without a title heuristic. It also catches
# the *phantom* records that same ingest produces: a real book's title attached to
# a wrong author and year, with a scraped junk abstract.
#
# Enforced at both ends: seed_pool.in_scope() at build time, and
# tests/test_pool.py against the published pool.json.
def in_scope(item: dict) -> tuple[bool, str]:
    """(ok, reason). ``reason`` is empty when the item is in scope."""
    has_doi = bool((item.get("doi") or "").strip())
    has_venue = bool((item.get("venue") or "").strip())
    if not has_doi and not has_venue:
        return False, "no DOI and no venue — book/phantom signature"
    return True, ""


# Pool ids withdrawn after publication. An id is a permanent handle: it appears in
# claim records, issue threads and the ledger, so a retired id is **never reused**
# and never renumbered onto another paper. seed_pool.py assigns ids over the full
# ranked list and drops these afterwards, which is what keeps every surviving id
# stable; the gaps are the record that something was withdrawn.
RETIRED = {
    "EEG-03": "phantom duplicate of EEG-07 — Luck's ERP textbook re-ingested under a "
              "wrong author (M. Schmid, 2016) with a scraped junk abstract",
    "EEG-07": "textbook — out of scope (Luck, An Introduction to the ERP Technique)",
    "EEG-08": "textbook — out of scope (Cohen, Analyzing Neural Time Series Data)",
    "EEG-17": "handbook — out of scope, and misattributed (the Oxford Handbook of ERP "
              "Components is Luck & Kappenman 2011, not W. Ziegler 2016)",
    "EEG-04": "textbook — out of scope (Nunez, Electric Fields of the Brain). Entered "
              "through a *review of* the book: the DOI is Fermaglich's one-page notice "
              "in JAMA 247:1879, carrying the book's 2 760 citations. in_scope() cannot "
              "see this — the record has a real DOI and a real venue",
    "CROSS-25": "reference-work entry — out of scope (\"Image Segmentation\", Encyclopedia "
                "of Database Systems). Same unit problem as a textbook: a survey entry "
                "states consensus rather than making a claim a reviewer can contest",
}

# in_scope() catches records with no bibliographic identity at all. It does NOT catch a
# book that entered through a *review of* the book (EEG-04), an encyclopedia entry, or a
# textbook that happens to carry a chapter DOI — those look like ordinary articles.
# Tried and rejected as a screen: "single page + high citation count". Journals that
# number articles rather than paginate (J. Neural Eng., Sensors, Cancers) report one
# page, so it flags a dozen legitimate reviews — including EEG-22, an actual submission.
# Treat a new generation's book screening as a human pass over the ranked list, not a
# solved problem.


def weighted_score(axes):
    """Combine per-axis 0-5 scores into the weighted 0-5 score."""
    return round(sum(RUBRIC_WEIGHTS[k] * float(axes[k]) for k in RUBRIC_WEIGHTS), 4)
