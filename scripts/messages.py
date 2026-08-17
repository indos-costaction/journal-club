#!/usr/bin/env python3
"""Every participant-facing string the club emits.

Prose used to live in five places at once — ``state.py`` (the ``apply_*`` outcome
lines), ``issue_ops.py`` (the close notice), ``sweep.py`` (reminders + expiry) and
inline in ``issue-ops.yml``. That made it impossible to say one consistent thing
about how to submit. It all lives here now.

Same discipline as ``state.py``: **pure functions of their inputs**, no IO, no
GitHub knowledge, no wall-clock reads. Callers pass ``claim`` / ``pool`` / an
``Outcome`` and get markdown back.

The composition model
---------------------
Every comment is::

    delta (what just changed)  +  holdings (what you hold now)
                               +  next steps (first claim only)
                               +  command reference

The *delta* comes from ``Outcome``; the *holdings* table is regenerated from live
state each time. Keeping those separate is why a ``/withdraw`` reply and a claim
confirmation can share one code path without ``Outcome`` needing to carry
structured records.
"""
from __future__ import annotations

from urllib.parse import urlencode

import params
import prose
import state

# Rendered instead of an upload link while SUBMISSION_FORM_URL is unset, so a
# half-built pathway degrades to an honest promise rather than a dead link.
FORM_PENDING = prose.t("form_pending")

_NO_AI = prose.t("no_ai")


# --- helpers ----------------------------------------------------------------
def upload_url(issue: int, pid: str, participant: str) -> str | None:
    """The paper's prefilled LimeSurvey link, or None while the form is unbuilt.

    The query carries the join key, so the response arrives already bound to
    (issue, paper, participant) and never has to be matched by hand. ``newtest=Y``
    forces a fresh response rather than resuming an earlier one.
    """
    if not params.SUBMISSION_FORM_URL:
        return None
    q = urlencode({"newtest": "Y", "issue": issue, "paper": pid, "gh": participant})
    sep = "&" if "?" in params.SUBMISSION_FORM_URL else "?"
    return f"{params.SUBMISSION_FORM_URL}{sep}{q}"


def _cell(text: str) -> str:
    """Make arbitrary text safe inside a markdown table cell.

    Paper titles are third-party data: an unescaped ``|`` silently shears a row in
    half, and a newline ends the table.
    """
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _paper_ref(pool: dict, pid: str) -> str:
    p = pool.get(pid)
    if not p:
        return f"**{pid}**"
    head = f"**{pid}**"
    who, yr = p.get("first_author"), p.get("year")
    if who and yr:
        head += f" · {_cell(str(who))} et al. ({yr})"
    elif who:
        head += f" · {_cell(str(who))} et al."
    title = (p.get("title") or "").strip()
    if not title:
        return head
    if len(title) > 80:
        title = title[:77].rstrip() + "…"
    return f"{head}<br>_{_cell(title)}_"


def _title_suffix(pool: dict, pid: str) -> str:
    """An inline ", Author et al. (year)" for running prose.

    _paper_ref() is built for a table cell — it embeds a <br> — so it shears a sentence
    in half if reused inline.
    """
    p = pool.get(pid) or {}
    who, yr = p.get("first_author"), p.get("year")
    if not who:
        return ""
    return f" ({_cell(str(who))} et al." + (f", {yr})" if yr else ")")


def _doi_cell(pool: dict, pid: str) -> str:
    url = (pool.get(pid) or {}).get("url")
    return f"[DOI ↗]({url})" if url else "—"


def _upload_cell(issue: int, pid: str, who: str) -> str:
    url = upload_url(issue, pid, who)
    return f"[**Upload ↗**]({url})" if url else "_link coming_"


