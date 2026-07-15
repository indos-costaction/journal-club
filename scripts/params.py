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


def weighted_score(axes):
    """Combine per-axis 0-5 scores into the weighted 0-5 score."""
    return round(sum(RUBRIC_WEIGHTS[k] * float(axes[k]) for k in RUBRIC_WEIGHTS), 4)
