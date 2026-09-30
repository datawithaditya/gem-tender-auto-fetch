"""
Test the sync approach:

Phase 1 — Verify empty-keyword API call works
Phase 2 — Simulate watermark at end of 2026-07-01 (since = "2026-07-01 23:59:59")
Phase 3 — Sync from that watermark; should only fetch July-2+ bids (fast: ~10-30 pages)
Phase 4 — Keyword-filter the new bids

Usage: python test_sync.py
"""
import json
import time
import requests

from gem_client import (
    HEADERS, SESSION_URL, SEARCH_URL, _extract_csrf,
    _parse_docs, _SORT_DELTA, REQUEST_DELAY_SECONDS,
    sync_fetch_new_bids,
)
from pipeline import keyword_match_local
from keywords_loader import load_keywords

KEYWORDS_FILE = "Keywords (2).xlsx"
SINCE         = "2026-07-01 23:59:59"   # watermark: fetch bids AFTER this moment


def _make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    resp = s.get(SESSION_URL, timeout=8)
    resp.raise_for_status()
    csrf = _extract_csrf(resp.text)
    time.sleep(REQUEST_DELAY_SECONDS)
    return s, csrf


def fetch_page_raw(session, csrf, page=1, keyword=""):
    """Single raw API call — returns (docs, numFound)."""
    postdata = {
        "param":  {"searchBid": keyword, "searchType": "fullText"},
        "filter": {
            "bidStatusType": "ongoing_bids",
            "byType":        "all",
            "highBidValue":  "",
            "byEndDate":     {"from": "", "to": ""},
            "sort":          _SORT_DELTA,
        },
        "page": page,
    }
    resp = session.post(
        SEARCH_URL,
        data={"payload": json.dumps(postdata), "csrf_bd_gem_nk": csrf},
        timeout=8,
    )
    resp.raise_for_status()
    j = resp.json()
    if j.get("code") != 200:
        return [], 0
    rr = j["response"]["response"]
    docs = _parse_docs(rr.get("docs", []), keyword)
    return docs, rr.get("numFound", 0)


# ── Phase 1: confirm empty-keyword API call works + show newest bids ──────────
print("=" * 60)
print("PHASE 1 — verify empty-keyword API call")
print("=" * 60)

session, csrf = _make_session()
bids_p1, total = fetch_page_raw(session, csrf, page=1, keyword="")

if not bids_p1:
    print("ERROR: Empty keyword returned no results — API may require a keyword.")
    raise SystemExit(1)

print(f"OK — total ongoing bids on GeM: {total:,}")
print(f"Newest {len(bids_p1)} bids (page 1, sorted newest-first):")
for b in bids_p1:
    print(f"  {b['bid_no']}  start={b['start_date'][:10]}  end={b['end_date'][:10]}  {b['title'][:50]}")

# ── Phase 2: show watermark ───────────────────────────────────────────────────
print()
print("=" * 60)
print(f"PHASE 2 — watermark: {SINCE}")
print("=" * 60)
print(f"Will fetch all bids with start_date > '{SINCE}'")
print("Expected: only July-2 and later bids (~10-30 pages, not 300+)")

# ── Phase 3: run sync with date watermark ────────────────────────────────────
print()
print("=" * 60)
print("PHASE 3 — run sync_fetch_new_bids with date watermark")
print("=" * 60)

pages_done = [0]

def on_page(page, total_on_gem, new_count):
    pages_done[0] = page
    print(f"  Page {page:3d} — {new_count} new bids so far")

import time as _time
t0 = _time.perf_counter()
new_bids = sync_fetch_new_bids(SINCE, on_page=on_page)
elapsed = _time.perf_counter() - t0

print(f"\nSync complete in {elapsed:.1f}s — {len(new_bids)} new bids across {pages_done[0]} pages")

# ── Phase 4: keyword-filter the new bids ─────────────────────────────────────
print()
print("=" * 60)
print("PHASE 4 — keyword filter")
print("=" * 60)

positive, negative = load_keywords(KEYWORDS_FILE)
neg_lower = [n.lower() for n in negative]

matched = []
for bid in new_bids:
    kw = keyword_match_local(bid, positive)
    if kw and not any(n in (bid.get("title") or "").lower() for n in neg_lower):
        bid["keyword"] = kw
        matched.append(bid)

print(f"New bids fetched:    {len(new_bids)}")
print(f"Keyword-matched:     {len(matched)}")
print(f"Pages scanned:       {pages_done[0]}")
print(f"Time taken:          {elapsed:.1f}s")
print()
if matched:
    print("Sample matched bids:")
    for b in matched[:10]:
        print(f"  {b['bid_no']}  [{b['keyword']}]  {b['title'][:60]}")
