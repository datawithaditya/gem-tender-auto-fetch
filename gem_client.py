"""
Client for GeM Bid Search portal (bidplus.gem.gov.in).

Modes
-----
full     — adaptive pagination per keyword, sorted by end-date (most urgent first).
           Page budget:  ≤50 results → all pages | ≤200 → 3 pages | >200 → 1 page
           Keywords that hit the budget but still have more pages are flagged as
           "capped" so a deep-fetch pass can retrieve the rest.
fullrun  — no page budget; fetches every page for every keyword (up to 200-page
           safety cap). Slower but exhaustive. No capping occurs.
delta    — sorted newest-first (start date). Stops each keyword the moment it sees
           a bid older than `since`. Use for daily incremental runs.

Verified working: 2026-07-01 against live site.
"""
import json
import re
import time
import requests

BASE_URL = "https://bidplus.gem.gov.in"
SESSION_URL = f"{BASE_URL}/all-bids"
SEARCH_URL = f"{BASE_URL}/all-bids-data"
BID_DOC_URL = f"{BASE_URL}/showbidDocument"
RA_DOC_URL = f"{BASE_URL}/showradocumentPdf"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": SESSION_URL,
    "Origin": BASE_URL,
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

REQUEST_DELAY_SECONDS = 0.5

_SORT_FULL  = "Bid-End-Date-Oldest"    # full: most urgent deadlines first
_SORT_DELTA = "Bid-Start-Date-Latest"  # delta: newest bids first


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_csrf(html: str) -> str:
    matches = re.findall(r"csrf_bd_gem_nk'\s*:\s*'([a-f0-9]+)'", html)
    if not matches:
        raise ValueError("Could not find CSRF token in page HTML")
    return matches[0]


