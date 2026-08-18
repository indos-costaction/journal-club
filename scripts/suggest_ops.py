#!/usr/bin/env python3
"""Paper suggestions: triage one on arrival, and apply an organizer's ``/accept-paper``.

The claim engine's sibling, and deliberately a separate one. ``issue_ops.py`` moves *claim*
state and knows nothing about the pool; this moves the *pool* and knows nothing about
claims. They are kept apart by a label filter in the workflow YAML, not by convention:
``issue-ops.yml`` had no label condition at all (the check lives in ``issue_ops.py``), so a
suggestion thread used to start the claim engine, and ``_detect_consent`` greps ``- [x]``
across the whole body. That is how #26 answered a bug report with a consent rejection.

Two entrypoints, one per event:

``triage``  an issue is opened or edited -> one verdict per identifier, no state written.
``accept``  an organizer comments ``/accept-paper`` -> papers appended to the pool.

**Triage must survive having no network.** Whether a DOI is already in the pool, retired, or
previously declined are lookups on the identifier string itself, so they are pure and local.
Crossref only supplies the human-readable echo. When it is unreachable the verdicts still
land and the reply says resolution was unavailable — the alternative is a bot that goes
silent exactly when someone is waiting for it, or worse, one that reports "not in the pool"
as though that were the whole answer.

**Triage never says a paper is in scope.** ``params.in_scope`` only catches records with no
bibliographic identity at all; a textbook entering through a review of the book looks like an
ordinary article (see EEG-04, and the note under ``params.RETIRED``). Scope is a human call.

**Accept refuses rather than half-adds.** A pool record with no title is worse than no
record, so a failed lookup fails the command and says to retry.

Accepting is idempotent because it has to be: ``suggest-ops.yml`` carries the rebase-retry
loop, which re-runs this script rather than replaying a diff. The pool is keyed by DOI, so a
second apply finds the paper present and reports it as already added.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import params
import prose
import state

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
COMMENT_FILE = REPO / "comment.md"
ACTIONS_FILE = REPO / "actions.json"

SUGGESTION_LABEL = "paper-suggestion"

# `/accept-paper [<doi>] [<modality>]`. Not in issue_ops.CMD_RE, and it cannot be: that
# regex is `/(claim|withdraw|...)\s+` and requires an argument, so `/accept-paper` on its
# own would not match even if the verb were listed. The label filter in the workflow is
# what actually keeps the two engines apart; this is the second lock on the same door.
ACCEPT_RE = re.compile(r"^\s*/accept-paper\b([^\n]*)", re.IGNORECASE | re.MULTILINE)

# One identifier per line, in any of the shapes people actually paste.
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:a-z0-9<>\[\]+]+)", re.IGNORECASE)
PMID_RE = re.compile(r"\bPMID:?\s*(\d{6,9})\b", re.IGNORECASE)
# Trailing punctuation a DOI never ends in but a sentence does. `.` and `,` are the common
# ones; `)` only when unbalanced, since DOIs legitimately contain paired brackets.
_TRAILING = ".,;:'\"”’>"


def normalise_doi(raw: str) -> str:
    """Any way someone might write a DOI, reduced to the one form we compare on.

    Resolver prefixes, wrapping brackets and sentence punctuation all go; the trailing
    strip has to respect balance, because DOIs legitimately contain paired brackets
    (10.1002/(sici)…) and a blind rstrip would corrupt them.
    """
    text = (raw or "").strip()
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", text, flags=re.IGNORECASE)
    m = DOI_RE.search(text)          # also drops anything wrapping it, e.g. a leading "("
    doi = (m.group(1) if m else text).strip().rstrip(_TRAILING)
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1].rstrip(_TRAILING)
    return doi.lower()


@dataclass(frozen=True)
class Identifier:
    """One thing the proposer asked us to look at."""
    raw: str                      # the line as typed, for quoting back
    doi: str = ""                 # normalised, or "" when we only have a PMID
    pmid: str = ""

    @property
    def key(self) -> str:
        return self.doi or (f"pmid:{self.pmid}" if self.pmid else self.raw.strip().lower())

    @property
    def resolvable(self) -> bool:
        return bool(self.doi or self.pmid)


def _papers_section(body: str) -> str:
    """The form's Papers field, or the whole body when this was filed by hand.

    GitHub renders a form as `### <label>` blocks and the field ids are gone by the time
    we see it, so the heading is the only handle. Scoping matters more than it looks: the
    dropdown renders its value as a line ("not sure") and an empty optional field renders
    as "_No response_", and both are short enough to pass for something someone typed.
    Reading the whole body reported them as unresolvable identifiers, which made a
    perfectly good suggestion come back looking like it had three errors in it.
    """
    m = re.search(r"^###\s*Papers\s*$(.*?)(?=^###\s|\Z)", body or "",
                  re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else (body or "")


def parse_identifiers(body: str) -> list[Identifier]:
    """One Identifier per line that carries something we can act on, in order, deduped.

    Lines with neither a DOI nor a PMID are kept (as unresolvable) rather than dropped: a
    proposer who pasted a bare title deserves "I could not resolve that", not silence.
    Prose is dropped by length, which is a heuristic and is why the section scoping above
    does the real work.
    """
    out, seen = [], set()
    for line in _papers_section(body).splitlines():
        line = line.strip().lstrip("-*• \t")
        if not line or line.startswith("#") or line.startswith(">"):
            continue
        doi = DOI_RE.search(line)
        pmid = PMID_RE.search(line)
        if not doi and not pmid and len(line.split()) > 6:
            continue  # a sentence, not an identifier
        ident = Identifier(raw=line,
                           doi=normalise_doi(doi.group(1)) if doi else "",
                           pmid=pmid.group(1) if pmid else "")
        if ident.key in seen:
            continue
        seen.add(ident.key)
        out.append(ident)
    return out


def parse_modality(raw: str | None) -> str | None:
    """Match a modality name case-insensitively; None when it is absent or 'not sure'."""
    want = (raw or "").strip().lower()
    if not want or want in ("not sure", "unsure", "none", "_no response_"):
        return None
    for mod in params.MODALITY_ORDER:
        if want == mod.lower() or want == params.MODALITY_PREFIX[mod].lower():
            return mod
    return None


# --- verdicts (pure) --------------------------------------------------------
@dataclass
class Verdict:
    ident: Identifier
    kind: str                     # known | retired | declined | new
                                  # | unresolvable | unknown_id
    detail: str = ""              # a pool id, a reason, or a citation
    meta: dict = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        """Would /accept-paper do anything with this one?"""
        return self.kind == "new"


def _pool_by_doi(pool: dict) -> dict[str, str]:
    return {normalise_doi(rec.get("doi") or ""): pid
            for pid, rec in pool.items() if (rec.get("doi") or "").strip()}


def verdict_for(ident: Identifier, pool: dict, fetch=None) -> Verdict:
    """Classify one identifier. Pure but for ``fetch``, which may be absent or may fail.

    Order matters: the three local checks run first and are never skipped, so a network
    failure downgrades the *detail* of a verdict and never its correctness.
    """
    if ident.doi:
        by_doi = _pool_by_doi(pool)
        if ident.doi in by_doi:
            return Verdict(ident, "known", by_doi[ident.doi])
        why = params.DECLINED.get(ident.doi)
        if why:
            return Verdict(ident, "declined", why)
    if not ident.resolvable:
        return Verdict(ident, "unresolvable")

    meta, missing = {}, False
    if fetch is not None:
        try:
            meta = fetch(ident) or {}
        except LookupError as exc:
            # The identifier resolved to nothing — a typo, or a DOI that was never
            # registered. Distinct from an outage on purpose: "we could not reach the
            # metadata service" sends someone away to wait when what they need is to
            # check what they pasted.
            print(f"::warning::no such identifier {ident.key}: {exc}")
            missing = True
        except Exception as exc:                      # noqa: BLE001 — any failure degrades
            print(f"::warning::lookup failed for {ident.key}: {exc}")
            meta = {}
    # A PMID that resolved to a DOI can still be a paper we already hold.
    got = normalise_doi(meta.get("doi") or "")
    if got and got != ident.doi:
        ident = Identifier(raw=ident.raw, doi=got, pmid=ident.pmid)
        if got in _pool_by_doi(pool):
            return Verdict(ident, "known", _pool_by_doi(pool)[got])
        if got in params.DECLINED:
            return Verdict(ident, "declined", params.DECLINED[got])
    if missing:
        return Verdict(ident, "unknown_id")
    if not meta:
        return Verdict(ident, "new", "", {})
    cite = " · ".join(str(x) for x in (meta.get("title"), meta.get("venue"), meta.get("year")) if x)
    return Verdict(ident, "new", cite, meta)


def retired_note(pool_id: str) -> str:
    return params.RETIRED.get(pool_id, "")


# --- metadata lookup (the only network in this repo) ------------------------
_last_call = 0.0


def _get(url: str, *, retries: int = 3, timeout: int = 20) -> dict:
    """GET one JSON document, with a courtesy delay and backoff on the retryable codes.

    Stdlib urllib, because the repo has no third-party dependencies and is not about to
    grow one for three fields. A polite User-Agent with a contact address is what Crossref
    asks for in exchange for the fast pool.
    """
    global _last_call
    req = urllib.request.Request(url, headers={
        "User-Agent": "indos-journal-club/1.0 (https://www.indos-costaction.eu/journal-club/)",
        "Accept": "application/json",
    })
    delay = 2.0
    for attempt in range(retries + 1):
        gap = time.monotonic() - _last_call
        if gap < 0.3:
            time.sleep(0.3 - gap)
        _last_call = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LookupError(f"not found: {url}") from None
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def _crossref(doi: str) -> dict:
    m = _get(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")["message"]
    author = (m.get("author") or [{}])[0]
    name = " ".join(x for x in (author.get("given"), author.get("family")) if x).strip()
    return {
        "doi": normalise_doi(m.get("DOI") or doi),
        "title": (m.get("title") or [""])[0].strip(),
        "first_author": name,
        "year": (m.get("issued", {}).get("date-parts") or [[None]])[0][0],
        "venue": (m.get("container-title") or [""])[0],
    }


def _pubmed_doi(pmid: str) -> str:
    """PMID -> DOI. Everything else comes from Crossref, which has the better metadata."""
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
           f"?db=pubmed&retmode=json&id={urllib.parse.quote(pmid)}")
    rec = (_get(url).get("result") or {}).get(pmid) or {}
    for a in rec.get("articleids") or []:
        if (a.get("idtype") or "").lower() == "doi":
            return normalise_doi(a.get("value") or "")
    return ""


def _semantic_scholar(doi: str) -> dict:
    """s2_id and citation_count, best effort. A 2026 paper may simply not be indexed."""
    try:
        url = ("https://api.semanticscholar.org/graph/v1/paper/DOI:"
               f"{urllib.parse.quote(doi)}?fields=paperId,citationCount")
        got = _get(url, retries=1)
        return {"s2_id": got.get("paperId"), "citation_count": got.get("citationCount")}
    except Exception:                                 # noqa: BLE001 — never blocks an accept
        return {"s2_id": None, "citation_count": None}


def lookup(ident: Identifier) -> dict:
    """Resolve one identifier to the fields a pool record needs. Raises on failure."""
    doi = ident.doi or (_pubmed_doi(ident.pmid) if ident.pmid else "")
    if not doi:
        raise LookupError(f"no DOI for {ident.raw!r}")
    meta = _crossref(doi)
    meta.update(_semantic_scholar(doi))
    return meta


# --- the pool ---------------------------------------------------------------
def next_id(pool: dict, modality: str) -> str:
    """The next free id in this modality: max + 1, never a gap.

    The gaps ARE the record. ``params.RETIRED`` ids appear in claim records, issue threads
    and the ledger, so filling one would silently point an old reference at a new paper.
    Seed ids (``EEG-R1``) are a separate series and are not counted here.
    """
    prefix = params.MODALITY_PREFIX[modality]
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    used = [int(m.group(1)) for pid in pool if (m := pat.match(pid))]
    used += [int(m.group(1)) for pid in params.RETIRED if (m := pat.match(pid))]
    return f"{prefix}-{(max(used) + 1) if used else 1:02d}"


def pool_record(pool_id: str, modality: str, meta: dict) -> dict:
    """The 11-key published shape. Mirrors seed_pool.record(), which cannot be reused here:
    it requires an S2 ``paperId`` and would KeyError on a paper S2 has never seen."""
    doi = normalise_doi(meta.get("doi") or "")
    return {
        "id": pool_id,
        "modality": modality,
        "level": 1,                      # level 0 is the seed-review series only
        "title": (meta.get("title") or "").strip(),
        "first_author": meta.get("first_author") or "",
        "year": meta.get("year"),
        "doi": doi or None,
        "url": f"https://doi.org/{doi}" if doi else "",
        "venue": meta.get("venue") or "",
        "s2_id": meta.get("s2_id"),
        "citation_count": meta.get("citation_count"),
    }


def insert(pool_list: list[dict], rec: dict) -> list[dict]:
    """Append within the record's modality block, which is the order the site renders in."""
    last = max((i for i, p in enumerate(pool_list) if p["modality"] == rec["modality"]),
               default=None)
    if last is None:
        return pool_list + [rec]
    return pool_list[:last + 1] + [rec] + pool_list[last + 1:]


