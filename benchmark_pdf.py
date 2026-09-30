"""
Benchmark: sequential vs threaded vs multiprocess PDF analysis
Usage: python benchmark_pdf.py
"""
import csv
import time
import threading
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

from pdf_analyzer import (
    _make_session, _optimal_workers, download_pdf, parse_pdf, _thread_local,
)
from gem_client import REQUEST_DELAY_SECONDS


def load_bids(date_str: str, limit: int = 10) -> list[dict]:
    with open("relevant_tenders.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    bids = [r for r in rows if (r.get("end_date") or "")[:10] == date_str]
    print(f"Found {len(bids)} bids for {date_str}, using first {min(limit, len(bids))}")
    return bids[:limit]


# ── Sequential ────────────────────────────────────────────────────────────────
def run_sequential(bids: list[dict]) -> float:
    session = _make_session()
    t0 = time.perf_counter()
    for bid in bids:
        bid_no = bid["bid_no"]
        path = download_pdf(bid["url"], bid_no, session)
        time.sleep(REQUEST_DELAY_SECONDS)
        if path:
            try:
                parse_pdf(path)
                print(f"  [seq] {bid_no} done")
            except Exception as e:
                print(f"  [seq] {bid_no} error: {e}")
        else:
            print(f"  [seq] {bid_no} download failed")
    return time.perf_counter() - t0


# ── Threaded ──────────────────────────────────────────────────────────────────
def _thread_session_local():
    if not hasattr(_thread_local, "session"):
        _thread_local.session = _make_session()
    return _thread_local.session

def _process_one_threaded(bid: dict) -> str:
    bid_no = bid["bid_no"]
    session = _thread_session_local()
    path = download_pdf(bid["url"], bid_no, session)
    time.sleep(REQUEST_DELAY_SECONDS)
    if path:
        try:
            parse_pdf(path)
            return f"  [thr] {bid_no} done"
        except Exception as e:
            return f"  [thr] {bid_no} error: {e}"
    return f"  [thr] {bid_no} download failed"

def run_threaded(bids: list[dict], workers: int) -> float:
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(_process_one_threaded, bids):
            print(result)
    return time.perf_counter() - t0


# ── Process pool (top-level function required for pickling) ───────────────────
def _parse_in_process(args) -> dict:
    path, bid = args
    try:
        parsed = parse_pdf(path)
        parsed["url"]     = bid.get("url", "")
        parsed["title"]   = bid.get("title", "")
        parsed["keyword"] = bid.get("keyword", "")
        return {"ok": True, "bid_no": bid["bid_no"], "parsed": parsed}
    except Exception as e:
        return {"ok": False, "bid_no": bid["bid_no"], "error": str(e)}

def run_multiprocess(bids: list[dict], workers: int) -> float:
    t0 = time.perf_counter()

    # Phase 1 — download in parallel with threads (I/O bound)
    paths: dict[str, str | None] = {}
    def _download(bid):
        session = _thread_session_local()
        path = download_pdf(bid["url"], bid["bid_no"], session)
        time.sleep(REQUEST_DELAY_SECONDS)
        return bid["bid_no"], path

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for bid_no, path in pool.map(_download, bids):
            paths[bid_no] = path

    t_downloaded = time.perf_counter() - t0
    print(f"  [mp]  Downloads done in {t_downloaded:.1f}s")

    # Phase 2 — parse in parallel with processes (CPU bound)
    to_parse = [(paths[b["bid_no"]], b) for b in bids if paths.get(b["bid_no"])]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(_parse_in_process, to_parse):
            if result["ok"]:
                print(f"  [mp]  {result['bid_no']} done")
            else:
                print(f"  [mp]  {result['bid_no']} error: {result['error']}")

    return time.perf_counter() - t0


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    DATE = "2026-07-08"
    bids = load_bids(DATE, limit=10)
    if not bids:
        print("No bids found.")
        raise SystemExit(1)

    workers = _optimal_workers(len(bids))
    print(f"Machine: {workers} workers (cpus=12)\n")

    print("=" * 52)
    print(f"1. SEQUENTIAL  (10 bids, 1 worker)")
    print("=" * 52)
    t1 = run_sequential(bids)
    print(f"-> {t1:.1f}s\n")

    print("=" * 52)
    print(f"2. THREADED    (10 bids, {workers} workers) -- GIL limited")
    print("=" * 52)
    t2 = run_threaded(bids, workers)
    print(f"-> {t2:.1f}s\n")

    print("=" * 52)
    print(f"3. MULTIPROCESS (10 bids, {workers} workers) -- true parallel")
    print("=" * 52)
    t3 = run_multiprocess(bids, workers)
    print(f"-> {t3:.1f}s\n")

    print("=" * 52)
    print("RESULTS")
    print("=" * 52)
    print(f"  Sequential:   {t1:.1f}s  (baseline)")
    print(f"  Threaded:     {t2:.1f}s  ({t1/t2:.1f}x)")
    print(f"  Multiprocess: {t3:.1f}s  ({t1/t3:.1f}x)")