def _unwrap(value):
    """Unwrap single-element lists returned by the GeM API."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _bid_url(doc: dict) -> str:
    bid_type = _unwrap(doc.get("b_bid_type", 1))
    bid_id   = _unwrap(doc.get("b_id", ""))
    if bid_type in (2, 5):
        return f"{RA_DOC_URL}/{bid_id}"
    return f"{BID_DOC_URL}/{bid_id}"


def _parse_docs(docs: list, keyword: str) -> list[dict]:
    results = []
    for doc in docs:
        start_date = _unwrap(doc.get("final_start_date_sort", "")) or ""
        end_date   = _unwrap(doc.get("final_end_date_sort",   "")) or ""
        results.append({
            "keyword":    keyword,
            "bid_no":     _unwrap(doc.get("b_bid_number", ""))    or "",
            "title":      _unwrap(doc.get("b_category_name", "")) or "",
            "ministry":   _unwrap(doc.get("ba_official_details_minName"))  or "",
            "dept":       _unwrap(doc.get("ba_official_details_deptName",  "")) or "",
            "quantity":   _unwrap(doc.get("b_total_quantity", "")),
            "start_date": start_date.replace("T", " ").replace("Z", ""),
            "end_date":   end_date.replace("T", " ").replace("Z", ""),
            "url":        _bid_url(doc),
        })
    return results


def _max_pages(total: int) -> int:
    """Adaptive page budget for full mode."""
    if total <= 50:
        return (total + 9) // 10   # fetch every page — it's a specific keyword
    if total <= 200:
        return 3
    return 1                        # too broad; top-10 is enough


# ── Core search ───────────────────────────────────────────────────────────────

def search_bids(
    keyword: str,
    session: requests.Session,
    csrf: str,
    page: int = 1,
    sort: str = _SORT_FULL,
) -> tuple[list[dict], int]:
    """One POST to /all-bids-data. Returns (results, numFound)."""
    postdata = {
        "param":  {"searchBid": keyword, "searchType": "fullText"},
        "filter": {
            "bidStatusType": "ongoing_bids",
            "byType":        "all",
            "highBidValue":  "",
            "byEndDate":     {"from": "", "to": ""},
            "sort":          sort,
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
    return _parse_docs(rr.get("docs", []), keyword), rr.get("numFound", 0)


# ── Multi-keyword runner ──────────────────────────────────────────────────────

def search_all_keywords(
    keywords: list[str],
    mode: str = "full",
    since: str | None = None,
) -> list[dict]:
    """
    Search all keywords.

    mode='full'  — adaptive pagination, sorted by urgency (end date).
    mode='delta' — sorted newest-first; stops per keyword once bids are older
                   than `since` (ISO datetime string "YYYY-MM-DD HH:MM:SS").
                   Falls back to full if `since` is None.
    """
    if mode == "delta" and not since:
        print("Warning: delta mode requested but no `since` date given. Using full mode.")
        mode = "full"

    sort = _SORT_DELTA if mode == "delta" else _SORT_FULL
    all_results: list[dict] = []

    with requests.Session() as session:
        session.headers.update(HEADERS)
        print("Establishing session with GeM portal...")
        resp = session.get(SESSION_URL, timeout=8)
        resp.raise_for_status()
        csrf = _extract_csrf(resp.text)
        print(f"Session ready. Mode: {mode}. Searching {len(keywords)} keywords...")
        time.sleep(REQUEST_DELAY_SECONDS)

        for i, kw in enumerate(keywords, 1):
            kw_results: list[dict] = []
            try:
                for page in range(1, 100):   # upper bound; break controls exit
                    hits, total = search_bids(kw, session, csrf, page, sort)

                    if not hits:
                        if page == 1:
                            print(f"[{i}/{len(keywords)}] '{kw}' -> 0 results")
                        break

                    if mode == "delta":
                        # Keep only bids newer than last run
                        new_hits = [h for h in hits if h["start_date"] > since]
                        kw_results.extend(new_hits)
                        if page == 1:
                            print(
                                f"[{i}/{len(keywords)}] '{kw}' -> "
                                f"{total} total, {len(new_hits)} new"
                            )
                        # Hit the time boundary or safety page cap
                        if len(new_hits) < len(hits) or page >= 5:
                            break
                    else:
                        kw_results.extend(hits)
                        max_pg = _max_pages(total)
                        if page == 1:
                            print(
                                f"[{i}/{len(keywords)}] '{kw}' -> "
                                f"{total} total ({max_pg} page(s))"
                            )
                        if page >= max_pg:
                            break

                    time.sleep(REQUEST_DELAY_SECONDS)

            except requests.RequestException as exc:
                print(f"[{i}/{len(keywords)}] '{kw}' -> ERROR: {exc}")

            all_results.extend(kw_results)
            time.sleep(REQUEST_DELAY_SECONDS)

    return all_results


def search_keywords_chunk(
    keywords: list[str],
    mode: str = "full",
    since: str | None = None,
    on_keyword=None,
) -> tuple[list[dict], list[dict]]:
    """
    Search a subset of keywords using its own session (designed for parallel use).

    on_keyword(kw, hits) is called after each keyword completes.
    Returns (results, capped) where capped is a list of
    {keyword, total, pages_fetched} for keywords where the page budget was hit
    but more pages exist.
    """
    sort = _SORT_DELTA if mode == "delta" else _SORT_FULL
    all_results: list[dict] = []
    capped: list[dict] = []  # only populated in "full" (best run) mode

    with requests.Session() as session:
        session.headers.update(HEADERS)
        resp = session.get(SESSION_URL, timeout=8)
        resp.raise_for_status()
        csrf = _extract_csrf(resp.text)
        time.sleep(REQUEST_DELAY_SECONDS)

        for kw in keywords:
            kw_results: list[dict] = []
            try:
                for page in range(1, 200):
                    hits, found = search_bids(kw, session, csrf, page, sort)
                    if not hits:
                        break

                    if mode == "delta":
                        new_hits = [h for h in hits if h["start_date"] > since]
                        kw_results.extend(new_hits)
                        if len(new_hits) < len(hits) or page >= 5:
                            break
                    elif mode == "fullrun":
                        kw_results.extend(hits)
                        # fetch until exhausted — no budget cap
                    else:  # "full" / best run
                        kw_results.extend(hits)
                        max_pg = _max_pages(found)
                        if page >= max_pg:
                            if found > page * 10:
                                capped.append({
                                    "keyword": kw,
                                    "total": found,
                                    "pages_fetched": page,
                                })
                            break

                    time.sleep(REQUEST_DELAY_SECONDS)

            except requests.RequestException as exc:
                print(f"'{kw}' -> ERROR: {exc}")

            if on_keyword:
                on_keyword(kw, kw_results)

            all_results.extend(kw_results)
            time.sleep(REQUEST_DELAY_SECONDS)

    return all_results, capped


def search_keywords_deep(
    capped: list[dict],
    on_keyword=None,
) -> list[dict]:
    """
    Fetch remaining pages for keywords that were capped in a previous run.
    Each item in capped: {keyword, total, pages_fetched}.
    """
    all_results: list[dict] = []

    with requests.Session() as session:
        session.headers.update(HEADERS)
        resp = session.get(SESSION_URL, timeout=8)
        resp.raise_for_status()
        csrf = _extract_csrf(resp.text)
        time.sleep(REQUEST_DELAY_SECONDS)

        for item in capped:
            kw = item["keyword"]
            start_page = item["pages_fetched"] + 1
            kw_results: list[dict] = []
            try:
                for page in range(start_page, 200):
                    hits, _ = search_bids(kw, session, csrf, page, _SORT_FULL)
                    if not hits:
                        break
                    kw_results.extend(hits)
                    time.sleep(REQUEST_DELAY_SECONDS)
            except requests.RequestException as exc:
                print(f"'{kw}' deep -> ERROR: {exc}")

            if on_keyword:
                on_keyword(kw, kw_results)
            all_results.extend(kw_results)
            time.sleep(REQUEST_DELAY_SECONDS)

    return all_results


# ── Sync fetch (local-DB stop condition, no keyword filter) ──────────────────

def sync_fetch_new_bids(
    known_bid_nos: set[str],
    on_page=None,
    max_pages: int = 500,
) -> list[dict]:
    """
    Fetch all ongoing bids newest-first until we encounter a bid_no already
    present in known_bid_nos (i.e. already in the local all_bids_raw.csv).

    Stops at the first page that contains any known bid_no — everything from
    that point on is already in the local DB.

    on_page(page, total_on_gem, new_count_so_far) called after each page.
    Returns only the truly new bids not in known_bid_nos.
    """
    new_bids: list[dict] = []

    with requests.Session() as session:
        session.headers.update(HEADERS)
        resp = session.get(SESSION_URL, timeout=8)
        resp.raise_for_status()
        csrf = _extract_csrf(resp.text)
        time.sleep(REQUEST_DELAY_SECONDS)

        for page in range(1, max_pages + 1):
            hits, total = search_bids("", session, csrf, page, _SORT_DELTA)

            if not hits:
                break

            truly_new = [h for h in hits if h["bid_no"] not in known_bid_nos]
            new_bids.extend(truly_new)

            if on_page:
                on_page(page, total, len(new_bids))

            # Stop as soon as we see a bid already in our local DB
            if len(truly_new) < len(hits):
                break

            time.sleep(REQUEST_DELAY_SECONDS)

    return new_bids


# ── Debug helper ──────────────────────────────────────────────────────────────

def debug_fetch_one(keyword: str) -> None:
    with requests.Session() as session:
        session.headers.update(HEADERS)
        resp = session.get(SESSION_URL, timeout=8)
        resp.raise_for_status()
        csrf = _extract_csrf(resp.text)
        print(f"CSRF: {csrf[:12]}...  cookies: {list(session.cookies.keys())}")
        time.sleep(REQUEST_DELAY_SECONDS)

        hits, total = search_bids(keyword, session, csrf)
        print(f"\n'{keyword}'  |  Total on GeM: {total}\n")
        for h in hits:
            print(f"  {h['bid_no']}")
            print(f"    {h['title']}")
            print(f"    {h['dept']} / {h['ministry']}")
            print(f"    ends {h['end_date']}  |  {h['url']}")
            print()


if __name__ == "__main__":
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "welding electrode"
    debug_fetch_one(kw)
