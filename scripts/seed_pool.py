#!/usr/bin/env python3
"""Seed ``data/pool.json`` from the curated literature DB.

Run ONCE (from the my-grants monorepo, where ``references/lit-db/`` exists) and
commit the output. IDs are frozen on first publication: re-running reproduces the
same ids as long as the source data is unchanged, because the sort key is
(citationCount desc, paperId) — fully deterministic.

    python scripts/seed_pool.py --source /path/to/references/lit-db

The pool holds two levels: level-0 seed reviews (``<PREFIX>-R1``…) and the
level-1 first-generation reading pool (``<PREFIX>-01``…). No PDFs — DOI/link only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from params import MODALITY_ORDER, MODALITY_PREFIX, NO_SEED_MODALITIES

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# Default: references/lit-db in the surrounding my-grants grant repo.
DEFAULT_SOURCE = REPO.parents[2] / "references" / "lit-db"


def link_for(item: dict) -> str:
    doi = (item.get("doi") or "").strip()
    if doi:
        return f"https://doi.org/{doi}"
    return f"https://www.semanticscholar.org/paper/{item['paperId']}"


def record(item: dict, pool_id: str, level: int) -> dict:
    return {
        "id": pool_id,
        "modality": item["modality"],
        "level": level,
        "title": item["title"].strip(),
        "first_author": item.get("first_author", ""),
        "year": item.get("year"),
        "doi": (item.get("doi") or "").strip() or None,
        "url": link_for(item),
        "venue": item.get("venue", ""),
        "s2_id": item["paperId"],
        "citation_count": item.get("citationCount"),
    }


def _on_topic(item: dict) -> bool:
    # drop only items the modality audit explicitly marked off-topic; absent = kept
    return item.get("on_topic", True) is not False


def build_pool(source: Path) -> list[dict]:
    children = json.loads((source / "children.json").read_text())
    candidates = json.loads((source / "candidates.json").read_text())
    reviews = [c for c in candidates if c.get("selected") and _on_topic(c)]
    children = [c for c in children if _on_topic(c)]

    pool: list[dict] = []
    for modality in MODALITY_ORDER:
        prefix = MODALITY_PREFIX[modality]

        # no-seed categories (e.g. Cross-modality): pool members flat, no level-0 elders
        if modality in NO_SEED_MODALITIES:
            members = [c for c in children if c["modality"] == modality] + \
                      [r for r in reviews if r["modality"] == modality]
            members.sort(key=lambda c: (-(c.get("citationCount") or 0), c["paperId"]))
            for i, m in enumerate(members, start=1):
                pool.append(record(m, f"{prefix}-{i:02d}", level=1))
            continue

        # level-0 seed reviews, ordered by the curation rank then citations
        mod_reviews = sorted(
            (r for r in reviews if r["modality"] == modality),
            key=lambda r: (r.get("rank", 99), -(r.get("citationCount") or 0), r["paperId"]),
        )
        for i, r in enumerate(mod_reviews, start=1):
            pool.append(record(r, f"{prefix}-R{i}", level=0))

        # level-1 reading pool, ordered by citations (deterministic tie-break)
        mod_children = sorted(
            (c for c in children if c["modality"] == modality),
            key=lambda c: (-(c.get("citationCount") or 0), c["paperId"]),
        )
        for i, c in enumerate(mod_children, start=1):
            pool.append(record(c, f"{prefix}-{i:02d}", level=1))

    return pool


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help=f"path to references/lit-db (default: {DEFAULT_SOURCE})")
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "data" / "pool.json")
    args = ap.parse_args()

    if not (args.source / "children.json").exists():
        raise SystemExit(
            f"error: {args.source}/children.json not found.\n"
            "Run this once from the my-grants monorepo, or pass --source explicitly."
        )

    pool = build_pool(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pool, indent=2, ensure_ascii=False) + "\n")

    levels = {0: 0, 1: 0}
    for p in pool:
        levels[p["level"]] += 1
    print(f"wrote {args.out} — {len(pool)} papers "
          f"({levels[1]} reading-pool + {levels[0]} seed reviews) across "
          f"{len({p['modality'] for p in pool})} modalities")


if __name__ == "__main__":
    main()
