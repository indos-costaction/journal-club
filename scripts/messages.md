# Message catalogue

Every sentence the club says to a participant. **Edit the prose here**; no Python
knowledge needed, and a wording change shows up in the diff as a wording change.

- Each `## key` section is one message or block. The body is markdown, because the
  output is markdown — what you write is what lands on the thread.
- `{pid}`, `{who}` and friends are filled in by the code. Leave them alone unless you
  also change the call site; a placeholder nobody supplies raises an error rather than
  posting a half-rendered sentence.
- Every tunable in `params.py` is available by name — `{DEADLINE_DAYS}`,
  `{ACTIVE_CLAIM_CAP}`, `{EXTENSION_DAYS}`, `{SITE_URL}`, `{QUALITY_FLOOR}` — so a
  number never has to be typed twice.
- `scripts/messages.py` decides *which* block appears *when*, and builds the tables.
  This file decides what they say.

Two standing rules the tests enforce, both learned the hard way:

1. **Never tell someone that nothing will be held against them.** Saying it plants the
   idea that something could be. Withdrawing, missing a deadline and declining an
   upload are ordinary outcomes — state the outcome and what to do next.
2. **The bot alleges nothing.** It reports mechanics. Accusations are for organizers,
   in their own words, and preferably in private.

---

## claim.not_in_pool
`{raw}` is not a paper id in the pool.

## claim.already_held
`{pid}` — you already hold this paper.

## claim.paper_done
`{pid}` is complete (≥{COMPLETION_THRESHOLD} reviews) — pick another.

## claim.paper_closed
`{pid}` is closed ({live} claimants) — pick a paper still open.

## claim.at_cap
`{pid}` — you already hold {ACTIVE_CLAIM_CAP} active claims; withdraw one first.

## claim.ok
`{pid}` claimed — due **{due}**.

## withdraw.no_claim
`{pid}` — you have no active claim on this paper.

## withdraw.ok
`{pid}` returned to the pool. Slot freed.

## receive.no_such_paper
`{pid}` — this thread holds no claim on that paper.

## receive.wrong_state
`{pid}` — can't record an upload against a `{st}` claim.

## receive.was_declined
`{pid}` — response `{ref}` was declined by the claimant; it can't be recorded. A new upload gets a new id.

## receive.same_ref
`{pid}` — already recorded (ref `{ref}`); still waiting on your `/confirm {pid}`.

## receive.from_expired
`{pid}` — upload received and **accepted**; your claim is reinstated. Confirm it below and it counts in full.

## receive.replacing
`{pid}` — newer upload received; it replaces the earlier one. Still needs your confirmation below.

## receive.ok
`{pid}` — upload received. It needs your confirmation below.

## confirm.already
`{pid}` — already confirmed; it's with the organizers. Nothing to do.

## confirm.nothing_to_confirm
`{pid}` — nothing to confirm (we have no upload for it yet). Upload it first and we'll ask you here.

## confirm.ok
`{pid}` confirmed — thank you. It's with the organizers for grading.

## decline.already
`{pid}` — already declined; nothing more to do. Upload again whenever you're ready.

## decline.already_confirmed
`{pid}` — you already confirmed this one, so it's with the organizers. Ask them here and they can pull it back.

## decline.nothing_to_decline
`{pid}` — nothing to decline; we're not holding an upload for it.

## decline.ok
`{pid}` — understood, we won't grade that upload. The paper is still yours, due **{due}**.

## decline.note
An organizer will pick this up. The paper is still yours, and the file you declined won't be graded or re-attached — a new upload gets a new id.

## decline.note_ask_why
Could you add a line here about what happened? Anything is useful — "that wasn't my upload" and "I want to redo some comments" need very different things from us, and we can't tell which from the command alone.

## reject.already_freed
`{pid}` — already `{st}`; it counts for nothing already.

## reject.ok
`{pid}` has been withdrawn by the organizers and no longer counts. The paper is back in the pool.

## extend.clock_stopped
`{pid}` — no deadline left to extend; we already have your upload and the clock stopped.

## extend.no_active_claim
`{pid}` — no active claim to extend.

## extend.already_used
`{pid}` — you have already used your one-time extension. Withdraw it if you cannot finish.

## extend.ok
`{pid}` extended to **{due}** — this was your one-time +{EXTENSION_DAYS}-day extension.

## form_pending
> ⏳ **The upload form isn't live yet.** We'll comment here the moment it is, and we'll move your deadline to match — so start reading now.

## no_ai
**No AI** — it's the club's one hard rule, and AI-written reviews don't score.

## delta.accepted_heading
**Accepted:**

## delta.rejected_heading
**Not applied:**

## delta.nothing
_No recognised command found._

## holdings.heading
| Paper | Get the PDF | Due | Next step |
|---|---|---|---|

## holdings.next_confirmed
✅ confirmed — with us for grading

## holdings.next_sign_off
⏳ **`/confirm {pid}`** to sign it off

## holdings.clock_stopped
_clock stopped_

## cap.room
That's **{n} of {cap}** active{note} — room for {left} more.

## cap.full
That's **{n} of {cap}** active{note} — you'll need to finish or withdraw one before claiming another{tail}.

## cap.waiting_confirm
{n} {verb} waiting on your `/confirm`

## cap.waiting_grading
{n} {verb} with us for grading — {slots} up once scored

## commands.heading
### If your plans change

Comment on this thread:

