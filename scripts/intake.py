#!/usr/bin/env python3
"""Organizer-side intake: retrieve uploads, match them to claims, post the intent.

An upload is **not** a submission. The LimeSurvey form is open-access and its per-paper
link is published in a public issue, so a file proves only that *someone* had the link.
The claimant's ``/confirm`` on their own thread is what makes it a review. This script
runs the organizer's half::

    python scripts/intake.py ingest --zip files.zip --map export.csv   # → canonical PDFs
    python scripts/intake.py reconcile                                 # read-only report
    python scripts/intake.py post --map export.csv [--dry-run]         # → /received …

Three design rules make this safe:

**1. Intake posts intent; issue-ops applies it.** This script never writes ``claims/``.
It comments ``/received <ID> ref:<n>`` on the thread and stops; ``issue_ops.py`` — the
same validated path a participant's command would take — does the mutation. Exactly one
writer.

    Trap: if this script *also* mutated state, ``issue-ops`` would fire on the comment
    and apply the same receive a second time.

That works because ``issue_ops.COMMAND_ACL`` grants ``received`` to organizers. It must
therefore run **locally, as you** — a comment posted from inside a GitHub Action comes
from ``github-actions[bot]``, which ``issue-ops.yml`` deliberately ignores, so the
receive would silently never apply.

**2. The inbox can never live in this repo.** ``indos-costaction/journal-club`` is
public; annotated PDFs are copyrighted derivatives *and* identity-bound personal data.
``.gitignore`` is a convenience, not a control — one ``git add -f`` and they are in
public history for good. ``_resolve_inbox`` refuses rather than trusting.

**3. Never guess which file is whose.** ``ingest`` matches on identifiers LimeSurvey
records in the response itself, not on where a file happens to sit in the archive. An
unmatched file is reported, never assigned by proximity.

The feed is swappable by design: ``reconcile()`` is a pure function of ``[Upload] +
claims``. Reading filenames is one producer of that list; an IMAP poller or a LimeSurvey
RPC poller would be others, with no change here.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
    "pdf": "pdf",           # the file-upload question — a JSON blob, see _file_meta
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
    # From the response's file-upload blob; used only by `ingest` to find the file in the
    # archive. `stored` is LimeSurvey's own `fu_<random>` name — the unambiguous key.
    stored: str = ""
    orig: str = ""

    @property
    def claim_id(self) -> str:
        return state.claim_id(self.paper, self.gh, self.issue)


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
        stored, orig = _file_meta(r.get(c["pdf"]) or "")
        out.append(Upload(issue=int(issue), paper=paper.upper(),
                          gh=(r.get(c["gh"]) or "").strip().lower(),
                          ref=(r.get(c["ref"]) or "").strip() or None,
                          submitted_at=(r.get(c["submitted_at"]) or "").strip(),
                          stored=stored, orig=orig,
                          source=f"response {r.get(c['ref'])}"))
    return out


def _file_meta(cell: str) -> tuple[str, str]:
    """(stored_name, original_name) from a LimeSurvey file-upload cell.

    The cell holds a JSON array of file records, e.g.::

        [{"title":"","comment":"","size":"842.1","name":"my review.pdf",
          "filename":"fu_ph4x7k2m9qab","ext":"pdf"}]

    ``filename`` is what's on disk (and in the archive); ``name`` is what the participant
    called it. Returns ("", "") on anything unexpected — the caller reports the response
    as unresolvable rather than guessing at a file.
    """
    cell = (cell or "").strip()
    if not cell:
        return "", ""
    try:
        items = json.loads(cell)
    except (json.JSONDecodeError, TypeError):
        return "", ""
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return "", ""
    f = items[0]
    return str(f.get("filename") or "").strip(), str(f.get("name") or "").strip()


# How LimeSurvey names members of its "export uploaded files" archive:
#
#     00010_01-pdf_00-removal-of-artifacts-from-eeg-signals-a-review.pdf
#     └─┬─┘ └┬┘ └┬┘ └┬┘└──────────────────┬─────────────────────────┘
#    response  │   │   │        slugified original filename
#       id     │  question index of this file within the response
#              │   code
#     running counter over the exported files
#
# The leading number is the **response id**, zero-padded — the only part of the name a
# response can predict about its own file, and therefore the key this matches on.
# Confirmed against the 2026-08-17 export of survey 428628: eight files, eight
# responses, an exact 1:1 on that prefix.
MEMBER_RESPONSE_RE = re.compile(r"^(\d+)_")


def _member_response_id(member: str) -> int | None:
    """The response id a member filename is prefixed with, or None.

    ``PurePosixPath`` because zip entries always use ``/``, whatever the host OS.
    """
    m = MEMBER_RESPONSE_RE.match(PurePosixPath(member).name)
    return int(m.group(1)) if m else None


def _slug(name: str) -> str:
    """LimeSurvey's slug for an original filename, as far as it can be observed.

    Verified against the 2026-08-17 export only for names that were already
    hyphen-cased: it lowercases and leaves ``.``, ``-``, ``(`` and ``)`` alone.
    Whitespace handling is **inferred, not observed** — no exported name contained a
    space. That guess is safe to make here because this feeds only the last-resort
    rule below, which refuses on any ambiguity: getting it wrong costs a fallback
    match, never a wrong one.
    """
    return re.sub(r"\s+", "-", (name or "").strip()).lower()


def find_member(members: list[str], u: Upload) -> tuple[str | None, str]:
    """Locate this response's file among archive members. Returns (member, why_not).

    Deliberately **ignores directory structure** — LimeSurvey's archive layout is not a
    contract, and betting on one would break silently the day it changes. Instead this
    matches on identifiers the response itself carries, strongest first:

      1. ``fu_<random>`` stored name. The strongest key when it is there, and it is
         **not** there in the 2026-08 export: LimeSurvey renames on export and the
         on-disk name survives nowhere in the archive. Kept because a different
         version or export mode may still carry it, and a hit is unambiguous.
      2. the response id the member filename is prefixed with — the rule that actually
         works. It used to test ``u.ref in Path(m).parts``, i.e. the id as a whole
         *path component*, which cannot match a flat archive of ``00010_…`` names and
         would not match the zero-padding even if the archive were nested. That, plus
         (1) never firing and (3) being case-sensitive, is why this matched 0 of 8
         real files while reporting each one as simply "not found".
      3. the participant's slugified original filename — last, and only if unique,
         because two people both uploading `review.pdf` is entirely likely. In the
         2026-08 export one participant's four re-uploads were all
         ``MEEG-Review-fonay.pdf``, so this correctly refuses on them and (2) resolves
         them instead.

    Ambiguity is never resolved by picking one. It is reported.
    """
    def _hits(pred):
        return [m for m in members if pred(m)]

    ref = (u.ref or "").strip()
    slug = _slug(u.orig)

    for label, hits in (
        (f"stored name {u.stored}",
         _hits(lambda m: u.stored and u.stored in m)),
        (f"response id {ref}",
         _hits(lambda m: ref.isdigit() and _member_response_id(m) == int(ref))),
        (f"filename {u.orig!r}",
         _hits(lambda m: slug and PurePosixPath(m).name.lower().endswith(slug))),
    ):
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            return None, f"{len(hits)} archive files match {label} — ambiguous, not guessing"
    return None, ("no archive file matches this response "
                  f"(response id={ref or '?'}, stored={u.stored or '?'}, "
                  f"name={u.orig or '?'})")


def has_file(u: Upload) -> bool:
    """Did this response actually carry an upload?

    A blank file-upload cell means the participant reached the form and never got a
    file into it. Common enough to matter — one participant left ~55 such rows across
    12-13 Aug — and it must not be confused with an upload we failed to locate in the
    archive: there is nothing there to locate.
    """
    return bool(u.stored or u.orig)


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
    """Pure: uploads + claims -> buckets. The feed-agnostic heart of intake."""
    matched, to_post, late, awaiting_confirm, unmatchable = [], [], [], [], []
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
        st = rec["state"]
        if st == "active":
            to_post.append(u)
        elif st == "expired":
            # They uploaded; the daily sweep expired the claim before we got here. The
            # deadline is a cron but retrieval is manual, so this gap is *our* latency,
            # not their lateness. /received accepts it — but surface it, don't bury it.
            late.append(u)
        elif st == "pending":
            awaiting_confirm.append((u, claim["participant"]))
        elif st in ("submitted", "completed"):
            matched.append(u)
        else:
            unmatchable.append((u, f"claim on {u.paper} is `{st}` — "
                                   f"it was returned to the pool"))

    awaiting = [(issue, pid, c["participant"])
                for issue, c in claims.items()
                for pid, rec in c["papers"].items()
                if rec["state"] == "active" and (issue, pid) not in seen]

    return {"matched": matched, "to_post": to_post, "late": late,
            "awaiting_confirm": awaiting_confirm,
            "awaiting": sorted(awaiting), "unmatchable": unmatchable}


# --- commands ---------------------------------------------------------------
def _report(b: dict, bad: list[str]) -> None:
    def _line(u, extra=""):
        print(f"     {u.paper} · @{u.gh} · #{u.issue}"
              + (f" · ref {u.ref}" if u.ref else " · no ref") + extra)

    print(f"\n✅ already recorded         {len(b['matched'])}")
    for u in b["matched"]:
        _line(u)
    print(f"\n📥 to post (/received)      {len(b['to_post'])}")
    for u in b["to_post"]:
        _line(u)
    if b["late"]:
        print(f"\n⚠️  LATE — claim expired     {len(b['late'])}")
        print("     Uploaded, but the sweep expired the claim before intake ran.")
        print("     /received accepts these; check the submitdate and post them.")
        for u in b["late"]:
            _line(u, f" · uploaded {u.submitted_at or '?'}")
    if b["awaiting_confirm"]:
        print(f"\n🖊️  awaiting their /confirm  {len(b['awaiting_confirm'])}")
        print("     Posted; waiting on the claimant to sign off. Nothing to do.")
        for u, who in b["awaiting_confirm"]:
            _line(u)
    print(f"\n⏳ claimed, no upload       {len(b['awaiting'])}")
    for issue, pid, who in b["awaiting"]:
        print(f"     {pid} · @{who} · #{issue}")
    print(f"\n❌ unmatchable              {len(b['unmatchable']) + len(bad)}")
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
        # has_file for the same reason cmd_ingest uses it, and it matters more here:
        # this ref is written into the claim record by /received and is the permanent
        # pointer to the store of record. A file-less response winning the tiebreak
        # would aim it at a row that never carried a review. Currently latent — all 161
        # such rows in the 2026-08 export are unsubmitted, so they carry a blank
        # submitdate and sort last — which is luck, not a property worth relying on.
        responses = latest_per_claim(
            [u for u in uploads_from_csv(Path(args.map)) if has_file(u)])
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


def cmd_ingest(args) -> None:
    """Unpack a LimeSurvey file archive into the inbox under canonical names.

    Turns the manual half of the loop — download, unzip, work out which mangled file is
    whose, rename — into one command. The naming is not cosmetic: the filename **is**
    ``state.claim_id()``, the same key the ledger uses, which is what lets `reconcile`
    work off filenames alone with no CSV in hand.
    """
    if not args.map:
        raise SystemExit("error: ingest needs --map (the CSV export). The archive's own\n"
                         "filenames don't say which paper or participant a file belongs to;\n"
                         "the response export is the only thing that joins them.")
    inbox = _resolve_inbox(args.inbox)
    every_response = uploads_from_csv(Path(args.map))

    # A response whose file-upload cell was empty carries no file, so there is nothing
    # in the archive to find and reporting it as "unresolved" is answering a question
    # nobody asked. Real and common: one participant's 12-13 Aug attempts left ~55 such
    # rows, which buried the five genuine uploads under a wall of false alarms.
    #
    # Filtered BEFORE latest_per_claim on purpose. "Latest" has to mean the latest
    # response that actually carried a file — otherwise a failed re-attempt after a
    # good upload would win the tiebreak and silently discard the real review.
    with_file = [u for u in every_response if has_file(u)]
    no_file = [u for u in every_response if not has_file(u)]
    responses = latest_per_claim(with_file)
    superseded = [u for u in with_file if u not in responses]
    if not responses:
        raise SystemExit(f"error: no usable responses in {Path(args.map).name}")

    if args.zip:
        zf = zipfile.ZipFile(args.zip)
        members = [n for n in zf.namelist() if not n.endswith("/")]
        read = zf.read
    else:
        root = Path(args.files).expanduser().resolve()
        if not root.is_dir():
            raise SystemExit(f"error: not a directory: {root}")
        paths = {str(p.relative_to(root)): p for p in root.rglob("*") if p.is_file()}
        members = sorted(paths)
        read = lambda m: paths[m].read_bytes()  # noqa: E731

    print(f"archive: {len(members)} file(s)   inbox: {inbox}\n")
    claimed: set[str] = set()
    written = skipped = unresolved = 0
    for u in responses:
        member, why = find_member(members, u)
        if not member:
            print(f"  ?? {u.paper} · @{u.gh} · #{u.issue}: {why}")
            unresolved += 1
            continue
        claimed.add(member)
        dest = inbox / f"{u.claim_id}.pdf"
        if dest.exists() and not args.force:
            print(f"  == {dest.name} (already in the inbox; --force to overwrite)")
            skipped += 1
            continue
        if args.dry_run:
            print(f"  -> {member}  =>  {dest.name}")
        else:
            dest.write_bytes(read(member))
            print(f"  ok {dest.name}   <- {member}")
        written += 1

    # An earlier upload the participant later replaced is accounted for, not a stray:
    # latest_per_claim dropped its response on purpose. Counting it as unmatched would
    # print `!!` at four of the eight files in the 2026-08 export, which are simply one
    # person's re-uploads — and an alarm that cries wolf is an alarm nobody reads.
    replaced = {m for m in (find_member(members, u)[0] for u in superseded) if m}
    claimed |= replaced

    # A file nobody claimed is a real signal — a response we failed to parse, or an
    # upload against a question code we don't know about. Never silently ignored.
    strays = [m for m in members if m not in claimed]
    for m in strays:
        print(f"  !! {m}: in the archive but matched to no response")

    tail = [f"{'would write' if args.dry_run else 'wrote'} {written} file(s)"]
    if skipped:
        tail.append(f"skipped {skipped} already present")
    if replaced:
        tail.append(f"ignored {len(replaced)} superseded by a later upload")
    if no_file:
        # Counted, never listed: these are usually one person failing to upload over
        # and over, and the per-row detail is in LimeSurvey. A non-zero count here is
        # worth a look — someone may be stuck.
        threads = sorted({u.issue for u in no_file})
        tail.append(f"{len(no_file)} response(s) carried no file "
                    f"(thread{'s' if len(threads) > 1 else ''} "
                    f"{', '.join('#' + str(i) for i in threads)})")
    if unresolved:
        tail.append(f"**{unresolved} response(s) unresolved**")
    if strays:
        tail.append(f"{len(strays)} archive file(s) unclaimed")
    print("\n" + ", ".join(tail) + ".")
    print("(dry run — nothing written.)" if args.dry_run
          else "Next: python scripts/intake.py reconcile")


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

    # Late ones are posted too: /received accepts an expired claim precisely so our
    # retrieval latency can't penalise someone who uploaded on time.
    outgoing = buckets["to_post"] + buckets["late"]
    if not outgoing:
        print("nothing to post — already reconciled.")
        return
    for u in outgoing:
        body = f"/received {u.paper}" + (f" ref:{u.ref}" if u.ref else "")
        if args.dry_run:
            print(f"[dry-run] gh issue comment {u.issue} --body '{body}'")
            continue
        r = subprocess.run(["gh", "issue", "comment", str(u.issue), "--body", body],
                           cwd=state.REPO, check=False)
        print(f"{'posted ' if r.returncode == 0 else 'FAILED '} #{u.issue}: {body}")
    if args.dry_run:
        print("\n(dry run — nothing posted. Drop --dry-run to send.)")
    else:
        print("\nissue-ops will apply these and ask each claimant to /confirm. "
              "Re-running posts nothing.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inbox", help=f"default: $JC_INBOX or {DEFAULT_INBOX}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="unpack a LimeSurvey file archive into the inbox")
    src = i.add_mutually_exclusive_group(required=True)
    src.add_argument("--zip", help="the survey's uploaded-files archive")
    src.add_argument("--files", help="a directory of already-unpacked files")
    i.add_argument("--map", help="LimeSurvey CSV export (required — it's the only join)")
    i.add_argument("--force", action="store_true", help="overwrite files already in the inbox")
    i.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    i.set_defaults(func=cmd_ingest)

    r = sub.add_parser("reconcile", help="read-only report")
    r.add_argument("--map", help="LimeSurvey CSV export (optional; else filenames only)")
    r.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("post", help="post /received for uploads not yet recorded")
    p.add_argument("--map", help="LimeSurvey CSV export — supplies submission_ref")
    p.add_argument("--dry-run", action="store_true", help="print the comments, send nothing")
    p.set_defaults(func=cmd_post)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
