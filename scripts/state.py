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
import prose

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
# in-flight : the paper is still out — being read, or waiting on a sign-off. Occupies a
#             slot against the cap and counts as a live claimant.
# delivered : a finished review. Counts toward the paper's three.
# done      : delivered AND graded above the floor. Counts for points, and only points.
# freed     : slot released, contributes nothing.
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
IN_FLIGHT = {"active", "pending"}
# **A review is finished when its author signs it off.** Grading is a separate, later,
# organizer-side question about quality and points, and it used to gate everything: the
# board moved only on a grade, so on 2026-08-18 six delivered reviews and an empty ledger
# rendered as "0 of 717 reviews in" with all 239 tiles untouched. `submitted` therefore
# leaves IN_FLIGHT — the copy has come back, and the slot with it — and joins `completed`
# here, where both ladders and the outstanding need read it.
DELIVERED = {"submitted", "completed"}
DONE = {"completed"}
# Uploaded, not yet signed off: the ball is in the claimant's court. Reported beside the
# delivered count so a paper with a file sitting on it does not read as untouched, and
# never folded into it — an upload proves only that someone had the (public) link.
AWAITING_SIGNOFF = {"pending"}
# "recalled": legacy pre-rename data. "rejected": removed by an organizer for a rules
# violation — mechanically identical to withdrawn/returned, but kept distinct because
# for a paper carrying co-authorship, "removed for a violation" and "scored badly" must
# never be the same record. `returned` (graded below the floor) stays here deliberately:
# the floor is a "did you actually annotate it" bar, so a review that fails it is one the
# paper should not be counting, and the tile going back from `done` to `partial` is the
# correct answer in the only case that can trigger it.
FREED = {"withdrawn", "recalled", "expired", "returned", "rejected"}
ALL_STATES = IN_FLIGHT | DELIVERED | FREED

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


def delivered_count(claims: dict, paper_id: str) -> int:
    """Finished reviews for this paper: signed off by their author, graded or not.

    The input to **both** ladders and to the outstanding need — the number that means
    "how many of the three are in".
    """
    return sum(1 for pid, _p, s in _iter_paper_states(claims)
               if pid == paper_id and s in DELIVERED)


def completed_count(claims: dict, paper_id: str) -> int:
    """Delivered reviews that have also been graded above the floor.

    Drives points (``rank.py``) and is reported on the site; it moves neither ladder.
    """
    return sum(1 for pid, _p, s in _iter_paper_states(claims)
               if pid == paper_id and s in DONE)


def awaiting_signoff_count(claims: dict, paper_id: str) -> int:
    """Uploads received for this paper that their claimant has not confirmed yet."""
    return sum(1 for pid, _p, s in _iter_paper_states(claims)
               if pid == paper_id and s in AWAITING_SIGNOFF)


def active_cap_count(claims: dict, participant: str) -> int:
    """How many in-flight papers this participant holds (across all their issues)."""
    return sum(1 for _pid, p, s in _iter_paper_states(claims)
               if p == participant and s in IN_FLIGHT)


def participant_holds(claims: dict, participant: str, paper_id: str) -> bool:
    """Has this person got this paper — still out, or already reviewed?

    Spans DELIVERED as well as IN_FLIGHT, and that is the whole point of it. Since a
    confirmed review frees the slot, ``IN_FLIGHT`` alone would let someone re-claim a
    paper they have already reviewed and deliver a second review that counts toward the
    same three. The claim-side guard is here rather than in ``apply_claim`` so every
    caller inherits it.
    """
    return any(pid == paper_id and p == participant and s in (IN_FLIGHT | DELIVERED)
               for pid, p, s in _iter_paper_states(claims))


def paper_status(live: int, delivered: int) -> str:
    if delivered >= params.COMPLETION_THRESHOLD:
        return "done"
    if live >= params.POOL_CLOSE_THRESHOLD:
        return "closed"
    return "open"