# --- event handling ---------------------------------------------------------
def _labels(issue: dict) -> set[str]:
    return {(lb.get("name") or "").lower() for lb in (issue.get("labels") or [])}


def _empty(issue=None) -> dict:
    return {"comment": "", "add_labels": [], "issue": issue, "changed": False,
            "close": False, "close_comment": ""}


def triage(body: str, pool: dict, fetch=None) -> list[Verdict]:
    return [verdict_for(i, pool, fetch) for i in parse_identifiers(body)]


def accept(argline: str, verdicts: list[Verdict], declared: str | None,
           pool_list: list[dict], fetch=lookup) -> tuple[state.Outcome, list[dict]]:
    """Apply one ``/accept-paper``. Returns the outcome and the new pool list.

    ``argline`` is everything after the verb: an optional DOI to single one out, and an
    optional modality that overrides what the proposer declared. Order-independent, because
    telling people which comes first is a rule nobody should have to remember.
    """
    args = [a for a in re.split(r"[\s,]+", (argline or "").strip()) if a]
    target = next((normalise_doi(a) for a in args if DOI_RE.search(a)), "")
    override = next((m for a in args if (m := parse_modality(a))), None)
    # A word that is neither a DOI nor a modality is a typo, not a filter: saying so beats
    # silently accepting everything because "EGG" matched nothing.
    unknown = [a for a in args if not DOI_RE.search(a) and not parse_modality(a)]

    out = state.Outcome()
    pool_list = list(pool_list)
    if unknown:
        out.reject(prose.t("accept.unknown_argument", args=", ".join(f"`{a}`" for a in unknown),
                           modalities=", ".join(f"`{m}`" for m in params.MODALITY_ORDER)))
        return out, pool_list

    wanted = [v for v in verdicts if not target or v.ident.doi == target]
    if target and not wanted:
        out.reject(prose.t("accept.not_on_thread", doi=target))
        return out, pool_list
    if not wanted:
        out.reject(prose.t("accept.nothing_to_do"))
        return out, pool_list

    for v in wanted:
        if v.kind == "known":
            # An accept, not a refusal: the organizer asked for an end state that already
            # holds. Same shape as apply_confirm on an already-submitted paper. It is also
            # what makes the rebase-retry loop safe — the re-run sees the paper it just
            # added and must report success, not "not applied".
            out.accept(prose.t("accept.already_added", raw=v.ident.raw, pid=v.detail))
            continue
        if v.kind == "retired":
            out.reject(prose.t("accept.retired", raw=v.ident.raw, pid=v.detail))
            continue
        if v.kind == "declined":
            out.reject(prose.t("accept.previously_declined", raw=v.ident.raw, why=v.detail))
            continue
        if v.kind in ("unresolvable", "unknown_id"):
            out.reject(prose.t("accept.unresolvable", raw=v.ident.raw))
            continue

        modality = override or declared
        if not modality:
            out.reject(prose.t("accept.no_modality", raw=v.ident.raw,
                               modalities=", ".join(f"`{m}`" for m in params.MODALITY_ORDER)))
            continue

        try:
            meta = v.meta or fetch(v.ident)
        except Exception as exc:                      # noqa: BLE001
            # Refuse rather than write a record with no title: the id would be permanent
            # and the fix would be a retirement, not an edit.
            print(f"::warning::lookup failed for {v.ident.key}: {exc}")
            out.reject(prose.t("accept.lookup_failed", raw=v.ident.raw))
            continue
        if not (meta.get("title") or "").strip():
            out.reject(prose.t("accept.lookup_failed", raw=v.ident.raw))
            continue

        doi = normalise_doi(meta.get("doi") or v.ident.doi)
        already = _pool_by_doi({p["id"]: p for p in pool_list}).get(doi)
        if already:
            # The rebase-retry loop re-runs this script, so a second apply must be a no-op
            # that still reports the truth rather than a duplicate append.
            out.accept(prose.t("accept.already_added", raw=v.ident.raw, pid=already))
            continue

        pid = next_id({p["id"]: p for p in pool_list}, modality)
        rec = pool_record(pid, modality, meta)
        ok, why = params.in_scope(rec)
        if not ok:
            out.reject(prose.t("accept.out_of_scope", raw=v.ident.raw, why=why))
            continue
        pool_list = insert(pool_list, rec)
        out.accept(prose.t("accept.added", pid=pid, title=rec["title"],
                           modality=modality, url=rec["url"]))
    return out, pool_list


