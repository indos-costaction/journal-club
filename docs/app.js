"use strict";
// Client-only renderer. Reads the three JSONs the Actions regenerate; no build step.

// Which GitHub repo the "Claim" buttons target. Precedence:
//   1. window.JC_REPO — explicit override (index.html);
//   2. data/site.json {"repo": "..."} — generated from ${{ github.repository }} at
//      GitHub Pages BUILD time by .github/workflows/pages.yml; never committed to git;
//   3. the official repo, as a last resort (no build yet, or local file:// preview).
const OFFICIAL = "indos-costaction/journal-club";
let REPO = window.JC_REPO || OFFICIAL;   // refined from data/site.json in load()

const lastName = a => (a || "").trim().split(/\s+/).pop() || "";
function issueTitle(p) {
  return p ? `[claim] ${lastName(p.first_author)} et al. ${p.year} - ${p.title}` : "[claim] ";
}
function newIssueURL(p) {
  const q = new URLSearchParams({ template: "claim.yml", title: issueTitle(p) });
  if (p) q.set("paper_ids", p.id);
  return `https://github.com/${REPO}/issues/new?${q}`;
}

const $ = sel => document.querySelector(sel);
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of kids) n.append(k);
  return n;
};

let POOL = [], STATUS = {}, RANKING = { participants: [] };
let POOL_BY_ID = new Map();   // id -> paper, for the mosaic hover card

async function load() {
  // GitHub Pages serves these with max-age=600, so a plain fetch can show a claim/
  // withdrawal up to ~10 min stale. Cache-bust the volatile files so a reload is live.
  // pool.json is static (only changes on reseed) → let it cache normally.
  const bust = u => `${u}?t=${Date.now()}`;
  const nostore = { cache: "no-store" };
  const [pool, status, ranking, site] = await Promise.all([
    fetch("data/pool.json").then(r => r.json()),
    fetch(bust("data/status.json"), nostore).then(r => r.json()),
    fetch(bust("data/ranking.json"), nostore).then(r => r.json()).catch(() => ({ participants: [] })),
    fetch(bust("data/site.json"), nostore).then(r => r.json()).catch(() => null),
  ]);
  POOL = pool;
  STATUS = status;
  RANKING = ranking;
  POOL_BY_ID = new Map(POOL.map(p => [p.id, p]));
  // build-time slug from site.json is the authoritative repo (unless overridden)
  if (!window.JC_REPO && site && site.repo) REPO = site.repo;
  $("#claimTop").href = newIssueURL(null);
  initModalities();
  applyFiltersFromURL();   // after the modality options exist, so they can be matched
  renderProgress();
  renderPool();
  renderBoard();
  const t = status.generated_at ? `Updated ${status.generated_at.slice(0, 10)}.` : "";
  $("#stamp").textContent = t;
  renderMosaic();
  scrollToPapersIfDeepLinked();
}

// The mosaic answers "how far along is this paper?", which is NOT the table's
// `status` field: that one answers "can I still claim it?" and stays "open" until
// five people hold it. A paper with one claimant is being read, so it must not
// look untouched. Progress is therefore derived here, from the counts.
const REVIEW = "review", DONE = "done", OPEN = "open";
function paperProgress(id) {
  const s = STATUS.papers?.[id];
  if (!s) return OPEN;
  if (s.status === "done") return DONE;
  return (s.live_claims > 0 || s.completed_reviews > 0) ? REVIEW : OPEN;
}

// Hero mosaic: one tile per paper, in pool.json order (which is clustered by modality).
function renderMosaic() {
  const box = $("#mosaic"); if (!box) return;
  const frag = document.createDocumentFragment();
  const tally = { open: 0, review: 0, done: 0 };
  for (const p of POOL) {
    const st = paperProgress(p.id);
    tally[st]++;
    // No `title` — the native tooltip only fits the ID and looks nothing like the
    // site. The hover card below carries the real detail.
    const tile = el("a", {
      className: "s-" + st,
      href: "#row-" + p.id,
      // 242 tab stops in front of the CTA would wreck keyboard navigation; the
      // table below (with its search) is the accessible route to any paper.
      tabIndex: -1,
    });
    tile.dataset.paper = p.id;
    frag.append(tile);
  }
  box.replaceChildren(frag);
  box.setAttribute("aria-label",
    `Paper pool: ${tally.open} not yet started, ${tally.review} in review, ${tally.done} complete, of ${POOL.length}.`);
  const t = STATUS.totals;
  if (t) $("#mosaicCount").textContent =
    `${t.reviews_completed} of ${t.papers * STATUS.params.completion_threshold} reviews in`;
}

