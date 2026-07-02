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
IN_FLIGHT = {"active", "submitted"}
DONE = {"completed"}
FREED = {"withdrawn", "recalled", "expired", "returned"}  # "recalled": legacy pre-rename data
ALL_STATES = IN_FLIGHT | DONE | FREED


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

    def comment(self, participant: str) -> str:
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
        out.accept(f"`{pid}` claimed — due **{iso(due)[:10]}** "
                   f"({active_cap_count(claims, participant)}/{params.ACTIVE_CLAIM_CAP} active).")
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


def apply_submit(claim: dict, ids: list[str]) -> Outcome:
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
        if not rec or rec["state"] != "active":
            out.reject(f"`{pid}` — no active claim to submit (already submitted or returned?).")
            continue
        rec["state"] = "submitted"
        out.accept(f"`{pid}` marked submitted — organizers will verify your upload and grade it.")
    return out


def apply_extend(claim: dict, ids: list[str], now: datetime) -> Outcome:
    out = Outcome()
    for raw in ids:
        pid = raw.strip().upper()
        rec = claim["papers"].get(pid)
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
