#!/usr/bin/env python3
"""Recompute ``data/ranking.json`` — a pure function of the ledger + claims.

    participant_points = Σ over floor-passing completed reviews ( weighted )

Only floor-passing reviews **whose claim is still `completed`** contribute; withdrawals,
expiries, below-floor reviews and organizer-rejected ones count zero. Tie-breakers, in
order: (1) number of completed reviews, (2) mean weighted score, (3) earliest to reach
the current point total. Because the whole board is recomputed from ledger + claims every
time, re-running never double-counts, and a corrected ledger entry or a rejected claim
simply yields the corrected board.
"""
from __future__ import annotations

import hashlib
import json

import params
import state


def _pseudonym(handle: str) -> str:
    h = hashlib.sha1(handle.encode()).hexdigest()[:6]
    return f"Reviewer-{h}"


def _attribution_map(claims: dict) -> dict:
    out = {}
    for c in claims.values():
        out.setdefault(c["participant"], c.get("attribution", "attributed"))
    return out


def compute_ranking(claims: dict, ledger: list[dict], status: dict | None = None) -> dict:
    attribution = _attribution_map(claims)

    # optional under-served-modality bonus: reward reviews on papers still short of
    # the completion threshold (steer effort where the pool needs it).
    underserved = set()
    if params.UNDERSERVED_BONUS_ENABLED and status:
        underserved = {pid for pid, s in status["papers"].items()
                       if s["completed_reviews"] < params.COMPLETION_THRESHOLD}

    agg: dict[str, dict] = {}
    for e in ledger:
        if not e.get("floor_ok"):
            continue
        # The board follows the *claim*, not the ledger alone. A ledger entry records that
        # a review was scored; the claim records whether it still stands. Without this, an
        # organizer's /reject would remove a review everywhere except the one place that
        # matters — the entry would sit here with floor_ok and keep paying out.
        #
        # A no-op on data written before /reject existed: grade.py sets `completed` iff
        # floor_ok, so the two conditions were equivalent and only diverge once a review
        # is rejected after grading.
        rec = (claims.get(e["issue"], {}).get("papers", {}) or {}).get(e["paper_id"])
        if not rec or rec["state"] != "completed":
            continue
        p = e["participant"]
        a = agg.setdefault(p, {"points": 0.0, "reviews": 0, "last": ""})
        pts = float(e["weighted"])
        if e["paper_id"] in underserved:
            pts += params.UNDERSERVED_BONUS
        a["points"] += pts
        a["reviews"] += 1
        a["last"] = max(a["last"], e.get("graded_at", ""))

    rows = []
    for p, a in agg.items():
        reviews = a["reviews"]
        points = round(a["points"], 4)
        rows.append({
            "participant": p,
            "attribution": attribution.get(p, "attributed"),
            "display": p if attribution.get(p, "attributed") == "attributed" else _pseudonym(p),
            "points": points,
            "reviews": reviews,
            "mean": round(points / reviews, 4) if reviews else 0.0,
            "reached_total_at": a["last"],
        })

    # sort: points desc, then #reviews desc, then mean desc, then earliest-to-total asc
    rows.sort(key=lambda r: (-r["points"], -r["reviews"], -r["mean"], r["reached_total_at"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    return {
        "generated_at": state.iso(state.now_utc()),
        "snapshot": None,
        "floor": params.QUALITY_FLOOR,
        "underserved_bonus": params.UNDERSERVED_BONUS_ENABLED,
        "participants": rows,
    }


def write_ranking(claims: dict, ledger: list[dict], status: dict | None = None) -> dict:
    ranking = compute_ranking(claims, ledger, status)
    state.RANKING_FILE.parent.mkdir(parents=True, exist_ok=True)
    state.RANKING_FILE.write_text(json.dumps(ranking, indent=2, ensure_ascii=False) + "\n")
    return ranking


if __name__ == "__main__":
    claims = state.load_claims()
    status = state.write_status(state.load_pool(), claims)
    r = write_ranking(claims, state.load_ledger(), status)
    print(f"ranking: {len(r['participants'])} participants")