def declared_modality(body: str) -> str | None:
    """The modality the proposer picked in the form's dropdown, if any.

    Read out of the rendered issue body rather than by field id, the way
    ``issue_ops._detect_attribution`` does: GitHub renders a form as markdown headings and
    the field ids are gone by the time we see it.
    """
    m = re.search(r"###\s*Modality\s*\n+([^\n]+)", body or "", re.IGNORECASE)
    return parse_modality(m.group(1)) if m else None


def handle_event(event: dict, fetch=None, organizers: set[str] | None = None,
                 pool_list: list[dict] | None = None) -> dict:
    """One GitHub event to one intent. Writes nothing; ``main`` persists the artifacts."""
    import issue_ops   # imported here so a missing ORGANIZERS cannot break module import

    organizers = issue_ops.ORGANIZERS if organizers is None else organizers
    name = event.get("event_name")
    issue = (event.get("issue") or {}).get("number")

    if name == "issues":
        if SUGGESTION_LABEL not in _labels(event["issue"]):
            return _empty(issue)
        body = event["issue"].get("body") or ""
        verdicts = triage(body, state.load_pool(), fetch)
        if not verdicts:
            return {**_empty(issue), "comment": prose.t("suggest.nothing_found")}
        dupes = all(v.kind in ("known", "retired", "declined") for v in verdicts)
        return {**_empty(issue),
                "comment": render_triage(verdicts),
                "add_labels": ["duplicate"] if dupes else []}

    if name == "issue_comment":
        if SUGGESTION_LABEL not in _labels(event.get("issue") or {}):
            return _empty(issue)
        comment = (event.get("comment") or {}).get("body") or ""
        m = ACCEPT_RE.search(comment)
        if not m:
            return _empty(issue)
        actor = ((event.get("comment") or {}).get("user") or {}).get("login", "").lower()
        if actor not in organizers:
            # Same posture as issue_ops: say no out loud. A silent refusal on a command
            # that looks like it worked is how state and expectation drift apart.
            return {**_empty(issue), "comment": prose.t("accept.organizers_only", actor=actor)}

        body = (event.get("issue") or {}).get("body") or ""
        pool_list = list(pool_list if pool_list is not None else state.load_pool().values())
        verdicts = triage(body, {p["id"]: p for p in pool_list}, fetch=None)
        out, new_pool = accept(m.group(1), verdicts, declared_modality(body), pool_list,
                               fetch=fetch or lookup)
        changed = new_pool != pool_list
        # Resolved = nothing left that /accept-paper could still act on. A thread whose
        # papers are all in the pool, already known, or refused needs nobody.
        settled = not any(v.kind == "new" for v in
                          triage(body, {p["id"]: p for p in new_pool}, fetch=None))
        return {"comment": prose.t("accept.header", who=actor) + "\n\n" + out.delta(),
                "add_labels": [], "issue": issue, "changed": changed,
                "close": settled and changed,
                "close_comment": prose.t("accept.closing") if settled and changed else "",
                "pool": new_pool}

    return _empty(issue)


