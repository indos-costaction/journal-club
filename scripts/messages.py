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
import state

# Rendered instead of an upload link while SUBMISSION_FORM_URL is unset, so a
# half-built pathway degrades to an honest promise rather than a dead link.
FORM_PENDING = (
    "> ⏳ **The upload form isn't live yet.** We'll comment here the moment it is, and your "
    "deadline won't be held against you in the meantime — so start reading now."
)

_NO_AI = (
    "**No AI** — it's the club's one hard rule, and AI-written reviews don't score."
)


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
            rows.append(f"| {_paper_ref(pool, pid)} | {_doi_cell(pool, pid)} | — "
                        f"| ✅ confirmed — with us for grading |")
            continue
        if rec["state"] == "pending":
            # No due date: the clock stopped when we received the file. Saying "—" and
            # naming the one action left is the whole message for this row.
            rows.append(f"| {_paper_ref(pool, pid)} | {_doi_cell(pool, pid)} | _clock stopped_ "
                        f"| ⏳ **`/confirm {pid}`** to sign it off |")
            continue
        due = rec["due_at"][:10]
        due_cell = f"**{due}**" + (" · _extended_" if rec.get("extended") else "")
        rows.append(f"| {_paper_ref(pool, pid)} | {_doi_cell(pool, pid)} | {due_cell} "
                    f"| {_upload_cell(issue, pid, who)} |")
    if not rows:
        return ""
    return ("| Paper | Get the PDF | Due | Next step |\n|---|---|---|---|\n"
            + "\n".join(rows))


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
        return f"\n\nThat's **{n} of {cap}** active{note} — room for {cap - n} more."
    # A submitted or pending paper still holds its slot (state.IN_FLIGHT), so "withdraw
    # one" is impossible advice for someone whose papers are all already with us.
    def _count(st):
        return sum(1 for c in claims.values() if c["participant"] == who
                   for r in c["papers"].values() if r["state"] == st)

    waiting, unconfirmed = _count("submitted"), _count("pending")
    bits = []
    if unconfirmed:
        # Actionable, so it leads: confirming is the one thing they can do right now.
        bits.append(f"{unconfirmed} {'is' if unconfirmed == 1 else 'are'} waiting on your "
                    f"`/confirm`")
    if waiting:
        bits.append(f"{waiting} {'is' if waiting == 1 else 'are'} with us for grading — "
                    f"{'that slot frees' if waiting == 1 else 'those slots free'} up once scored")
    tail = f" ({_prose_list(bits)})" if bits else ""
    return (f"\n\nThat's **{n} of {cap}** active{note} — you'll need to finish or withdraw one "
            f"before claiming another{tail}.")


def _commands(extra_claim: bool = True) -> str:
    rows = [
        ("`/extend <ID>`", f"One-time **+{params.EXTENSION_DAYS} days**. Once per paper."),
        ("`/withdraw <ID>`", "Back to the pool. **No penalty** — far better than a rushed review."),
    ]
    if extra_claim:
        rows.append(("`/claim <ID>`", "Take another paper, if you're under the cap."))
    body = "\n".join(f"| {c} | {d} |" for c, d in rows)
    return ("### If your plans change\n\nComment on this thread:\n\n"
            "| Comment | What happens |\n|---|---|\n" + body)


def _next_steps() -> str:
    return f"""### What to do

1. **Get each PDF** through your institution's library access — we can't host them
   (copyright). No access to one? Just say so here and we'll sort it out.
2. **Read and annotate it yourself.** Typed inline comments in the PDF, spread across the
   whole paper — methods and results, not just the intro. Mark what you didn't understand,
   what's been contested or superseded, and what matters for INDoS. {_NO_AI}
   → [How to read a paper]({params.SITE_URL}reading.html)
3. **Upload it** with that paper's Upload link above. It already knows which paper is yours.
4. **Sign it off here.** Once we have your file we'll @-mention you on this thread; reply
   `/confirm <ID>` and it goes to grading.

**Why the sign-off?** The upload form is a public link, so on its own it can't prove *who*
sent a file. Your `/confirm` here is what puts your name on the review — and on the no-AI
declaration. It's one comment, and we ask for it; you don't have to remember it.

Then we grade it against the [rubric]({params.SITE_URL}participate.html#how-to-review-a-paper)."""