# --- blocks -----------------------------------------------------------------
def holdings_table(claim: dict, pool: dict) -> str:
    """What the participant currently holds. Empty string if nothing is in flight."""
    issue, who = claim["issue"], claim["participant"]
    rows = []
    for pid, rec in claim["papers"].items():
        if rec["state"] not in state.IN_FLIGHT:
            continue
        if rec["state"] == "submitted":
            # The catalogue lookups are bound first, not inlined into the f-string:
            # nesting the same quote inside an f-string expression is a syntax error
            # before Python 3.12 (PEP 701) and this repo pins 3.10.
            nxt = prose.t("holdings.next_confirmed")
            rows.append(f"| {_paper_ref(pool, pid)} | {_doi_cell(pool, pid)} | — "
                        f"| {nxt} |")
            continue
        if rec["state"] == "pending":
            # No due date: the clock stopped when we received the file. Saying "—" and
            # naming the one action left is the whole message for this row.
            stopped = prose.t("holdings.clock_stopped")
            nxt = prose.t("holdings.next_sign_off", pid=pid)
            rows.append(f"| {_paper_ref(pool, pid)} | {_doi_cell(pool, pid)} "
                        f"| {stopped} | {nxt} |")
            continue
        due = rec["due_at"][:10]
        due_cell = f"**{due}**" + (" · _extended_" if rec.get("extended") else "")
        rows.append(f"| {_paper_ref(pool, pid)} | {_doi_cell(pool, pid)} | {due_cell} "
                    f"| {_upload_cell(issue, pid, who)} |")
    if not rows:
        return ""
    return prose.t("holdings.heading") + "\n" + "\n".join(rows)


def _cap_line(claim: dict, claims: dict) -> str:
    """The cap is per *participant*, not per thread.

    One participant routinely holds several papers across several claim issues (the
    form takes one claim at a time), so counting only this thread's papers would tell
    someone at 3/3 that they have room for two more.
    """
    who = claim["participant"]
    n = state.active_cap_count(claims, who)
    here = sum(1 for r in claim["papers"].values() if r["state"] in state.IN_FLIGHT)
    cap, elsewhere = params.ACTIVE_CLAIM_CAP, n - here
    note = (f" (counting {elsewhere} on your other claim "
            f"thread{'s' if elsewhere > 1 else ''})" if elsewhere > 0 else "")
    # blank line first: without it GitHub absorbs this into the table above as a
    # lazy continuation of the last row
    if n < cap:
        return "\n\n" + prose.t("cap.room", n=n, cap=cap, note=note, left=cap - n)
    # A submitted or pending paper still holds its slot (state.IN_FLIGHT), so "withdraw
    # one" is impossible advice for someone whose papers are all already with us.
    def _count(st):
        return sum(1 for c in claims.values() if c["participant"] == who
                   for r in c["papers"].values() if r["state"] == st)

    waiting, unconfirmed = _count("submitted"), _count("pending")
    bits = []
    if unconfirmed:
        # Actionable, so it leads: confirming is the one thing they can do right now.
        bits.append(prose.t("cap.waiting_confirm", n=unconfirmed,
                            verb="is" if unconfirmed == 1 else "are"))
    if waiting:
        bits.append(prose.t("cap.waiting_grading.one" if waiting == 1
                            else "cap.waiting_grading.many", n=waiting))
    tail = f" ({_prose_list(bits)})" if bits else ""
    return "\n\n" + prose.t("cap.full", n=n, cap=cap, note=note, tail=tail)


def _commands(extra_claim: bool = True, pending: bool = False) -> str:
    rows = [
        ("`/extend <ID>`", prose.t("commands.extend")),
        ("`/withdraw <ID>`", prose.t("commands.withdraw")),
    ]
    if pending:
        # Shown only while something is actually awaiting sign-off. `/decline` is
        # meaningless otherwise, and a permanent row inviting people to refuse an upload
        # they haven't made would be noise on every reply the bot posts.
        rows.insert(0, ("`/decline <ID> <why>`", prose.t("commands.decline")))
    if extra_claim:
        rows.append(("`/claim <ID>`", prose.t("commands.claim")))
    body = "\n".join(f"| {c} | {d} |" for c, d in rows)
    return prose.t("commands.heading") + "\n" + body


def _next_steps() -> str:
    return prose.t("next_steps", no_ai=_NO_AI)


