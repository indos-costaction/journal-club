#!/usr/bin/env python3
"""Issue-ops dispatcher — the single entrypoint ``issue-ops.yml`` runs.

Parses a GitHub ``issues`` (claim form) or ``issue_comment`` (`/claim` `/withdraw`
`/extend` `/confirm`, and the organizer-only `/received` `/reject`) event, validates
+ applies it against freshly-loaded state, and writes:

* the updated ``claims/<issue>.json`` + regenerated ``data/status.json``;
* ``comment.md``   — the reply body the workflow posts (GitHub then emails the author);
* ``actions.json`` — {issue, add_labels, assignees} the workflow applies via ``gh``.

No network calls here, so ``handle_event`` is unit-testable with a plain dict.

Identity is enforced in two tiers (see ``COMMAND_ACL``): a coarse gate rejects anyone
who is neither the thread's author nor an organizer, then each command checks the role
it actually requires. The fine tier is what makes ``/confirm`` author-only — an
organizer may drive every other command on any thread, but not that one.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import messages
import params
import state

# Organizers may act on any claim thread. This is what lets intake.py post
# `/received <ID> ref:<n>` on a participant's thread once their upload is in hand.
# Override with the ORGANIZERS env var (comma-separated GitHub handles).
ORGANIZERS = set(filter(None, os.environ.get(
    "ORGANIZERS", "oesteban").lower().split(",")))

ID_RE = re.compile(r"\b([A-Za-z]+-R?\d+)\b")
CMD_RE = re.compile(r"/(claim|withdraw|submit|received|confirm|reject|extend)\s+([^\n]*)",
                    re.IGNORECASE)
# `/received EEG-15 ref:12345` — the LimeSurvey response id, our submission provenance.
REF_RE = re.compile(r"\bref:([A-Za-z0-9_-]+)", re.IGNORECASE)

# `/submit` was the pre-handshake name for `/received`. Kept as an undocumented alias
# because an unrecognised command is a *silent* no-op (see the `not commands` return
# below) — a stale intake.py in a shell history would otherwise fail without a sound.
ALIASES = {"submit": "received"}

# Who may run each command. Roles are not exclusive: an organizer acting on their own
# claim thread holds both, so they can /received then /confirm their own upload —
# unavoidable, acceptable, and on the public record.
#
# `confirm` being author-only is the load-bearing entry. The whole point of the
# handshake is that GitHub identity — not an open, publicly-linked web form — asserts
# who wrote a review; an organizer able to confirm on someone's behalf makes it theatre.
COMMAND_ACL = {
    "claim":    {"author", "organizer"},
    "withdraw": {"author", "organizer"},
    "extend":   {"author", "organizer"},
    "confirm":  {"author"},        # NOT organizer — deliberately un-proxyable
    "received": {"organizer"},
    "reject":   {"organizer"},
}

# What marks a thread as a claim. The claim form applies it at creation time, so it
# is present on the `opened` payload; issue_ops keys off it rather than off the body.
CLAIM_LABEL = "claim"


def _ids(segment: str) -> list[str]:
    return [m.group(1).upper() for m in ID_RE.finditer(segment)]


def _labels(issue: dict) -> set[str]:
    return {(lb.get("name") or "").lower() for lb in (issue.get("labels") or [])}


def _split_ref(segment: str) -> tuple[list[str], str | None]:
    """Pull the ref out of a command segment, then read ids from what's left.

    Stripping first matters: a hypothetical non-numeric ref like `ref:abc-123` would
    otherwise be scraped by ID_RE as a paper id.
    """
    m = REF_RE.search(segment)
    return _ids(REF_RE.sub(" ", segment)), (m.group(1) if m else None)


def _parse(body: str) -> list[tuple[str, list[str], str | None]]:
    """Every command in a body, as (canonical_name, paper_ids, ref)."""
    out = []
    for m in CMD_RE.finditer(body):
        name = m.group(1).lower()
        out.append((ALIASES.get(name, name), *_split_ref(m.group(2))))
    return out


def _roles(actor: str, author: str) -> set[str]:
    """Which hats this actor wears on this thread. Not mutually exclusive."""
    roles = set()
    if actor == author:
        roles.add("author")
    if actor in ORGANIZERS:
        roles.add("organizer")
    return roles


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
    return {"comment": messages.close_notice(author, held), "add_labels": [],
            "assignees": [], "issue": issue, "changed": False}


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
            commands = [("claim", _ids(body), None)]
        elif CMD_RE.search(body):
            # Hand-filed without the form: an explicit /claim is unambiguous intent.
            commands = _parse(body)
        else:
            return {"comment": "", "add_labels": [], "assignees": [], "issue": issue,
                    "changed": False}
        attribution = _detect_attribution(body)
        consent = _detect_consent(body)
        is_form = True  # the claim form: reply with the full onboarding
    elif name == "issue_comment":
        issue = event["issue"]["number"]
        author = event["issue"]["user"]["login"].lower()
        actor = event["comment"]["user"]["login"].lower()
        body = event["comment"].get("body") or ""
        commands = _parse(body)
        attribution = None
        consent = None
        is_form = False
    else:
        return {"comment": "", "add_labels": [], "assignees": [], "issue": None, "changed": False}

    if not commands or all(not ids for _c, ids, _r in commands):
        return {"comment": "", "add_labels": [], "assignees": [], "issue": issue, "changed": False}

    # Identity barrier, tier 1 (coarse): an actor with no role at all gets nothing. This
    # early return also keeps a passer-by from causing the new_claim()+save_claim() below
    # to write a claim file for them. Tier 2 (per command) is inside the loop.
    roles = _roles(actor, author)
    if not roles:
        return {"comment": messages.not_your_thread(actor, author),
                "add_labels": [], "assignees": [], "issue": issue, "changed": False}

    claim = claims.get(issue) or state.new_claim(issue, author)
    if attribution:
        claim["attribution"] = attribution
    if consent is not None and consent and not claim["consent"]["gdpr"]:
        claim["consent"] = {"gdpr": True, "at": state.iso(state.now_utc())}
    claims[issue] = claim

    # GDPR gate: a first claim requires consent (the form makes the box required)
    is_claim = any(c == "claim" for c, _ids_, _ref in commands)
    if is_claim and not claim["consent"]["gdpr"]:
        return {"comment": messages.consent_missing(),
                "add_labels": [], "assignees": [], "issue": issue, "changed": False}

    now = state.now_utc()
    out = state.Outcome()
    accepted_modalities: set[str] = set()
    attempted = False  # a state-changing command with ids was processed
    for cmd, ids, ref in commands:
        if not ids:
            continue
        # Identity barrier, tier 2 (per command). Rendered through the normal delta path
        # rather than an early return, so a mixed comment still applies what it
        # legitimately can and explains what it couldn't.
        if not (roles & COMMAND_ACL.get(cmd, set())):
            out.reject(messages.not_allowed(actor, cmd, author))
            continue
        if cmd == "claim":
            r = state.apply_claim(pool, claims, claim, ids, now)
            attempted = True
        elif cmd == "withdraw":
            r = state.apply_withdraw(claim, ids)
            attempted = True
        elif cmd == "received":
            r = state.apply_receive(claim, ids, now, ref)
            attempted = True
        elif cmd == "confirm":
            r = state.apply_confirm(claim, ids, now)
            attempted = True
        elif cmd == "reject":
            r = state.apply_reject(claim, ids, now, actor)
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

    # Close once nothing on the thread needs the participant. `pending` counts as needing
    # them — we are about to ask for a /confirm, and closing the thread we're asking them
    # to reply on would strand the handshake.
    waiting_on_them = sum(1 for r in claim["papers"].values()
                          if r["state"] in state.NEEDS_PARTICIPANT)
    close_issue = attempted and waiting_on_them == 0

    add_labels = ["claim"] + [f"mod:{m}" for m in sorted(accepted_modalities)]
    return {
        # `claims` was reloaded above, so the cap line sees this participant's other
        # threads too — the cap is per participant, not per thread
        "comment": (messages.claim_confirmation(claim, pool, out, claims) if is_form
                    else messages.command_ack(claim, pool, out, claims)),
        "add_labels": add_labels,
        "assignees": [author],
        "issue": issue,
        "changed": True,
        "close": close_issue,
        # prose belongs in messages.py, not inline in the workflow YAML
        "close_comment": messages.thread_done(claim) if close_issue else "",
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
        "close_comment": result.get("close_comment", ""),
    }, indent=2) + "\n")
    print(f"issue-ops: issue={result['issue']} changed={result['changed']} "
          f"labels={result['add_labels']}")


if __name__ == "__main__":
    main()