/* ---------------------------------------------------------------------------
   Hover card. One reusable node, not 242, positioned on demand.
--------------------------------------------------------------------------- */
const WORD = { open: "open", review: "in review", done: "complete" };

function tipContent(p) {
  const s = STATUS.papers?.[p.id] || { live_claims: 0, completed_reviews: 0, status: "open" };
  const st = paperProgress(p.id);
  const meta = [p.first_author, p.year, p.venue].filter(Boolean).join(" · ");

  // What the click actually does depends on claimability (`status`), which is not
  // the same question as progress: a paper can be in review and still claimable.
  const cta = s.status === "open"
    ? "Click to find it in the list and claim it →"
    : s.status === "done"
      ? "Complete — click to find it in the list →"
      : "Closed to new claims — click to find it in the list →";

  return [
    el("p", { className: "tip-title", textContent: p.title }),
    el("p", { className: "tip-meta", textContent: meta }),
    el("p", { className: "tip-stats" },
      el("span", { className: "key k-" + st }),
      el("span", { textContent: WORD[st] }),
      el("span", { className: "tip-sep", textContent: "·" }),
      el("span", { textContent: `${s.live_claims}/${STATUS.params.pool_close_threshold} claims` }),
      el("span", { className: "tip-sep", textContent: "·" }),
      el("span", { textContent: `${s.completed_reviews}/${STATUS.params.completion_threshold} reviews` })),
    el("p", { className: "tip-cta", textContent: cta }),
  ];
}

function showTip(tile) {
  const tip = $("#mosaicTip");
  const p = POOL_BY_ID.get(tile.dataset.paper);
  if (!tip || !p) return;
  tip.replaceChildren(...tipContent(p));
  tip.classList.add("show");          // measurable before positioning: visibility, not display

  const a = tile.getBoundingClientRect(), t = tip.getBoundingClientRect(), gap = 10;
  const x = Math.max(8, Math.min(a.left + a.width / 2 - t.width / 2, innerWidth - t.width - 8));
  const below = a.bottom + gap;
  const y = below + t.height > innerHeight - 8 ? a.top - t.height - gap : below;
  tip.style.left = `${Math.round(x)}px`;
  tip.style.top = `${Math.round(Math.max(8, y))}px`;
}

const hideTip = () => $("#mosaicTip")?.classList.remove("show");

const mosaicBox = $("#mosaic");
if (mosaicBox) {
  // mouseover/mouseleave, not mouseenter/mouseout: mouseover bubbles from the tiles,
  // and hiding only on leaving the whole mosaic avoids a flicker between neighbours.
  mosaicBox.addEventListener("mouseover", e => {
    const tile = e.target.closest("a[data-paper]");
    if (tile) showTip(tile);
  });
  mosaicBox.addEventListener("mouseleave", hideTip);
  // The card is position:fixed, so it would otherwise detach from its tile.
  addEventListener("scroll", hideTip, { passive: true });

  // Clicking a tile jumps to that paper's row. Delegated — 242 listeners would be wasteful.
  mosaicBox.addEventListener("click", e => {
    const tile = e.target.closest("a[data-paper]");
    if (!tile) return;
    e.preventDefault();
    hideTip();
    focusPaper(tile.dataset.paper);
  });
}

function focusPaper(id) {
  document.querySelector('.tabs button[data-tab="pool"]')?.click();
  let row = document.getElementById("row-" + id);
  if (!row) {
    // A filter or search is hiding it — the tile promised a paper, so reveal it.
    $("#search").value = "";
    $("#modality").value = "";
    $("#status").value = "";
    $("#needy").checked = false;
    syncFiltersToURL();
    renderPool();
    row = document.getElementById("row-" + id);
  }
  if (!row) return;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.remove("row-focus");
  void row.offsetWidth;            // restart the flash when the same tile is clicked twice
  row.classList.add("row-focus");
}

// The pool now sits below the fold behind a landing hero, so old deep links
// (?modality=EEG, ?status=…, ?need=1) and #pool/#board hashes have to land the
// visitor on the table rather than the hero. "instant" bypasses the CSS smooth
// scroll — a deep link should arrive, not animate.
function scrollToPapersIfDeepLinked() {
  const p = new URLSearchParams(location.search);
  const hasFilter = p.has("modality") || p.has("status") || p.has("need");
  if (location.hash === "#board")
    document.querySelector('.tabs button[data-tab="board"]')?.click();
  if (!hasFilter && location.hash !== "#pool" && location.hash !== "#board") return;
  // Chrome would otherwise restore the pre-reload scroll position on top of this.
  history.scrollRestoration = "manual";
  document.getElementById("papers")?.scrollIntoView({ behavior: "instant", block: "start" });
}

