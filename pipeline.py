"""
GeM Tender Pipeline — full and delta modes.

Usage
-----
Full run (first time or weekly reset):
    python pipeline.py "Keywords (2).xlsx" relevant_tenders.csv

Daily incremental run (only new bids since last run):
    python pipeline.py "Keywords (2).xlsx" relevant_tenders.csv --mode delta

How delta mode works
--------------------
1. Reads pipeline_state.json for the timestamp of the last run.
2. Fetches only bids published AFTER that timestamp (sorted newest-first,
   stops per keyword as soon as older bids appear).
3. Loads the existing CSV, drops any bids whose end_date has now passed.
4. Merges surviving existing bids + new bids, applies negative filter, saves.

Typical times
-------------
Full run  : ~10–12 min  (290 keywords × adaptive pages)
Delta run : ~3–5 min    (most keywords have 0–2 new bids → 1 page each)
"""
import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from keywords_loader import load_keywords
from gem_client import search_all_keywords

STATE_FILE = "pipeline_state.json"
FIELDNAMES = [
    "bid_no", "title", "ministry", "dept", "quantity",
    "start_date", "end_date", "keyword", "url",
]


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    p = Path(STATE_FILE)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_state(data: dict) -> None:
    Path(STATE_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── CSV helpers ───────────────────────────────────────────────────────────────

def load_existing_csv(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def remove_expired(records: list[dict]) -> list[dict]:
    """Drop bids whose end_date has already passed (UTC date comparison)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    active = [r for r in records if (r.get("end_date") or "")[:10] >= today]
    expired = len(records) - len(active)
    if expired:
        print(f"Dropped {expired} expired bids from existing CSV")
    return active


# ── Filter helpers ────────────────────────────────────────────────────────────

def matches_negative(title: str, neg_lower: list[str]) -> bool:
    t = title.lower()
    return any(n in t for n in neg_lower)


def keyword_match_local(bid: dict, positive_kws: list[str]) -> str | None:
    """
    Check if a bid matches any positive keyword by scanning title + dept + ministry.
    Uses word-boundary matching so short keywords like 'ELEC' don't match
    substrings inside unrelated words (e.g. 'sel-ec-tion', 'el-ec-trical').
    Returns the first matching keyword string, or None if no match.
    """
    text = " ".join([
        bid.get("title", ""),
        bid.get("dept", ""),
        bid.get("ministry", ""),
    ]).lower()
    for kw in positive_kws:
        pattern = r'\b' + re.escape(kw.lower()) + r'\b'
        if re.search(pattern, text):
            return kw
    return None


def dedupe_and_filter(records: list[dict], neg_lower: list[str]) -> list[dict]:
    seen: dict = {}
    for r in records:
        seen[r["bid_no"]] = r
    deduped = list(seen.values())
    filtered = [r for r in deduped if not matches_negative(r.get("title", ""), neg_lower)]
    return filtered


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(xlsx_path: str, output_csv: str, mode: str = "full") -> None:
    positive, negative = load_keywords(xlsx_path)
    neg_lower = [n.lower() for n in negative]
    print(f"Loaded {len(positive)} positive keywords, {len(negative)} negative keywords")

    state = load_state()
    since: str | None = None

    if mode == "delta":
        since = state.get("last_run")
        if not since:
            print("No previous run found in pipeline_state.json — switching to full mode.")
            mode = "full"
        else:
            print(f"Delta mode: fetching bids published after {since}")

    # ── Fetch ────────────────────────────────────────────────────────────────
    raw_new = search_all_keywords(positive, mode=mode, since=since)
    print(f"Fetched {len(raw_new)} raw results from GeM")

    # ── Merge (delta only) ───────────────────────────────────────────────────
    if mode == "delta":
        existing = load_existing_csv(output_csv)
        existing_active = remove_expired(existing)
        print(f"Existing: {len(existing)} bids total, {len(existing_active)} still active")

        # Merge: existing base + new (new wins on bid_no collision)
        merged = {r["bid_no"]: r for r in existing_active}
        for r in raw_new:
            merged[r["bid_no"]] = r
        candidates = list(merged.values())
        print(f"After merge: {len(candidates)} candidates")
    else:
        candidates = raw_new

    # ── Dedupe + negative filter ──────────────────────────────────────────────
    before_filter = len(set(r["bid_no"] for r in candidates))
    filtered = dedupe_and_filter(candidates, neg_lower)
    print(f"After dedupe: {before_filter} | After negative filter: {len(filtered)}")

    # ── Save CSV ──────────────────────────────────────────────────────────────
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(filtered)

    # ── Save state ────────────────────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_state({"last_run": now, "mode": mode, "total_bids": len(filtered)})

    print(f"Saved {len(filtered)} relevant tenders → {output_csv}")
    print(f"State saved → {STATE_FILE}  (next --mode delta will use: {now})")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GeM Tender Pipeline")
    parser.add_argument("xlsx",    nargs="?", default="Keywords (2).xlsx")
    parser.add_argument("csv",     nargs="?", default="relevant_tenders.csv")
    parser.add_argument(
        "--mode",
        choices=["full", "delta"],
        default="full",
        help="full = re-fetch everything (default); delta = only new bids since last run",
    )
    args = parser.parse_args()
    run_pipeline(args.xlsx, args.csv, mode=args.mode)
