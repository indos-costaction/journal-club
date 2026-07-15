#!/usr/bin/env python3
"""Issue-ops dispatcher — the single entrypoint ``issue-ops.yml`` runs.

Parses a GitHub ``issues`` (claim form) or ``issue_comment`` (`/claim` `/withdraw`
`/submit` `/extend`) event, validates + applies it against freshly-loaded state,
and writes:

* the updated ``claims/<issue>.json`` + regenerated ``data/status.json``;
* ``comment.md``   — the reply body the workflow posts (GitHub then emails the author);
* ``actions.json`` — {issue, add_labels, assignees} the workflow applies via ``gh``.

No network calls here, so ``handle_event`` is unit-testable with a plain dict.
Identity: only the issue author (or an organizer) may drive a claim thread.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import params
import state

# Organizers may act on any claim thread (e.g. administrative withdrawal). Override
# with the ORGANIZERS env var (comma-separated GitHub handles).
ORGANIZERS = set(filter(None, os.environ.get(
    "ORGANIZERS", "oesteban,guiomarniso").lower().split(",")))

ID_RE = re.compile(r"\b([A-Za-z]+-R?\d+)\b")
CMD_RE = re.compile(r"/(claim|withdraw|submit|extend)\s+([^\n]*)", re.IGNORECASE)

# What marks a thread as a claim. The claim form applies it at creation time, so it
# is present on the `opened` payload; issue_ops keys off it rather than off the body.
CLAIM_LABEL = "claim"


def _ids(segment: str) -> list[str]:
    return [m.group(1).upper() for m in ID_RE.finditer(segment)]


def _labels(issue: dict) -> set[str]:
    return {(lb.get("name") or "").lower() for lb in (issue.get("labels") or [])}


def _detect_attribution(body: str) -> str:
    # issue-form dropdown renders the chosen value as plain text
    return "anonymous" if re.search(r"\banonymous\b", body, re.IGNORECASE) else "attributed"


def _detect_consent(body: str) -> bool:
    # a ticked GitHub-form checkbox renders as "- [x] ..."
    return bool(re.search(r"-\s*\[x\]", body, re.IGNORECASE))


def handle_close(issue: int, author: str, claims: dict) -> dict:
    """Heads-up on issue close — no state change. Silent if nothing is held."""
    claim = claims.get(issue)
    held = [pid for pid, r in (claim["papers"].items() if claim else [])
            if r["state"] in state.IN_FLIGHT]
    if not held:
        return {"comment": "", "add_labels": [], "assignees": [], "issue": issue, "changed": False}
    lst = ", ".join(f"`{p}`" for p in held)
    verb = "is" if len(held) == 1 else "are"
    body = (f"ℹ️ @{author} closing this issue does **not** release your claims — "
            f"{lst} {verb} still active and the 12-day deadline keeps running. "
            f"To return a paper, comment `/withdraw <ID>`; to submit, `/submit <ID>`. "
            f"Reopen this issue to keep working.")
    return {"comment": body, "add_labels": [], "assignees": [], "issue": issue, "changed": False}


def handle_event(event: dict) -> dict:
    """Return {comment, add_labels, assignees, issue, changed}. Mutates + persists
    the claim file when a command applies."""
    name = event.get("event_name")
    pool = state.load_pool()
    claims = state.load_claims()

    if name == "issues" and event.get("action") == "closed":
        # Closing is non-destructive: we never withdraw on close (protects against an
        # accidental close and preserves submitted/graded work). Just a heads-up if
        # the thread still holds in-flight papers.
        return handle_close(event["issue"]["number"],
                            event["issue"]["user"]["login"].lower(), claims)

    if name == "issues":
        issue = event["issue"]["number"]
        author = event["issue"]["user"]["login"].lower()
        body = event["issue"].get("body") or ""
        actor = author
        # Only a claim thread may drive claim state. Every other issue is none of our
        # business — stay silent. Previously ANY new issue had its body scanned for
        # pool IDs and was treated as a claim, so a bug report that merely quoted
        # "FMRI-01" was answered with a consent rejection (#26).
        if CLAIM_LABEL in _labels(event["issue"]):
            commands = [("claim", _ids(body))]
        elif CMD_RE.search(body):
            # Hand-filed without the form: an explicit /claim is unambiguous intent.
            commands = [(m.group(1).lower(), _ids(m.group(2))) for m in CMD_RE.finditer(body)]
        else:
            return {"comment": "", "add_labels": [], "assignees": [], "issue": issue,
                    "changed": False}
        attribution = _detect_attribution(body)
        consent = _detect_consent(body)
    elif name == "issue_comment":
        issue = event["issue"]["number"]
        author = event["issue"]["user"]["login"].lower()
        actor = event["comment"]["user"]["login"].lower()
        body = event["comment"].get("body") or ""
        commands = [(m.group(1).lower(), _ids(m.group(2))) for m in CMD_RE.finditer(body)]
        attribution = None
        consent = None
    else:
        return {"comment": "", "add_labels": [], "assignees": [], "issue": None, "changed": False}

    if not commands or all(not ids for _c, ids in commands):
        return {"comment": "", "add_labels": [], "assignees": [], "issue": issue, "changed": False}

    # identity barrier
    if actor != author and actor not in ORGANIZERS:
        return {"comment": (f"@{actor} only the thread's owner (@{author}) or an organizer "
                            f"can run claim commands here."),
                "add_labels": [], "assignees": [], "issue": issue, "changed": False}

    claim = claims.get(issue) or state.new_claim(issue, author)
    if attribution:
        claim["attribution"] = attribution
    if consent is not None and consent and not claim["consent"]["gdpr"]:
        claim["consent"] = {"gdpr": True, "at": state.iso(state.now_utc())}
    claims[issue] = claim

    # GDPR gate: a first claim requires consent (the form makes the box required)
    is_claim = any(c == "claim" for c, _ in commands)
    if is_claim and not claim["consent"]["gdpr"]:
        return {"comment": ("❌ We can't record a claim without the consent checkbox ticked. "
                            "Please edit the issue and confirm consent (see `CONSENT.md`)."),
                "add_labels": [], "assignees": [], "issue": issue, "changed": False}

    now = state.now_utc()
    out = state.Outcome()
    accepted_modalities: set[str] = set()
    attempted = False  # a claim/withdraw/submit command with ids was processed
    for cmd, ids in commands:
        if not ids:
            continue
        if cmd == "claim":
            r = state.apply_claim(pool, claims, claim, ids, now)
            attempted = True
        elif cmd == "withdraw":
            r = state.apply_withdraw(claim, ids)
            attempted = True
        elif cmd == "submit":
            r = state.apply_submit(claim, ids)
            attempted = True
        elif cmd == "extend":
            r = state.apply_extend(claim, ids, now)
        else:
            continue
        out.ok += r.ok
        out.rejected += r.rejected
        if cmd == "claim":
            accepted_modalities |= {pool[i]["modality"] for i in claim["papers"]
                                    if pool[i]["modality"]}

    state.save_claim(claim)
    claims = state.load_claims()
    state.write_status(pool, claims)

    # close the thread once nothing is left for the participant to actively work on:
    # a withdrawal/rejected-claim leaves it empty, or the last active paper was submitted
    # (submitted papers are now with the organizers for grading, not the participant).
    active_left = sum(1 for r in claim["papers"].values() if r["state"] == "active")
    close_issue = attempted and active_left == 0

    add_labels = ["claim"] + [f"mod:{m}" for m in sorted(accepted_modalities)]
    return {
        "comment": out.comment(author),
        "add_labels": add_labels,
        "assignees": [author],
        "issue": issue,
        "changed": True,
        "close": close_issue,
    }


def main() -> None:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text()) if event_path else {}
    event.setdefault("event_name", os.environ.get("GITHUB_EVENT_NAME", ""))

    result = handle_event(event)

    (state.REPO / "comment.md").write_text(result["comment"] + ("\n" if result["comment"] else ""))
    (state.REPO / "actions.json").write_text(json.dumps({
        "issue": result["issue"],
        "add_labels": result["add_labels"],
        "assignees": result["assignees"],
        "changed": result["changed"],
        "close": result.get("close", False),
    }, indent=2) + "\n")
    print(f"issue-ops: issue={result['issue']} changed={result['changed']} "
          f"labels={result['add_labels']}")


if __name__ == "__main__":
    main()