# The four buckets the hero mosaic paints. These deliberately do NOT match
# paper_status() above, which answers "can I still claim it?" and stays "open" until
# five people hold it. This answers "how far along is it?", so a paper with one
# claimant must not look untouched, and a paper one review short of complete must not
# look like one nobody has opened. Both are needed; neither derives from the other.
#
# Both now read the *delivered* count. Grading moves neither, so a tile can never be
# ahead of or behind the number of reviews the paper actually has.
PROGRESS_STATES = ("open", "review", "partial", "done")


def paper_progress(live: int, delivered: int) -> str:
    if delivered >= params.COMPLETION_THRESHOLD:
        return "done"
    if delivered > 0:
        return "partial"
    return "review" if live > 0 else "open"


def compute_status(pool: dict, claims: dict) -> dict:
    """Derive per-paper + club-level status. Pure function of pool + claims."""
    papers = {}
    total_outstanding = 0
    total_confirmed = total_graded = 0
    n_done = n_closed = n_open = 0
    for pid, rec in pool.items():
        live = len(live_claimants(claims, pid))
        delivered = delivered_count(claims, pid)
        graded = completed_count(claims, pid)
        st = paper_status(live, delivered)
        need = max(0, params.COMPLETION_THRESHOLD - delivered)
        # `need` counts finished (confirmed) reviews, and only those: it is the public
        # headline number and the site's "still needs reviews" filter. An upload nobody
        # has signed off is reported beside it, never subtracted from it — the form is
        # open-access, so a file on its own does not yet make a review.
        total_outstanding += need
        total_confirmed += delivered
        total_graded += graded
        n_done += st == "done"
        n_closed += st == "closed"
        n_open += st == "open"
        papers[pid] = {
            "modality": rec["modality"],
            "level": rec["level"],
            "live_claims": live,       # "copies" currently drawn from the library
            "reviews_confirmed": delivered,       # finished: signed off by their author
            "reviews_graded": graded,             # ...and also scored
            "reviews_awaiting_signoff": awaiting_signoff_count(claims, pid),
            "status": st,              # claimability
            "progress": paper_progress(live, delivered),  # how far along (mosaic)
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
            "reviews_confirmed": total_confirmed,   # the headline "reviews in"
            "reviews_graded": total_graded,         # the grading backlog's other end
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
            lines.append(prose.t("delta.accepted_heading"))
            # No per-item marker: every bullet under "Accepted:" is accepted, so a tick
            # on each one repeats the heading once per line.
            lines += [f"- {m}" for m in self.ok]
        if self.rejected:
            lines.append(prose.t("delta.rejected_heading"))
            lines += [f"- {m}" for m in self.rejected]
        return "\n".join(lines) if lines else prose.t("delta.nothing")


def apply_claim(pool: dict, claims: dict, claim: dict, ids: list[str],
                now: datetime) -> Outcome:
    """Claim up to remaining-cap papers into ``claim``. Validates against the
    whole ``claims`` world (cap + close-state), re-derivable on retry."""
    out = Outcome()
    participant = claim["participant"]
    for raw in ids:
        pid = raw.strip().upper()
        if pid not in pool:
            out.reject(prose.t("claim.not_in_pool", raw=raw))
            continue
        if participant_holds(claims, participant, pid):
            out.reject(prose.t("claim.already_held", pid=pid))
            continue
        live = len(live_claimants(claims, pid))
        st = paper_status(live, delivered_count(claims, pid))
        if st == "done":
            out.reject(prose.t("claim.paper_done", pid=pid))
            continue
        if st == "closed":
            out.reject(prose.t("claim.paper_closed", pid=pid, live=live))
            continue
        if active_cap_count(claims, participant) >= params.ACTIVE_CLAIM_CAP:
            out.reject(prose.t("claim.at_cap", pid=pid))
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
        out.accept(prose.t("claim.ok", pid=pid, due=iso(due)[:10]))
    return out


def apply_withdraw(claim: dict, ids: list[str]) -> Outcome:
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
        if rec and rec["state"] in DELIVERED:
            # There is nothing left to give back: the review is in, and it counts. Said
            # separately because "you have no active claim on this paper" would be a
            # baffling answer to someone whose review the site is currently displaying,
            # and because pulling a delivered review is an organizer's `/reject`.
            out.reject(prose.t("withdraw.already_confirmed", pid=pid))
            continue
        if not rec or rec["state"] not in IN_FLIGHT:
            out.reject(prose.t("withdraw.no_claim", pid=pid))
            continue
        rec["state"] = "withdrawn"
        out.accept(prose.t("withdraw.ok", pid=pid))
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
            out.reject(prose.t("receive.no_such_paper", pid=pid))
            continue
        if rec["state"] not in RECEIVABLE:
            out.reject(prose.t("receive.wrong_state", pid=pid, st=rec["state"]))
            continue
        if ref and ref in declined_refs(rec):
            # The claimant refused this exact file. `intake.reconcile` keeps it out of
            # `to_post`, so reaching here means a hand-typed /received — the one path
            # that would otherwise walk a disavowal back and ask them to confirm it again.
            out.reject(prose.t("receive.was_declined", pid=pid, ref=ref))
            continue
        if rec["state"] == "pending" and ref and rec.get("submission_ref") == ref:
            # Same LimeSurvey response id = the same file seen twice (a workflow re-run,
            # a redelivered webhook, an organizer pasting the command again). Restamping
            # would move the confirm-nudge clock and tell the claimant a newer upload had
            # replaced theirs, neither of which is true. `newtest=Y` on the upload link
            # (messages.upload_url) mints a fresh response id per submission, so an equal
            # ref cannot be a genuine re-upload. With no ref we cannot tell, so we fall
            # through and restamp.
            out.accept(prose.t("receive.same_ref", pid=pid, ref=ref))
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
            # Does NOT say "after the deadline". `expired` means the sweep ran before
            # an organizer got round to intake — retrieval is manual, expiry is a cron
            # — so it is evidence about our latency, not theirs. All five uploads in the
            # 2026-08 backlog were inside their deadlines and this line would have told
            # every one of them otherwise. Whether someone was actually late is
            # intake.upload_was_late's question, and it needs the submitdate to answer.
            out.accept(prose.t("receive.from_expired", pid=pid))
        elif was == "pending":
            out.accept(prose.t("receive.replacing", pid=pid))
        else:
            out.accept(prose.t("receive.ok", pid=pid))
    return out


def apply_confirm(claim: dict, ids: list[str], now: datetime) -> Outcome:
    """Author-only: the claimant signs off on an upload made in their name.

    This is the whole point of the handshake, and the only command an organizer may not
    proxy (see ``issue_ops.COMMAND_ACL``). GitHub identity is the anchor: it is what
    makes *this* person the author of the review, and — because the form's no-AI radio
    is unauthenticated — the only place the no-AI declaration acquires a signatory.

    It is also the moment the review **finishes**: `submitted` is DELIVERED, so the paper
    counts one of its three from here, and the slot is freed. Nothing downstream can hand
    the paper back to the claimant — a below-floor grade or an organizer's `/reject` both
    free it — so holding the slot open for grading only ever penalised the person who
    delivered first.
    """
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
        if not rec:
            out.reject(prose.t("receive.no_such_paper", pid=pid))
            continue
        if rec["state"] == "submitted":
            out.accept(prose.t("confirm.already", pid=pid))
            continue
        if rec["state"] != "pending":
            out.reject(prose.t("confirm.nothing_to_confirm", pid=pid))
            continue
        rec["state"] = "submitted"
        rec["confirmed_at"] = iso(now)
        out.accept(prose.t("confirm.ok", pid=pid))
    return out


def declined_refs(rec: dict) -> set[str]:
    """Every LimeSurvey response id the claimant has refused for this paper.

    Read by ``apply_receive`` and by ``intake.reconcile``. Both need it: without the
    second, the next routine ``intake.py post`` re-posts `/received` for the very file
    that was refused, and without the first a hand-typed `/received` does the same.
    """
    return {d["ref"] for d in rec.get("declined", []) if d.get("ref")}


def apply_decline(claim: dict, ids: list[str], now: datetime, by: str,
                  reason_given: bool = False) -> Outcome:
    """The claimant refuses to sign off an upload — the other answer to `/confirm`.

    Two shapes, deliberately served by one command because the mechanics are identical
    and only the prose differs: *"that wasn't me"* (the upload form is open-access and
    its link is public, so someone else's file can land against your claim) and *"that
    was me, but I want to replace it"*.

    The paper goes back to **`active`**, not to a state of its own. `active` keeps the
    slot, keeps the row in the holdings table, and keeps the thread open — a new state
    outside IN_FLIGHT would silently do the opposite of all three, and the claimant has
    not given the paper up.

    The deadline is restored to what was still on the clock when the upload arrived
    (see ``params.DECLINE_FLOOR_DAYS``). Without that, a claim declined after its
    original due date is back at `active` with a past deadline and the next 06:00 sweep
    expires it the same morning — with no warning, because the pre-deadline nudges have
    already fired.

    The **reason text is not stored here.** Only whether one was given. It lives in the
    participant's own public comment, which they can edit or delete; copying it into a
    committed JSON turns a retraction into a git-history rewrite.
    """
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
        if not rec:
            out.reject(prose.t("receive.no_such_paper", pid=pid))
            continue
        ref = rec.get("submission_ref")
        if rec["state"] == "active" and ref is None and rec.get("declined"):
            # Already declined. Accept as a no-op rather than reject: the rebase-retry
            # loop re-runs this script, and a second stamp would make the claim file
            # differ on a re-apply. Same shape as apply_confirm's already-`submitted`.
            out.accept(prose.t("decline.already", pid=pid))
            continue
        if rec["state"] == "submitted":
            out.reject(prose.t("decline.already_confirmed", pid=pid))
            continue
        if rec["state"] != "pending":
            out.reject(prose.t("decline.nothing_to_decline", pid=pid))
            continue

        rec["declined"] = rec.get("declined", []) + [
            {"ref": ref, "at": iso(now), "by": by, "reason_given": bool(reason_given)}]
        rec["state"] = "active"
        rec["submission_ref"] = None
        rec["due_at"] = iso(_restored_due(rec, now))
        # the old deadline's nudges already fired; re-arm them against the new one
        rec["reminded"] = []
        rec.pop("pending_since", None)
        out.accept(prose.t("decline.ok", pid=pid, due=rec["due_at"][:10]))
    return out


def _restored_due(rec: dict, now: datetime) -> datetime:
    """Give back the time the clock was stopped at, never less than the floor."""
    floor = now + timedelta(days=params.DECLINE_FLOOR_DAYS)
    since, due = rec.get("pending_since"), rec.get("due_at")
    if not since or not due:
        return floor
    return max(floor, now + (parse(due) - parse(since)))


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
            out.reject(prose.t("receive.no_such_paper", pid=pid))
            continue
        if rec["state"] in FREED:
            out.reject(prose.t("reject.already_freed", pid=pid, st=rec["state"]))
            continue
        rec["state"] = "rejected"
        rec["rejected_at"] = iso(now)
        rec["rejected_by"] = by
        out.accept(prose.t("reject.ok", pid=pid))
    return out


def apply_extend(claim: dict, ids: list[str], now: datetime) -> Outcome:
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
        if rec and rec["state"] in ("pending", "submitted"):
            # Not an error worth spending their extension on: the clock already stopped
            # when we received the upload. Say so rather than "no active claim".
            out.reject(prose.t("extend.clock_stopped", pid=pid))
            continue
        if not rec or rec["state"] != "active":
            out.reject(prose.t("extend.no_active_claim", pid=pid))
            continue
        if rec["extended"]:
            out.reject(prose.t("extend.already_used", pid=pid))
            continue
        due = parse(rec["due_at"]) + timedelta(days=params.EXTENSION_DAYS)
        rec["due_at"] = iso(due)
        rec["extended"] = True
        rec["reminded"] = []  # nudges re-fire against the new deadline
        out.accept(prose.t("extend.ok", pid=pid, due=iso(due)[:10]))
    return out
