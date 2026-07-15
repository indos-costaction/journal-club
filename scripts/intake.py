#!/usr/bin/env python3
"""Organizer-side intake: match retrieved PDFs to claims, then post the intent.

Participants upload to LimeSurvey and are done — uploading *is* submitting (see
``submission-form.md``). This script is the other half: the organizer downloads the
responses, drops the PDFs in the private inbox, and runs::

    python scripts/intake.py reconcile                  # read-only, four-bucket report
    python scripts/intake.py post --map export.csv      # post /submit <ID> ref:<n>

Two design rules make this safe:

**1. Intake posts intent; issue-ops applies it.** This script never writes
``claims/``. It comments ``/submit <ID> ref:<n>`` on the thread and stops;
``issue_ops.py`` — the same validated path a participant's command would take —
does the mutation. There is exactly one writer.

    Trap: if this script *also* mutated state, ``issue-ops`` would fire on the
    comment and apply the same submit a second time.

That works because ``issue_ops.ORGANIZERS`` already lets an organizer drive any
claim thread. It must therefore run **locally, as you** — a comment posted from
inside a GitHub Action comes from ``github-actions[bot]``, which ``issue-ops.yml``
deliberately ignores, so the submit would silently never apply.

**2. The inbox can never live in this repo.** ``indos-costaction/journal-club`` is
public; annotated PDFs are copyrighted derivatives *and* identity-bound personal
data. ``.gitignore`` is a convenience, not a control — one ``git add -f`` and they
are in public history for good. ``_resolve_inbox`` refuses rather than trusting.

The feed is swappable by design: ``reconcile()`` is a pure function of
``[Upload] + claims``. Reading filenames is one producer of that list; an IMAP
poller or a LimeSurvey RPC poller would be others, with no change here.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import state

# Default: a sibling of the repo, i.e. the PRIVATE parent initiative folder
# (initiatives/2026-07-federated-journal-club/inbox/). Override with JC_INBOX.
DEFAULT_INBOX = state.REPO.parent / "inbox"

# LimeSurvey response-export columns. These are not guesses: submission-form.md
# pins the question codes, so exporting with "question code" headings produces
# exactly these. Change them here and nowhere else.
LIMESURVEY_COLUMNS = {
    "issue": "issue",
    "paper": "paper",
    "gh": "gh",
    "ref": "id",            # LimeSurvey's own response id — our submission_ref
    "submitted_at": "submitdate",
}

# EEG-15--friedrich-ph-carrle--9.pdf  ==  state.claim_id() + ".pdf"
# Split on the double hyphen: both paper ids and GitHub handles contain single ones.
FILENAME_RE = re.compile(r"^(?P<paper>.+?)--(?P<gh>.+?)--(?P<issue>\d+)$")


@dataclass(frozen=True)
class Upload:
    """One received review, however it was discovered."""
    issue: int
    paper: str
    gh: str
    ref: str | None = None
    submitted_at: str = ""
    source: str = ""


# --- inbox ------------------------------------------------------------------
def _resolve_inbox(raw: str | None = None) -> Path:
    inbox = Path(raw or os.environ.get("JC_INBOX") or DEFAULT_INBOX).expanduser().resolve()
    repo = state.REPO.resolve()
    if inbox == repo or repo in inbox.parents:
        raise SystemExit(
            f"error: refusing to use an inbox inside the journal-club repo.\n"
            f"  inbox: {inbox}\n"
            f"  repo:  {repo}  (PUBLIC)\n"
            f"Annotated PDFs are copyrighted derivatives and personal data; a .gitignore\n"
            f"will not save you from one `git add -f`. Put the inbox in the private parent\n"
            f"(default: {DEFAULT_INBOX}) or set JC_INBOX elsewhere.")
    if not inbox.is_dir():
        raise SystemExit(f"error: inbox does not exist: {inbox}\n"
                         f"Create it, or set JC_INBOX. (It should be git-ignored — LimeSurvey\n"
                         f"is the store of record; this is a working copy.)")
    return inbox


def uploads_from_inbox(inbox: Path) -> tuple[list[Upload], list[str]]:
    """Read PDFs whose filename is the claim id. Returns (uploads, bad_filenames)."""
    uploads, bad = [], []
    for f in sorted(inbox.iterdir()):
        if not f.is_file() or f.suffix.lower() != ".pdf":
            continue
        m = FILENAME_RE.match(f.stem)
        if not m:
            bad.append(f.name)
            continue
        uploads.append(Upload(issue=int(m.group("issue")), paper=m.group("paper").upper(),
                              gh=m.group("gh").lower(), source=f.name))
    return uploads, bad


# --- LimeSurvey export ------------------------------------------------------
def uploads_from_csv(path: Path) -> list[Upload]:
    """Parse a LimeSurvey response export. Loud on an unexpected shape."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return []
    found = set(rows[0].keys())
    missing = [c for c in LIMESURVEY_COLUMNS.values() if c not in found]
    if missing:
        raise SystemExit(
            f"error: {path.name} is missing column(s): {', '.join(missing)}\n"
            f"columns found: {', '.join(sorted(found))}\n"
            f"Export with 'question code' headings (not full/abbreviated question text),\n"
            f"or fix LIMESURVEY_COLUMNS in this file if the survey codes changed.")
    c = LIMESURVEY_COLUMNS
    out = []
    for r in rows:
        issue, paper = (r.get(c["issue"]) or "").strip(), (r.get(c["paper"]) or "").strip()
        if not issue.isdigit() or not paper:
            continue  # arrived without the prefilled link — the unmatchable bucket
        out.append(Upload(issue=int(issue), paper=paper.upper(),
                          gh=(r.get(c["gh"]) or "").strip().lower(),
                          ref=(r.get(c["ref"]) or "").strip() or None,
                          submitted_at=(r.get(c["submitted_at"]) or "").strip(),
                          source=f"response {r.get(c['ref'])}"))
    return out


