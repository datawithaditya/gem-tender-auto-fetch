"""
Download ALL ongoing bids from GeM (no keyword filter).
Saves raw data to all_bids_raw.csv

Resume behaviour:
- Saves a checkpoint CSV every 200 pages  (bid data)
- Saves a state JSON every 25 pages       (last page number)
- On restart, loads both and picks up from the last saved page
- Logs to fetch_all_bids.log + console
"""
import csv
import json
import logging
import os
import time
import requests

from gem_client import (
    HEADERS, SESSION_URL, SEARCH_URL,
    _extract_csrf, _parse_docs, _SORT_DELTA,
    REQUEST_DELAY_SECONDS,
)

OUTPUT     = "all_bids_raw.csv"
CHECKPOINT = "all_bids_checkpoint.csv"
STATE_FILE = "fetch_state.json"
LOG_FILE   = "fetch_all_bids.log"
FIELDNAMES = ["bid_no", "title", "ministry", "dept", "quantity",
              "start_date", "end_date", "keyword", "url"]
SINCE      = "2020-01-01 00:00:00"
MAX_PAGES  = 5000
SAVE_CSV_EVERY   = 200   # write checkpoint CSV every N pages (heavier)
SAVE_STATE_EVERY = 25    # write state JSON every N pages   (lightweight)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _new_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    resp = s.get(SESSION_URL, timeout=12)
    resp.raise_for_status()
    csrf = _extract_csrf(resp.text)
    time.sleep(REQUEST_DELAY_SECONDS)
    return s, csrf


def _fetch_page(session, csrf, page):
    postdata = {
        "param":  {"searchBid": "", "searchType": "fullText"},
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
        timeout=12,
    )
    resp.raise_for_status()
    j = resp.json()
    if j.get("code") != 200:
        return [], 0
    rr = j["response"]["response"]
    return _parse_docs(rr.get("docs", []), ""), rr.get("numFound", 0)


def _save_checkpoint(bids):
    with open(CHECKPOINT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(bids)


def _save_state(page, total_bids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "last_page":  page,
            "total_bids": total_bids,
            "saved_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
        }, f, indent=2)


# ── Load checkpoint + state ───────────────────────────────────────────────────
all_bids: list[dict] = []
seen_bid_nos: set[str] = set()
start_page = 1

if os.path.exists(CHECKPOINT) and os.path.exists(STATE_FILE):
    with open(CHECKPOINT, newline="", encoding="utf-8") as f:
        all_bids = list(csv.DictReader(f))
    seen_bid_nos = {r["bid_no"] for r in all_bids}

    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    start_page = state.get("last_page", 1) + 1

    log.info(f"Resuming from page {start_page}  ({len(all_bids):,} bids already saved)")
    log.info(f"State saved at: {state.get('saved_at', '?')}")

elif os.path.exists(CHECKPOINT):
    with open(CHECKPOINT, newline="", encoding="utf-8") as f:
        all_bids = list(csv.DictReader(f))
    seen_bid_nos = {r["bid_no"] for r in all_bids}
    log.warning(f"Checkpoint found ({len(all_bids):,} bids) but no state file — restarting from page 1")

else:
    log.info("Starting fresh download of all GeM bids...")

log.info(f"Output: {OUTPUT}  |  Max pages: {MAX_PAGES}")
log.info("-" * 60)

# ── Session ───────────────────────────────────────────────────────────────────
log.info("Establishing session with GeM...")
session, csrf = _new_session()
log.info("Session ready.")

t0 = time.perf_counter()
total_pages = 0

# ── Main loop ─────────────────────────────────────────────────────────────────
page = start_page
while page <= MAX_PAGES:
    retries = 0
    while retries < 5:
        try:
            hits, total = _fetch_page(session, csrf, page)
            break
        except Exception as exc:
            retries += 1
            log.warning(f"Page {page} — {exc.__class__.__name__}: {exc}  (retry {retries}/5)")
            time.sleep(3 * retries)
            try:
                session, csrf = _new_session()
                log.info("Session re-established.")
            except Exception as e:
                log.error(f"Session re-establishment failed: {e}")
    else:
        log.error(f"Gave up on page {page} after 5 retries. Saving progress and stopping.")
        break

    if not hits:
        log.info(f"No results on page {page} — download complete.")
        break

    total_pages = total // 10 + 1

    new_hits = [h for h in hits if h["bid_no"] not in seen_bid_nos]
    for h in new_hits:
        seen_bid_nos.add(h["bid_no"])
    all_bids.extend(new_hits)

    if any((h.get("start_date") or "") <= SINCE for h in hits):
        log.info(f"Reached watermark at page {page} — done.")
        break

    # Progress log every 25 pages
    if page % 25 == 0 or page <= 3:
        elapsed = time.perf_counter() - t0
        rate    = (page - start_page + 1) / elapsed if elapsed > 0 else 1
        eta_s   = int((total_pages - page) / rate) if rate > 0 else 0
        log.info(
            f"Page {page:4d}/{total_pages}  |  {len(all_bids):,} bids  "
            f"|  {page/total_pages*100:.0f}%  |  ETA {eta_s//60}m {eta_s%60:02d}s"
        )

    # Save state JSON every 25 pages (fast)
    if page % SAVE_STATE_EVERY == 0:
        _save_state(page, len(all_bids))

    # Save checkpoint CSV every 200 pages (slower)
    if page % SAVE_CSV_EVERY == 0:
        _save_checkpoint(all_bids)
        log.info(f"Checkpoint saved: {len(all_bids):,} bids at page {page}")

    time.sleep(REQUEST_DELAY_SECONDS)
    page += 1

# ── Final save ────────────────────────────────────────────────────────────────
elapsed = time.perf_counter() - t0
log.info("-" * 60)
log.info(f"Done: {len(all_bids):,} bids in {elapsed/60:.1f} min")

with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(all_bids)

log.info(f"Saved -> {OUTPUT}")

# Clean up checkpoint and state on successful completion
for f in (CHECKPOINT, STATE_FILE):
    if os.path.exists(f):
        os.remove(f)
log.info("Checkpoint and state files removed. Download complete.")