def render_triage(verdicts: list[Verdict]) -> str:
    """One table, one row per identifier, plus what happens next."""
    rows = []
    for v in verdicts:
        what = {
            "known": lambda: prose.t("verdict.known", pid=v.detail),
            "retired": lambda: prose.t("verdict.retired", pid=v.detail, why=retired_note(v.detail)),
            "declined": lambda: prose.t("verdict.declined", why=v.detail),
            "unresolvable": lambda: prose.t("verdict.unresolvable"),
            "unknown_id": lambda: prose.t("verdict.unknown_id"),
            "new": lambda: (prose.t("verdict.new", cite=v.detail) if v.detail
                            else prose.t("verdict.new_unresolved")),
        }[v.kind]()
        rows.append(f"| `{v.ident.raw}` | {what} |")

    # Three tails, because "nothing new here" is wrong in two different ways: it is not
    # true when a line was simply unreadable, and it is not true when there IS something
    # for an organizer to look at.
    if any(v.kind == "new" for v in verdicts):
        tail = prose.t("suggest.next_steps")
    elif all(v.kind in ("unresolvable", "unknown_id") for v in verdicts):
        tail = prose.t("suggest.nothing_resolvable")
    else:
        tail = prose.t("suggest.nothing_new")
    return "\n".join([prose.t("suggest.heading"), *rows, "", tail])


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text()) if event_path else {}
    event.setdefault("event_name", os.environ.get("GITHUB_EVENT_NAME", ""))

    # Triage resolves over the network for the citation echo and degrades when it cannot;
    # accept does its own resolving and refuses instead of degrading.
    result = handle_event(event, fetch=lookup if event.get("event_name") == "issues" else None)

    if result.get("pool") is not None and result.get("changed"):
        pool = result["pool"]
        state.POOL_FILE.write_text(json.dumps(pool, indent=2, ensure_ascii=False) + "\n")
        state.write_status({p["id"]: p for p in pool}, state.load_claims())

    # Written on EVERY path including the early returns, because the rebase-retry loop
    # `git reset --hard`s and re-runs: a stale artifact left behind would be announced as
    # though it were this run's decision. That was issue #40.
    COMMENT_FILE.write_text(result["comment"] + ("\n" if result["comment"] else ""))
    ACTIONS_FILE.write_text(json.dumps({
        "issue": result["issue"],
        "add_labels": result["add_labels"],
        "assignees": [],
        "changed": result["changed"],
        "close": result.get("close", False),
        "close_comment": result.get("close_comment", ""),
    }, indent=2) + "\n")
    print(f"suggest-ops: issue={result['issue']} changed={result['changed']} "
          f"labels={result['add_labels']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
