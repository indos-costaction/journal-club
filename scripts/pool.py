#!/usr/bin/env python3
"""Organizer-side digest of the open paper suggestions.

    python scripts/pool.py review [--json]

**Read-only, deliberately.** Papers are added by commenting ``/accept-paper`` on the thread,
never from a shell here. Two reasons that is not an inconvenience:

* the decision lands as a public audit trail beside the suggestion it answers, rather than
  in one person's terminal history, and the proposer sees the id their paper received;
* there is exactly one writer to ``docs/data/pool.json`` — the same rule that keeps
  ``intake.py`` posting ``/received`` instead of editing ``claims/`` itself.

So this command exists to answer "what is waiting on me?", which the open-time bot cannot:
it sees one thread at a time and cannot tell you the same DOI was proposed on two of them.

Needs ``gh`` authenticated, and the network for the citation echo. When the network is down
it still reports which papers are already in the pool, for the same reason triage does.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict

import state
import suggest_ops as so

BULLET = {"known": "  in pool", "retired": " retired", "declined": "declined",
          "new": "     NEW", "unresolvable": "       ?"}


def open_suggestions() -> list[dict]:
    out = subprocess.run(
        ["gh", "issue", "list", "--label", so.SUGGESTION_LABEL, "--state", "open",
         "--limit", "100", "--json", "number,title,author,body,createdAt"],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out or "[]")


def cmd_review(args) -> int:
    issues = open_suggestions()
    if not issues:
        print("No open paper suggestions.")
        return 0

    pool = state.load_pool()
    fetch = None if args.offline else so.lookup
    rows, across = [], defaultdict(list)

    for issue in sorted(issues, key=lambda i: i["number"]):
        verdicts = [so.verdict_for(i, pool, fetch)
                    for i in so.parse_identifiers(issue.get("body") or "")]
        declared = so.declared_modality(issue.get("body") or "")
        rows.append((issue, declared, verdicts))
        for v in verdicts:
            if v.ident.doi:
                across[v.ident.doi].append(issue["number"])

    if args.json:
        print(json.dumps([{
            "issue": i["number"], "author": (i.get("author") or {}).get("login"),
            "declared_modality": d,
            "papers": [{"raw": v.ident.raw, "doi": v.ident.doi, "verdict": v.kind,
                        "detail": v.detail} for v in vs],
        } for i, d, vs in rows], indent=2))
        return 0

    pending = 0
    for issue, declared, verdicts in rows:
        who = (issue.get("author") or {}).get("login", "?")
        print(f"\n#{issue['number']}  {issue['title'][:62]}"
              f"\n         @{who} · {issue['createdAt'][:10]}"
              f" · modality: {declared or 'not given'}")
        for v in verdicts:
            extra = f" — {v.detail}" if v.detail else ""
            dupe = across.get(v.ident.doi, [])
            if len(dupe) > 1:
                extra += f"  [also proposed on #{', #'.join(str(n) for n in dupe if n != issue['number'])}]"
            print(f"    {BULLET[v.kind]}  {v.ident.raw[:58]}{extra}")
        fresh = [v for v in verdicts if v.kind == "new"]
        pending += len(fresh)
        if fresh:
            mod = declared or "<modality>"
            print(f"    -> /accept-paper {mod}" if declared
                  else f"    -> /accept-paper {mod}   (nobody chose one)")

    print(f"\n{pending} paper(s) awaiting a decision across {len(rows)} open thread(s).")
    print("Accept them by commenting on the thread; this command never writes.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rv = sub.add_parser("review", help="what is waiting on an organizer")
    rv.add_argument("--json", action="store_true", help="machine-readable output")
    rv.add_argument("--offline", action="store_true",
                    help="skip the citation lookup; pool checks still run")
    rv.set_defaults(func=cmd_review)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
