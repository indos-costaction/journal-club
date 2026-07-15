#!/usr/bin/env python3
"""Core state engine for the Federated Journal Club.

Git is the database. Three kinds of file hold the truth:

* ``data/pool.json``      — static paper identity (seeded once).
* ``claims/<issue>.json`` — authoritative dynamic truth, one file per claim issue.
* ``ledger/<claim-id>.json`` — authoritative scores, one file per graded review.

Everything the public reads (``data/status.json``, ``data/ranking.json``) is a
*pure recompute* from these. This module is the single place that (a) enforces the
mechanics params and (b) derives ``status.json``. It has no GitHub knowledge — the
workflow entrypoints (``issue_ops.py``, ``sweep.py``, ``grade.py``) call into it.

All functions are deterministic given their inputs and the ``now`` argument, so
they are safe to re-apply on top of freshly pulled state (the apply-intent model).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import params

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# The three web-served JSONs live under docs/data/ so the GitHub Pages "/docs"
# deploy publishes them at the same origin the site fetches from. The authoritative
# per-entity state (claims/, ledger/) stays at repo root — not needed by the site.
DATA_DIR = REPO / "docs" / "data"
POOL_FILE = DATA_DIR / "pool.json"
STATUS_FILE = DATA_DIR / "status.json"
RANKING_FILE = DATA_DIR / "ranking.json"
CLAIMS_DIR = REPO / "claims"
LEDGER_DIR = REPO / "ledger"

# State semantics ------------------------------------------------------------
# in-flight  : occupies a slot, counts as a live claim and against the cap
# done       : a floor-passing completed review, counts toward the 3
# freed      : slot released, contributes nothing
#
# The lifecycle:
#
#     active --/received (organizer)--> pending --/confirm (author)--> submitted --> completed
#
# `pending` = the upload is in our hands but the claimant has not yet signed it off.
# It is IN_FLIGHT (the work is done; don't hand them a 4th paper meanwhile) and it
# **cannot expire** — sweep only touches `active`, so reaching `pending` stops the
# deadline clock. That is deliberate, not incidental: someone who uploads on day 11
# must not be expired on day 12 while waiting on us.
IN_FLIGHT = {"active", "pending", "submitted"}
DONE = {"completed"}
# "recalled": legacy pre-rename data. "rejected": removed by an organizer for a rules
# violation — mechanically identical to withdrawn/returned, but kept distinct because
# for a paper carrying co-authorship, "removed for a violation" and "scored badly" must
# never be the same record.
FREED = {"withdrawn", "recalled", "expired", "returned", "rejected"}
ALL_STATES = IN_FLIGHT | DONE | FREED

# The states in which the ball is in the *participant's* court. This is the concept
# the auto-close predicate needs: a thread closes when nothing here needs them.
# `submitted` is excluded (it's with the organizers for grading); `pending` is included
# (it's waiting on their /confirm) — closing a thread we're about to ask them to reply
# on would strand the handshake.
NEEDS_PARTICIPANT = {"active", "pending"}


# --- time helpers -----------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# --- IO ---------------------------------------------------------------------
def load_pool() -> dict:
    """Return {id: pool_record}."""
    return {p["id"]: p for p in json.loads(POOL_FILE.read_text())}


def load_claims() -> dict:
    """Return {issue_number: claim_dict} for every claims/<issue>.json."""
    out = {}
    for f in sorted(CLAIMS_DIR.glob("*.json")):
        c = json.loads(f.read_text())
        out[c["issue"]] = c
    return out


def load_ledger() -> list[dict]:
    return [json.loads(f.read_text()) for f in sorted(LEDGER_DIR.glob("*.json"))]


def save_claim(claim: dict) -> Path:
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    path = CLAIMS_DIR / f"{claim['issue']}.json"
    path.write_text(json.dumps(claim, indent=2, ensure_ascii=False) + "\n")
    return path


def claim_id(paper_id: str, participant: str, issue: int) -> str:
    """The canonical join key for one review: ledger filename, inbox PDF filename.

    Lives here (not in ``grade.py``) because ``grade.py`` and ``intake.py`` both key
    off it and must never drift apart.
    """
    return f"{paper_id}--{participant}--{issue}"


def new_claim(issue: int, participant: str, attribution: str = "attributed",
              gdpr: bool = False, at: str | None = None) -> dict:
    return {
        "issue": issue,
        "participant": participant,
        "attribution": attribution if attribution in ("attributed", "anonymous") else "attributed",
        "consent": {"gdpr": bool(gdpr), "at": at},
        "papers": {},
    }


# --- derived aggregates (pure) ---------------------------------------------
def _iter_paper_states(claims: dict):
    """Yield (paper_id, participant, state) across all claim files."""
    for claim in claims.values():
        for pid, rec in claim["papers"].items():
            yield pid, claim["participant"], rec["state"]


def live_claimants(claims: dict, paper_id: str) -> set[str]:
    return {p for pid, p, s in _iter_paper_states(claims)
            if pid == paper_id and s in IN_FLIGHT}


def completed_count(claims: dict, paper_id: str) -> int:
    return sum(1 for pid, _p, s in _iter_paper_states(claims)
               if pid == paper_id and s in DONE)


def active_cap_count(claims: dict, participant: str) -> int:
    """How many in-flight papers this participant holds (across all their issues)."""
    return sum(1 for _pid, p, s in _iter_paper_states(claims)
               if p == participant and s in IN_FLIGHT)


def participant_holds(claims: dict, participant: str, paper_id: str) -> bool:
    return any(pid == paper_id and p == participant and s in IN_FLIGHT
               for pid, p, s in _iter_paper_states(claims))


def paper_status(live: int, completed: int) -> str:
    if completed >= params.COMPLETION_THRESHOLD:
        return "done"
    if live >= params.POOL_CLOSE_THRESHOLD:
        return "closed"
    return "open"


def compute_status(pool: dict, claims: dict) -> dict:
    """Derive per-paper + club-level status. Pure function of pool + claims."""
    papers = {}
    total_outstanding = 0
    total_completed = 0
    n_done = n_closed = n_open = 0
    for pid, rec in pool.items():
        live = len(live_claimants(claims, pid))
        completed = completed_count(claims, pid)
        st = paper_status(live, completed)
        need = max(0, params.COMPLETION_THRESHOLD - completed)
        total_outstanding += need
        total_completed += completed
        n_done += st == "done"
        n_closed += st == "closed"
        n_open += st == "open"
        papers[pid] = {
            "modality": rec["modality"],
            "level": rec["level"],
            "live_claims": live,       # "copies" currently drawn from the library
            "completed_reviews": completed,  # reports received
            "status": st,
            "outstanding_need": need,
        }
    return {
        "generated_at": iso(now_utc()),
        "params": {
            "active_claim_cap": params.ACTIVE_CLAIM_CAP,
            "pool_close_threshold": params.POOL_CLOSE_THRESHOLD,
            "completion_threshold": params.COMPLETION_THRESHOLD,
            "deadline_days": params.DEADLINE_DAYS,
        },
        "totals": {
            "papers": len(pool),
            "done": n_done,
            "closed": n_closed,
            "open": n_open,
            "reviews_completed": total_completed,
            "total_outstanding": total_outstanding,  # estimate of remaining review work
        },
        "papers": papers,
    }


def write_status(pool: dict, claims: dict) -> dict:
    status = compute_status(pool, claims)
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n")
    return status


# --- mutations (return per-id outcome lines for the auto-comment) -----------
class Outcome:
    def __init__(self):
        self.ok: list[str] = []
        self.rejected: list[str] = []

    def accept(self, msg): self.ok.append(msg)
    def reject(self, msg): self.rejected.append(msg)

    def delta(self) -> str:
        """Render *what just changed* — not the whole comment.

        ``messages.py`` wraps this with the greeting, the current-holdings table and
        the command reference. Keeping the delta separate from the standing state is
        what lets one code path serve every command.
        """
        lines = []
        if self.ok:
            lines.append("**Accepted:**")
            lines += [f"- ✅ {m}" for m in self.ok]
        if self.rejected:
            lines.append("**Not applied:**")
            lines += [f"- ❌ {m}" for m in self.rejected]
        return "\n".join(lines) if lines else "_No recognised command found._"


def apply_claim(pool: dict, claims: dict, claim: dict, ids: list[str],
                now: datetime) -> Outcome:
    """Claim up to remaining-cap papers into ``claim``. Validates against the
    whole ``claims`` world (cap + close-state), re-derivable on retry."""
    out = Outcome()
    participant = claim["participant"]
    for raw in ids:
        pid = raw.strip().upper()
        if pid not in pool:
            out.reject(f"`{raw}` is not a paper id in the pool.")
            continue
        if participant_holds(claims, participant, pid):
            out.reject(f"`{pid}` — you already hold this paper.")
            continue
        live = len(live_claimants(claims, pid))
        completed = completed_count(claims, pid)
        st = paper_status(live, completed)
        if st == "done":
            out.reject(f"`{pid}` is complete (≥{params.COMPLETION_THRESHOLD} reviews) — pick another.")
            continue
        if st == "closed":
            out.reject(f"`{pid}` is closed ({live} claimants) — pick a paper still open.")
            continue
        if active_cap_count(claims, participant) >= params.ACTIVE_CLAIM_CAP:
            out.reject(f"`{pid}` — you already hold {params.ACTIVE_CLAIM_CAP} active claims; "
                       f"withdraw one first.")
            continue
        due = now + timedelta(days=params.DEADLINE_DAYS)
        claim["papers"][pid] = {
            "state": "active",
            "claimed_at": iso(now),
            "due_at": iso(due),
            "extended": False,
            "submission_ref": None,
            "reminded": [],
        }
        # reflect immediately so the next id in this batch sees the updated world
        claims[claim["issue"]] = claim
        # the running cap and the due date are restated by messages.holdings_table();
        # the delta stays a delta
        out.accept(f"`{pid}` claimed — due **{iso(due)[:10]}**.")
    return out


def apply_withdraw(claim: dict, ids: list[str]) -> Outcome:
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
        if not rec or rec["state"] not in IN_FLIGHT:
            out.reject(f"`{pid}` — you have no active claim on this paper.")
            continue
        rec["state"] = "withdrawn"
        out.accept(f"`{pid}` returned to the pool — no penalty. Slot freed.")
    return out


# A `/received` is an organizer asserting "I have this file in hand", so it is accepted
# from `expired` as well as `active`. The deadline is enforced by a daily cron but
# receipt is detected by a manual step — without this, an organizer running intake a day
# late would expire a punctual upload. LimeSurvey's submitdate is the evidence, and the
# organizer's judgement is the authority. `pending` is accepted too, which is what makes
# a re-upload (and a re-run of the rebase-retry loop) idempotent.
RECEIVABLE = {"active", "expired", "pending"}


def apply_receive(claim: dict, ids: list[str], now: datetime,
                  ref: str | None = None) -> Outcome:
    """Organizer-only: record that an upload is in hand, and ask for a sign-off.

    Driven by ``intake.py`` once the PDF has been retrieved. This does **not** mean the
    review is submitted — the form is open-access and its per-paper link is public, so
    an upload proves only that *someone* had the link. Only the claimant's ``/confirm``
    turns this into a submission.

    ``ref`` is the LimeSurvey response id (the store of record), carried in the comment
    as ``/received <ID> ref:<n>``.
    """
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
        if not rec:
            out.reject(f"`{pid}` — this thread holds no claim on that paper.")
            continue
        if rec["state"] not in RECEIVABLE:
            out.reject(f"`{pid}` — can't record an upload against a `{rec['state']}` claim.")
            continue
        was = rec["state"]
        rec["state"] = "pending"
        # Restamp on a re-upload: the nudge clock should track the newest file, not the
        # first one. `reminded` is reset for the same reason — the earlier nudges were
        # about a file that has now been replaced.
        rec["pending_since"] = iso(now)
        rec["reminded"] = [m for m in rec.get("reminded", []) if not m.startswith("conf")]
        if ref:
            rec["submission_ref"] = ref
        if was == "expired":
            out.accept(f"`{pid}` — upload received after the deadline and **accepted**. "
                       f"Confirm it below and it counts in full.")
        elif was == "pending":
            out.accept(f"`{pid}` — newer upload received; it replaces the earlier one. "
                       f"Still needs your confirmation below.")
        else:
            out.accept(f"`{pid}` — upload received. It needs your confirmation below.")
    return out


def apply_confirm(claim: dict, ids: list[str], now: datetime) -> Outcome:
    """Author-only: the claimant signs off on an upload made in their name.

    This is the whole point of the handshake, and the only command an organizer may not
    proxy (see ``issue_ops.COMMAND_ACL``). GitHub identity is the anchor: it is what
    makes *this* person the author of the review, and — because the form's no-AI radio
    is unauthenticated — the only place the no-AI declaration acquires a signatory.
    """
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
        if not rec:
            out.reject(f"`{pid}` — this thread holds no claim on that paper.")
            continue
        if rec["state"] == "submitted":
            out.accept(f"`{pid}` — already confirmed; it's with the organizers. Nothing to do.")
            continue
        if rec["state"] != "pending":
            out.reject(f"`{pid}` — nothing to confirm (we have no upload for it yet). "
                       f"Upload it first and we'll ask you here.")
            continue
        rec["state"] = "submitted"
        rec["confirmed_at"] = iso(now)
        out.accept(f"`{pid}` confirmed — thank you. It's with the organizers for grading.")
    return out


def apply_reject(claim: dict, ids: list[str], now: datetime, by: str) -> Outcome:
    """Organizer-only: remove a review that broke the rules.

    Works at **any** stage, including after grading — which is when a violation usually
    surfaces. The ledger entry is deliberately left intact (it is the audit trail of
    what was scored and by whom); ``rank.py`` stops counting it because the board
    follows the claim, not the ledger alone.
    """
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
        if not rec:
            out.reject(f"`{pid}` — this thread holds no claim on that paper.")
            continue
        if rec["state"] in FREED:
            out.reject(f"`{pid}` — already `{rec['state']}`; it counts for nothing already.")
            continue
        rec["state"] = "rejected"
        rec["rejected_at"] = iso(now)
        rec["rejected_by"] = by
        out.accept(f"`{pid}` has been withdrawn by the organizers and no longer counts. "
                   f"The paper is back in the pool.")
    return out


def apply_extend(claim: dict, ids: list[str], now: datetime) -> Outcome:
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
        if rec and rec["state"] in ("pending", "submitted"):
            # Not an error worth spending their extension on: the clock already stopped
            # when we received the upload. Say so rather than "no active claim".
            out.reject(f"`{pid}` — no deadline left to extend; we already have your "
                       f"upload and the clock stopped.")
            continue
        if not rec or rec["state"] != "active":
            out.reject(f"`{pid}` — no active claim to extend.")
            continue
        if rec["extended"]:
            out.reject(f"`{pid}` — you have already used your one-time extension. "
                       f"Withdraw it if you cannot finish.")
            continue
        due = parse(rec["due_at"]) + timedelta(days=params.EXTENSION_DAYS)
        rec["due_at"] = iso(due)
        rec["extended"] = True
        rec["reminded"] = []  # nudges re-fire against the new deadline
        out.accept(f"`{pid}` extended to **{iso(due)[:10]}** — this was your one-time "
                   f"+{params.EXTENSION_DAYS}-day extension.")
    return out