def _confirm_attestation(pid: str) -> str:
    """What `/confirm` actually attests — the single source of that wording.

    Without this the ask was one table cell reading "⏳ `/confirm EEG-15` to sign it
    off", and nothing said what was being signed. The upload form already collects the
    no-AI declaration ("I declare that I read and annotated this paper myself, without
    AI assistance"), so from the claimant's side a bare `/confirm` reads as a redundant
    second click, and a signature nobody understands is a rubber stamp.

    What the reply adds is not a second declaration but an **authenticated** one: the
    form is open-access, its link is published in a public claim thread, and the `gh`
    field is a URL parameter anyone can edit. So the form's answer is attributable to
    whoever had the link, which is everyone. The GitHub reply is attributable to one
    account. Say that plainly rather than implying we disbelieve the first answer.
    """
    return prose.t("confirm_attestation", pid=pid)


def declined_note(reason_given: bool) -> str:
    """Appended when a claimant refuses an upload.

    Alleges nothing and asks nothing of them beyond the reason. "That wasn't me" and
    "that was me and I want to redo it" arrive through the same command, and the bot
    cannot tell which — so it must not imply either. An organizer reads the thread.
    """
    ask = "" if reason_given else "\n\n" + prose.t("decline.note_ask_why")
    return prose.t("decline.note") + ask