| Comment | What happens |
|---|---|

## commands.decline
Won't sign off an upload — not yours, or you want to replace it.

## commands.extend
One-time **+{EXTENSION_DAYS} days**. Once per paper.

## commands.withdraw
Back to the pool — a better outcome than a rushed review.

## commands.claim
Take another paper, if you're under the cap.

## confirm_attestation
**Before `{pid}` can be graded**, reply **`/confirm {pid}`** on this thread to confirm that:

- this upload is yours, and
- you read and annotated the paper **yourself, without AI assistance**.

You answered that on the upload form too, but that form is open to anyone with the link — it can't tell who filled it in. Your reply here is signed by your GitHub account, so it's what actually puts your name on the review and on the no-AI declaration.

Didn't upload this, or want to replace it? **Don't confirm it** — reply **`/decline {pid}`** with a line saying why, and we won't grade it.

## deadline_footer
---

You get **{DEADLINE_DAYS} days** per paper, and we'll nudge you {when} before each deadline. Miss one and the paper returns to the pool, and you can claim it again whenever you like.

## claim_confirmation.welcome
👋 @{who} — you're in. **Your reading starts now.**

## claim_confirmation.refused
👋 @{who} — **this didn't go through.** Here's why:

## command_ack.greeting
@{who} —

## command_ack.holdings_heading
**On this thread you hold:**

## command_ack.holding_nothing
You're not holding any papers right now — [browse the pool]({SITE_URL}#papers) whenever you like.

## reminder
⏰ @{who} your claim on `{pid}` is due in ~{days} day(s) (**{due}**). Done reading? {up}.{ext} Not going to finish? `/withdraw {pid}` returns it, which is a perfectly good outcome.

## reminder.upload_link
[**upload it here**]({url})

## reminder.no_link_yet
we'll post your upload link here as soon as the form is live

## reminder.extend_offer
Need longer? `/extend {pid}` buys a one-time +{EXTENSION_DAYS} days.

## expiry
⌛ @{who} your claim on `{pid}` reached its deadline ({due}) and returned to the pool. It's open again if you'd still like it: `/claim {pid}`.

## confirm_nudge
👋 @{who} your annotated PDF for `{pid}` has been sitting with us for ~{days} day(s), waiting on your sign-off.

{attestation}

There's no deadline on this one — the clock stopped when your file arrived. But it won't be graded, and it holds one of your {ACTIVE_CLAIM_CAP} slots, until you sign it off.

## not_your_thread
@{actor} only the thread's owner (@{author}) or an organizer can run claim commands here.

## not_allowed.confirm
`/confirm` — only @{author} can confirm their own review. Not even organizers can do this on someone's behalf; that's the point of it.

## not_allowed.other
`/{cmd}` — organizers only.

## reject_notice.one
@{who} — {papers} has been withdrawn by the organizers and no longer counts toward the leaderboard. The paper is back in the pool and your slot is free.

{followup}

## reject_notice.many
@{who} — {papers} have been withdrawn by the organizers and no longer count toward the leaderboard. The papers are back in the pool and your slots are free.

{followup}

## reject_notice.followup
The organizers will follow up with the reason. If you think this is a mistake, reply here or contact them directly — it can be reversed.

## consent_missing
❌ We can't record a claim without the consent checkbox ticked. Please edit the issue and confirm consent (see [`CONSENT.md`](https://github.com/indos-costaction/journal-club/blob/main/CONSENT.md)) — we'll pick it up automatically.

## not_recorded
⚠️ Something went wrong on our side and **nothing was recorded** — your claims and deadlines are exactly as they were before this. Please post the same command again in a few minutes; if it fails a second time the organizers will pick it up from the run log.

## close_notice
ℹ️ @{author} closing this issue does **not** release your claims — {papers} {verb} still active and the {DEADLINE_DAYS}-day deadline keeps running. To return a paper, comment `/withdraw <ID>`. Reopen this issue to keep working.

## thread_done.submitted
Nothing on this thread needs your action — closing it. Your confirmed reviews are with the organizers for grading, and your points will appear on the leaderboard once they're scored. Open a new claim whenever you like → {SITE_URL}#papers

## thread_done.nothing_submitted
Nothing on this thread needs your action — closing it. Claim again whenever suits you → {SITE_URL}#papers

## thread_done_after_expiry
Nothing else on this thread needs your action — closing it. The paper is back in the pool; claim it, or any other, whenever suits you → {SITE_URL}#papers

## next_steps
### What to do

1. **Get each PDF** through your institution's library access — we can't host them
   (copyright). No access to one? Just say so here and we'll sort it out.
2. **Read and annotate it yourself.** Typed inline comments in the PDF, spread across the
   whole paper — methods and results, not just the intro. Mark what you didn't understand,
   what's been contested or superseded, and what matters for INDoS. {no_ai}
   → [How to read a paper]({SITE_URL}reading.html)
3. **Upload it** with that paper's Upload link above. It already knows which paper is yours.
4. **Sign it off here.** Once we have your file we'll @-mention you on this thread; reply
   `/confirm <ID>` and it goes to grading.

**Why the sign-off?** The upload form is a public link, so on its own it can't prove *who*
sent a file. Your `/confirm` here is what puts your name on the review — and on the no-AI
declaration. It's one comment, and we ask for it; you don't have to remember it.

Then we grade it against the [rubric]({SITE_URL}participate.html#how-to-review-a-paper).