def latest_per_claim(uploads: list[Upload]) -> list[Upload]:
    """Re-uploads are expected and allowed; the last one wins."""
    best: dict[tuple, Upload] = {}
    for u in uploads:
        k = (u.issue, u.paper, u.gh)
        if k not in best or u.submitted_at >= best[k].submitted_at:
            best[k] = u
    return sorted(best.values(), key=lambda u: (u.issue, u.paper))


# --- the reconcile core (pure) ---------------------------------------------
def reconcile(uploads: list[Upload], claims: dict) -> dict[str, list]:
    """Pure: uploads + claims -> four buckets. The feed-agnostic heart of intake."""
    matched, to_post, unmatchable = [], [], []
    seen: set[tuple] = set()

    for u in uploads:
        claim = claims.get(u.issue)
        if claim is None:
            unmatchable.append((u, f"no claim issue #{u.issue}"))
            continue
        if u.gh and claim["participant"].lower() != u.gh:
            unmatchable.append((u, f"issue #{u.issue} belongs to @{claim['participant']}, "
                                   f"not @{u.gh}"))
            continue
        rec = claim["papers"].get(u.paper)
        if rec is None:
            unmatchable.append((u, f"issue #{u.issue} holds no claim on {u.paper}"))
            continue
        seen.add((u.issue, u.paper))
        if rec["state"] == "active":
            to_post.append(u)
        elif rec["state"] in ("submitted", "completed"):
            matched.append(u)
        else:
            unmatchable.append((u, f"claim on {u.paper} is `{rec['state']}` — "
                                   f"it was returned to the pool"))

    awaiting = [(issue, pid, c["participant"])
                for issue, c in claims.items()
                for pid, rec in c["papers"].items()
                if rec["state"] == "active" and (issue, pid) not in seen]

    return {"matched": matched, "to_post": to_post,
            "awaiting": sorted(awaiting), "unmatchable": unmatchable}


