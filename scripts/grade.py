#!/usr/bin/env python3
"""Enter a graded review into the ledger and refresh the derived aggregates.

Called by ``grade.yml`` (organizer-only ``workflow_dispatch``) or run locally.
The five per-axis 0-5 scores are the ONLY thing that crosses from the private
grading workspace into the public repo — never the PDF, never an email.

    python scripts/grade.py --issue 42 --paper EEG-03 \
        --engagement 4 --comprehension 3 --critical 5 --action 4 --originality 4 \
        --grader org-bob

Effect: writes ``ledger/<claim-id>.json`` (weighted + floor_ok), flips the claim
``submitted → completed`` (floor passed) or ``submitted → returned`` (below floor),
then regenerates ``status.json`` + ``ranking.json``.
"""
from __future__ import annotations

import argparse
import json

import params
import rank
import state


# The join key lives in state.py so grade.py and intake.py cannot drift apart —
# it is both this ledger's filename and the inbox PDF's filename.
claim_id = state.claim_id


def grade(issue: int, paper_id: str, axes: dict, grader: str) -> dict:
    claims = state.load_claims()
    claim = claims.get(issue)
    if claim is None:
        raise SystemExit(f"error: no claim issue #{issue}")
    paper_id = paper_id.upper()
    rec = claim["papers"].get(paper_id)
    if rec is None:
        raise SystemExit(f"error: issue #{issue} has no claim on {paper_id}")
    if rec["state"] != "submitted":
        # Fatal, and the one guard that gives the handshake teeth. A `pending` review has
        # an upload but no confirmed author — the form is public, so grading it would put
        # points on the board for a review nobody has actually claimed authorship of.
        # (`completed`/`returned` = already graded; the rest were never submitted.)
        extra = ("  It's uploaded but not yet confirmed by the claimant — that's the one "
                 "thing that has to happen first.\n" if rec["state"] == "pending" else "")
        raise SystemExit(f"error: {paper_id} (issue #{issue}) is `{rec['state']}`, not "
                         f"`submitted` — refusing to grade it.\n{extra}")
    if not rec.get("submission_ref"):
        # not fatal: a review may predate the LimeSurvey pathway, or have been recorded by
        # hand. But grading something with no recorded upload is worth a second look
        # before it lands in the public ledger.
        print(f"warning: {paper_id} (issue #{issue}) has no submission_ref — "
              f"grading a review with no recorded upload")

    weighted = params.weighted_score(axes)
    floor_ok = weighted >= params.QUALITY_FLOOR
    cid = claim_id(paper_id, claim["participant"], issue)
    entry = {
        "claim_id": cid,
        "paper_id": paper_id,
        "participant": claim["participant"],
        "issue": issue,
        "axes": {k: int(axes[k]) for k in params.RUBRIC_WEIGHTS},
        "weighted": weighted,
        "floor_ok": floor_ok,
        "graded_at": state.iso(state.now_utc()),
        "grader": grader,
    }
    state.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    (state.LEDGER_DIR / f"{cid}.json").write_text(
        json.dumps(entry, indent=2, ensure_ascii=False) + "\n")

    # flip claim state, persist, and refresh the derived aggregates
    rec["state"] = "completed" if floor_ok else "returned"
    state.save_claim(claim)

    claims = state.load_claims()
    status = state.write_status(state.load_pool(), claims)
    rank.write_ranking(claims, state.load_ledger(), status)
    return entry


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--paper", required=True)
    for axis in params.RUBRIC_WEIGHTS:
        ap.add_argument(f"--{axis}", type=int, required=True, choices=range(0, 6))
    ap.add_argument("--grader", required=True)
    args = ap.parse_args()

    axes = {k: getattr(args, k) for k in params.RUBRIC_WEIGHTS}
    entry = grade(args.issue, args.paper, axes, args.grader)
    verdict = "PASS (counts)" if entry["floor_ok"] else f"below floor {params.QUALITY_FLOOR} (returned)"
    print(f"{entry['claim_id']}: weighted={entry['weighted']} → {verdict}")


if __name__ == "__main__":
    main()