function initModalities() {
  const mods = [...new Set(POOL.map(p => p.modality))];
  const sel = $("#modality");
  mods.forEach(m => sel.append(el("option", { value: m, textContent: m })));
}

function renderProgress() {
  const t = STATUS.totals; if (!t) return;
  const reviews = t.reviews_completed, needed = t.papers * STATUS.params.completion_threshold;
  const pct = needed ? Math.round(100 * reviews / needed) : 0;
  $("#progressFill").style.width = pct + "%";
  $("#progressText").textContent =
    `${reviews} / ${needed} reviews in · ${t.done} papers done · ` +
    `${t.total_outstanding} reviews still needed across ${t.papers} papers`;
  $("#progress").hidden = false;
}

function statusBadge(s) {
  return el("span", { className: "badge b-" + s, textContent: s });
}

function renderPool() {
  const q = $("#search").value.trim().toLowerCase();
  const mod = $("#modality").value, st = $("#status").value, needy = $("#needy").checked;
  const tbody = $("#poolTable tbody");
  tbody.replaceChildren();
  let n = 0;
  for (const p of POOL) {
    const s = STATUS.papers?.[p.id] || { live_claims: 0, completed_reviews: 0, status: "open", outstanding_need: 3 };
    if (mod && p.modality !== mod) continue;
    if (st && s.status !== st) continue;
    if (needy && s.outstanding_need === 0) continue;
    if (q && !(`${p.id} ${p.title} ${p.first_author}`.toLowerCase().includes(q))) continue;
    n++;
    const title = el("td", {},
      el("a", { href: p.url, target: "_blank", rel: "noopener", textContent: p.title }),
      el("div", { className: "meta", textContent: `${p.first_author || ""}${p.year ? " · " + p.year : ""}${p.level === 0 ? " · seed review" : ""}` }));
    const canClaim = s.status === "open";
    const action = canClaim
      ? el("a", { className: "claim", href: newIssueURL(p), textContent: "Claim" })
      : el("span", { className: "muted", textContent: s.status === "done" ? "—" : "closed" });
    tbody.append(el("tr", { id: "row-" + p.id },   // hero mosaic tiles link here
      el("td", { textContent: p.id }),
      el("td", { textContent: p.modality }),
      title,
      el("td", { className: "num", textContent: `${s.live_claims}/${STATUS.params.pool_close_threshold}` }),
      el("td", { className: "num", textContent: `${s.completed_reviews}/${STATUS.params.completion_threshold}` }),
      el("td", {}, statusBadge(s.status)),
      el("td", {}, action)));
  }
  $("#poolCount").textContent = `${n} paper${n === 1 ? "" : "s"} shown of ${POOL.length}.`;
}

function renderBoard() {
  const rows = RANKING.participants || [];
  const tbody = $("#boardTable tbody");
  tbody.replaceChildren();
  $("#boardEmpty").hidden = rows.length > 0;
  $("#boardTable").hidden = rows.length === 0;
  for (const r of rows) {
    tbody.append(el("tr", {},
      el("td", { className: "num", textContent: r.rank }),
      el("td", { textContent: r.display }),
      el("td", { className: "num", textContent: r.points.toFixed(2) }),
      el("td", { className: "num", textContent: r.reviews }),
      el("td", { className: "num", textContent: r.mean.toFixed(2) })));
  }
}

// Pool filters are mirrored in the URL query string (?modality=&status=&need=1)
// so they persist on reload and the view is shareable.
function applyFiltersFromURL() {
  const p = new URLSearchParams(location.search);
  const mod = p.get("modality");
  if (mod) {
    const opt = [...$("#modality").options].find(o => o.value.toLowerCase() === mod.toLowerCase());
    if (opt) $("#modality").value = opt.value;
  }
  const st = (p.get("status") || "").toLowerCase();
  if ([...$("#status").options].some(o => o.value === st)) $("#status").value = st;
  $("#needy").checked = p.get("need") === "1";
}

function syncFiltersToURL() {
  const p = new URLSearchParams();
  if ($("#modality").value) p.set("modality", $("#modality").value);
  if ($("#status").value) p.set("status", $("#status").value);
  if ($("#needy").checked) p.set("need", "1");
  const qs = p.toString();
  history.replaceState(null, "", (qs ? "?" + qs : location.pathname) + location.hash);
}

// tabs + controls
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("active", x === b));
  $("#pool").hidden = b.dataset.tab !== "pool";
  $("#board").hidden = b.dataset.tab !== "board";
}));
["#modality", "#status", "#needy"].forEach(s =>
  $(s).addEventListener("input", () => { syncFiltersToURL(); renderPool(); }));
$("#search").addEventListener("input", renderPool);  // search stays out of the URL

load().catch(e => { $("#poolCount").textContent = "Could not load data: " + e; });