# --- commands ---------------------------------------------------------------
def _report(b: dict, bad: list[str]) -> None:
    print(f"\n✅ already recorded      {len(b['matched'])}")
    for u in b["matched"]:
        print(f"     {u.paper} · @{u.gh} · #{u.issue}")
    print(f"\n📥 to post (/submit)     {len(b['to_post'])}")
    for u in b["to_post"]:
        print(f"     {u.paper} · @{u.gh} · #{u.issue}"
              + (f" · ref {u.ref}" if u.ref else " · no ref"))
    print(f"\n⏳ claimed, no upload    {len(b['awaiting'])}")
    for issue, pid, who in b["awaiting"]:
        print(f"     {pid} · @{who} · #{issue}")
    print(f"\n❌ unmatchable           {len(b['unmatchable']) + len(bad)}")
    for u, why in b["unmatchable"]:
        print(f"     {u.source or u.paper}: {why}")
    for name in bad:
        print(f"     {name}: filename is not <PAPER>--<handle>--<issue>.pdf")
    print()


def _gather(args) -> tuple[list[Upload], list[str]]:
    """Uploads in hand, enriched with response ids.

    The **inbox is the authority** on what has actually been received; the export only
    supplies each PDF's response id. Keying off the inbox is what stops a LimeSurvey
    response whose file we never retrieved from being posted as received.
    """
    inbox = _resolve_inbox(args.inbox)
    uploads, bad = uploads_from_inbox(inbox)

    refs: dict[tuple, str | None] = {}
    if args.map:
        responses = latest_per_claim(uploads_from_csv(Path(args.map)))
        refs = {(u.issue, u.paper): u.ref for u in responses}
        have = {(u.issue, u.paper) for u in uploads}
        pending = [u for u in responses if (u.issue, u.paper) not in have]
        if pending:
            print(f"note: {len(pending)} response(s) in the export have no PDF in the inbox "
                  f"— not downloaded yet:", file=sys.stderr)
            for u in pending:
                print(f"        {u.paper} · @{u.gh} · #{u.issue} · ref {u.ref}", file=sys.stderr)

    unreadable = f", {len(bad)} unreadable filename(s)" if bad else ""
    print(f"inbox: {inbox}   ({len(uploads) + len(bad)} PDF(s){unreadable})")
    enriched = [Upload(u.issue, u.paper, u.gh, ref=refs.get((u.issue, u.paper)),
                       source=u.source)
                for u in uploads]
    return enriched, bad


def cmd_reconcile(args) -> None:
    uploads, bad = _gather(args)
    _report(reconcile(uploads, state.load_claims()), bad)


def cmd_post(args) -> None:
    if not args.map:
        print("warning: no --map given, so no LimeSurvey response id is available and\n"
              "         submission_ref will stay null. Pass the CSV export to record\n"
              "         where each PDF actually came from.", file=sys.stderr)
    uploads, bad = _gather(args)
    buckets = reconcile(uploads, state.load_claims())
    _report(buckets, bad)

    if not buckets["to_post"]:
        print("nothing to post — already reconciled.")
        return
    for u in buckets["to_post"]:
        body = f"/submit {u.paper}" + (f" ref:{u.ref}" if u.ref else "")
        if args.dry_run:
            print(f"[dry-run] gh issue comment {u.issue} --body '{body}'")
            continue
        r = subprocess.run(["gh", "issue", "comment", str(u.issue), "--body", body],
                           cwd=state.REPO, check=False)
        print(f"{'posted ' if r.returncode == 0 else 'FAILED '} #{u.issue}: {body}")
    if args.dry_run:
        print("\n(dry run — nothing posted. Drop --dry-run to send.)")
    else:
        print("\nissue-ops will apply these and reply on each thread. Re-running posts nothing.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inbox", help=f"default: $JC_INBOX or {DEFAULT_INBOX}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("reconcile", help="read-only four-bucket report")
    r.add_argument("--map", help="LimeSurvey CSV export (optional; else filenames only)")
    r.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("post", help="post /submit for uploads not yet recorded")
    p.add_argument("--map", help="LimeSurvey CSV export — supplies submission_ref")
    p.add_argument("--dry-run", action="store_true", help="print the comments, send nothing")
    p.set_defaults(func=cmd_post)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