def _prose_list(items: list[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _deadline_footer() -> str:
    when = _prose_list([f"{d} day{'' if d == 1 else 's'}"
                        for d in sorted(params.REMIND_BEFORE_DAYS, reverse=True)])
    return (f"---\n\nYou get **{params.DEADLINE_DAYS} days** per paper, and we'll nudge you "
            f"{when} before each deadline. Miss one and the paper simply returns to the pool "
            f"— **no penalty**, and you're welcome to claim it again.")


def _form_banner() -> str:
    return "" if params.SUBMISSION_FORM_URL else FORM_PENDING


# --- whole comments ---------------------------------------------------------
def claim_confirmation(claim: dict, pool: dict, outcome, claims: dict) -> str:
    """Reply to the claim form — the participant's full onboarding."""
    who = claim["participant"]
    parts = [f"👋 @{who} — you're in. **Your reading starts now.**", outcome.delta()]
    table = holdings_table(claim, pool)
    if table:
        parts += [table + _cap_line(claim, claims), _form_banner(), _next_steps()]
    parts += [_commands(), _deadline_footer()]
    return "\n\n".join(p for p in parts if p.strip())


def command_ack(claim: dict, pool: dict, outcome, claims: dict) -> str:
    """Reply to any command comment: delta + where you stand."""
    who = claim["participant"]
    parts = [f"@{who} —", outcome.delta()]
    table = holdings_table(claim, pool)
    if table:
        parts += [f"**On this thread you hold:**\n\n{table}" + _cap_line(claim, claims),
                  _form_banner()]
    else:
        parts.append("You're not holding any papers right now — "
                     f"[browse the pool]({params.SITE_URL}#papers) whenever you like.")
    parts.append(_commands())
    return "\n\n".join(p for p in parts if p.strip())


def reminder(who: str, pid: str, rec: dict, pool: dict, issue: int, days: int) -> str:
    url = upload_url(issue, pid, who)
    up = (f"[**upload it here**]({url})" if url
          else "we'll post your upload link here as soon as the form is live")
    ext = ("" if rec.get("extended")
           else f" Need longer? `/extend {pid}` buys a one-time "
                f"+{params.EXTENSION_DAYS} days.")
    return (f"⏰ @{who} your claim on `{pid}` is due in ~{days} day(s) "
            f"(**{rec['due_at'][:10]}**). Done reading? {up}.{ext} "
            f"Not going to finish? `/withdraw {pid}` returns it — no penalty, and that's a "
            f"perfectly good outcome.")


def expiry(who: str, pid: str, rec: dict, pool: dict) -> str:
    return (f"⌛ @{who} your claim on `{pid}` reached its deadline ({rec['due_at'][:10]}) and "
            f"returned to the pool — **no penalty**, nothing held against you. It's open again "
            f"if you'd still like it: `/claim {pid}`.")


def not_your_thread(actor: str, author: str) -> str:
    return (f"@{actor} only the thread's owner (@{author}) or an organizer can run claim "
            f"commands here.")


def not_allowed(actor: str, cmd: str, author: str) -> str:
    """Tier-2 ACL refusal — rendered as a rejected line, not a whole comment."""
    if cmd == "confirm":
        # The one refusal that is a feature. Say why, so it doesn't read as a bug:
        # an organizer confirming on someone's behalf would defeat the handshake.
        return (f"`/confirm` — only @{author} can confirm their own review. Not even "
                f"organizers can do this on someone's behalf; that's the point of it.")
    return f"`/{cmd}` — organizers only."


def confirm_request(claim: dict, pool: dict, pid: str) -> str:
    """Posted when an upload lands: the sign-off we need before it counts.

    This is where the no-AI declaration acquires an authenticated author, so it has to
    restate what is being attested — a `/confirm` that doesn't say what it means would
    be a rubber stamp, and we'd have gained nothing over the anonymous form.
    """
    who = claim["participant"]
    return (f"📄 @{who} — we've got your annotated PDF for **{pid}**{_title_suffix(pool, pid)}. "
            f"One last thing before it goes to grading.\n\n"
            f"Reply **`/confirm {pid}`** on this thread to confirm that:\n\n"
            f"- this upload is yours, and\n"
            f"- you read and annotated the paper **yourself, without AI assistance**.\n\n"
            f"That's your signature on the review — the upload link is public, so your "
            f"reply here is what puts your name on it. Nothing else is needed.\n\n"
            f"Didn't upload this? **Don't confirm it** — just say so here and we'll drop it. "
            f"Uploaded the wrong file? Upload the right one and we'll ask again.\n\n"
            f"_Your deadline is on hold until you confirm — no rush, nothing is at risk._")


def reject_notice(who: str, pids: list[str]) -> str:
    """Posted when an organizer removes a review.

    This is a public comment naming a real person. It states the mechanical outcome and
    **alleges nothing** — the reason is the organizers' to give, in their own words and
    preferably in private. A bot must never be the thing that accuses someone.
    """
    lst = _prose_list([f"`{p}`" for p in pids])
    n = len(pids)
    return (f"@{who} — {lst} {'has' if n == 1 else 'have'} been withdrawn by the organizers "
            f"and {'no longer counts' if n == 1 else 'no longer count'} toward the "
            f"leaderboard. {'The paper is' if n == 1 else 'The papers are'} back in the pool "
            f"and your {'slot is' if n == 1 else 'slots are'} free.\n\n"
            f"The organizers will follow up with the reason. If you think this is a mistake, "
            f"reply here or contact them directly — it can be reversed.")


def confirm_nudge(who: str, pid: str, pool: dict, days: int) -> str:
    return (f"👋 @{who} your annotated PDF for `{pid}` has been sitting with us for "
            f"~{days} day(s), waiting on your sign-off. Reply **`/confirm {pid}`** and it "
            f"goes to grading.\n\n"
            f"Nothing is at risk — there's no deadline on this and the work is safe. But it "
            f"won't be graded, and it holds one of your {params.ACTIVE_CLAIM_CAP} slots, "
            f"until you do.\n\n"
            f"Didn't upload it? Say so here rather than confirming it.")


def consent_missing() -> str:
    return ("❌ We can't record a claim without the consent checkbox ticked. Please edit the "
            "issue and confirm consent (see [`CONSENT.md`](https://github.com/"
            "indos-costaction/journal-club/blob/main/CONSENT.md)) — we'll pick it up "
            "automatically.")


def close_notice(author: str, held: list[str]) -> str:
    """Heads-up when someone closes a thread that still holds in-flight papers."""
    lst = ", ".join(f"`{p}`" for p in held)
    verb = "is" if len(held) == 1 else "are"
    return (f"ℹ️ @{author} closing this issue does **not** release your claims — {lst} {verb} "
            f"still active and the {params.DEADLINE_DAYS}-day deadline keeps running. To return "
            f"a paper, comment `/withdraw <ID>`. Reopen this issue to keep working.")


def thread_done(claim: dict) -> str:
    """Posted with the auto-close when nothing on the thread needs the participant.

    Takes the claim because the honest ending depends on how the thread emptied: a
    withdrawal, an expiry or an organizer's removal leaves nothing to be scored, and
    promising that "your points will appear once graded" would be a small lie at a bad
    moment.
    """
    if any(r["state"] == "submitted" for r in claim["papers"].values()):
        return ("Nothing on this thread needs your action — closing it. Your confirmed reviews "
                "are with the organizers for grading, and your points will appear on the "
                f"leaderboard once they're scored. Open a new claim whenever you like → "
                f"{params.SITE_URL}#papers")
    return ("Nothing on this thread needs your action — closing it. Nothing is held against "
            f"you, and you're welcome to claim again whenever suits you → {params.SITE_URL}#papers")


def thread_done_after_expiry() -> str:
    """Appended to the last expiry notice when that expiry empties the thread.

    Distinct from thread_done(): nothing here was submitted, so promising that
    'your points will appear once scored' would be a small lie at a bad moment.
    """
    return (f"Nothing else on this thread needs your action — closing it. No hard feelings and "
            f"nothing lost: the paper is back in the pool, and you're welcome to claim it, or "
            f"any other, whenever suits you → {params.SITE_URL}#papers")