def _prose_list(items: list[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _deadline_footer() -> str:
    when = _prose_list([f"{d} day{'' if d == 1 else 's'}"
                        for d in sorted(params.REMIND_BEFORE_DAYS, reverse=True)])
    return prose.t("deadline_footer", when=when)


def _form_banner() -> str:
    return "" if params.SUBMISSION_FORM_URL else FORM_PENDING


# --- whole comments ---------------------------------------------------------
def claim_confirmation(claim: dict, pool: dict, outcome, claims: dict) -> str:
    """Reply to the claim form — the participant's full onboarding.

    The greeting tracks the outcome. A claim form can be answered with a pure refusal:
    the paper filled up while the form was open, the cap is already spent, or another
    thread won the same paper in a push race. Greeting all three with "you're in, your
    reading starts now" directly above "**Not applied:** ❌" makes the reply contradict
    itself in its first two lines. Spotted on #43 while reproducing issue #40 — the
    delta was correct by then and the banner above it still was not.
    """
    who = claim["participant"]
    greeting = prose.t("claim_confirmation.welcome" if outcome.ok
                       else "claim_confirmation.refused", who=who)
    parts = [greeting, outcome.delta()]
    table = holdings_table(claim, pool)
    if table:
        parts += [table + _cap_line(claim, claims), _form_banner(), _next_steps()]
    parts += [_commands(), _deadline_footer()]
    return "\n\n".join(p for p in parts if p.strip())


def command_ack(claim: dict, pool: dict, outcome, claims: dict,
                confirm_needed: list[str] | tuple[str, ...] = (),
                declined: list[str] | tuple[str, ...] = (),
                reason_given: bool = False) -> str:
    """Reply to any command comment: delta + where you stand.

    ``confirm_needed`` names the papers this command just moved into ``pending`` — i.e.
    the ones the claimant now has to sign off. Only the transition qualifies, so a
    re-upload against an already-pending paper is acknowledged without repeating the
    attestation at someone who has already read it.
    """
    who = claim["participant"]
    parts = [prose.t("command_ack.greeting", who=who), outcome.delta()]
    parts += [_confirm_attestation(pid) for pid in confirm_needed]
    if declined:
        parts.append(declined_note(reason_given))
    table = holdings_table(claim, pool)
    if table:
        parts += [prose.t("command_ack.holdings_heading") + "\n\n" + table
                  + _cap_line(claim, claims), _form_banner()]
    else:
        parts.append(prose.t("command_ack.holding_nothing"))
    # `/decline` is offered only while a paper is actually awaiting sign-off.
    parts.append(_commands(pending=any(r["state"] == "pending"
                                       for r in claim["papers"].values())))
    return "\n\n".join(p for p in parts if p.strip())


def reminder(who: str, pid: str, rec: dict, pool: dict, issue: int, days: int) -> str:
    url = upload_url(issue, pid, who)
    up = prose.t("reminder.upload_link", url=url) if url else prose.t("reminder.no_link_yet")
    ext = "" if rec.get("extended") else " " + prose.t("reminder.extend_offer", pid=pid)
    return prose.t("reminder", who=who, pid=pid, days=days,
                   due=rec["due_at"][:10], up=up, ext=ext)


def expiry(who: str, pid: str, rec: dict, pool: dict) -> str:
    return prose.t("expiry", who=who, pid=pid, due=rec["due_at"][:10])


def not_your_thread(actor: str, author: str) -> str:
    return prose.t("not_your_thread", actor=actor, author=author)


def not_allowed(actor: str, cmd: str, author: str) -> str:
    """Tier-2 ACL refusal — rendered as a rejected line, not a whole comment."""
    if cmd == "confirm":
        # The one refusal that is a feature. Say why, so it doesn't read as a bug:
        # an organizer confirming on someone's behalf would defeat the handshake.
        return prose.t("not_allowed.confirm", author=author)
    return prose.t("not_allowed.other", cmd=cmd)


# `confirm_request` lived here: a standalone version of the sign-off ask that nothing
# ever posted. Its wording is now `_confirm_attestation`, reached from command_ack (on
# the /received reply) and confirm_nudge. Two copies of participant-facing prose, one of
# them unreachable, is how the reachable one ends up wrong.


def reject_notice(who: str, pids: list[str]) -> str:
    """Posted when an organizer removes a review.

    This is a public comment naming a real person. It states the mechanical outcome and
    **alleges nothing** — the reason is the organizers' to give, in their own words and
    preferably in private. A bot must never be the thing that accuses someone.
    """
    lst = _prose_list([f"`{p}`" for p in pids])
    key = "reject_notice.one" if len(pids) == 1 else "reject_notice.many"
    return prose.t(key, who=who, papers=lst, followup=prose.t("reject_notice.followup"))


def confirm_nudge(who: str, pid: str, pool: dict, days: int) -> str:
    """The day-3 / day-7 reminder for an unconfirmed upload.

    Carries the attestation too, not just the command. Someone who acts on the nudge
    rather than on the original receipt would otherwise sign without ever being told
    what the signature means — and it is the nudge that reaches anyone whose receipt
    predates this wording.
    """
    return prose.t("confirm_nudge", who=who, pid=pid, days=days,
                   attestation=_confirm_attestation(pid))


def consent_missing() -> str:
    return prose.t("consent_missing")


def not_recorded() -> str:
    """Posted when the push exhausted its retries, so nothing at all was saved.

    The counterpart to the post-after-push ordering (issue #40): once announcements
    follow the push, a failed push means the participant hears nothing — and silence
    after filing a claim reads exactly like success. This says otherwise.
    """
    return prose.t("not_recorded")


def close_notice(author: str, held: list[str]) -> str:
    """Heads-up when someone closes a thread that still holds in-flight papers."""
    lst = ", ".join(f"`{p}`" for p in held)
    return prose.t("close_notice", author=author, papers=lst,
                   verb="is" if len(held) == 1 else "are")


def thread_done(claim: dict) -> str:
    """Posted with the auto-close when nothing on the thread needs the participant.

    Takes the claim because the honest ending depends on how the thread emptied: a
    withdrawal, an expiry or an organizer's removal leaves nothing to be scored, and
    promising that "your points will appear once graded" would be a small lie at a bad
    moment.
    """
    if any(r["state"] == "submitted" for r in claim["papers"].values()):
        return prose.t("thread_done.submitted")
    return prose.t("thread_done.nothing_submitted")


def thread_done_after_expiry() -> str:
    """Appended to the last expiry notice when that expiry empties the thread.

    Distinct from thread_done(): nothing here was submitted, so promising that
    'your points will appear once scored' would be a small lie at a bad moment.
    """
    return prose.t("thread_done_after_expiry")
