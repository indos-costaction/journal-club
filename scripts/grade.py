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


def claim_id(paper_id: str, participant: str, issue: int) -> str:
    return f"{paper_id}--{participant}--{issue}"


def grade(issue: int, paper_id: str, axes: dict, grader: str) -> dict:
    claims = state.load_claims()
    claim = claims.get(issue)
    if claim is None:
        raise SystemExit(f"error: no claim issue #{issue}")
    paper_id = paper_id.upper()
    rec = claim["papers"].get(paper_id)
    if rec is None:
        raise SystemExit(f"error: issue #{issue} has no claim on {paper_id}")

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
