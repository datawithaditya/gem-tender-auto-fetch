"""
FastAPI web portal for the GeM Tender Pipeline.

Routes:
  GET  /                — Dashboard UI
  POST /sync            — Fetch new bids since last run, update all_bids_raw.csv,
                          re-filter all bids against keywords → relevant_bids.xlsx
  GET  /progress        — SSE stream of live sync progress
  GET  /status          — Current state as JSON
  GET  /results         — Full results list as JSON
  GET  /analyze/dates   — Unique end-dates from current results (used by Get Bid dropdown)
  GET  /download        — Download relevant_bids.xlsx
  POST /get-bid         — Download bid documents + build analysis Excel for a date
  GET  /get-bid/status  — Get Bid progress
  GET  /get-bid/download — Download Get Bid Excel
"""
import asyncio
import csv
import json
import os
import queue
import threading
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from keywords_loader import load_keywords
from pipeline import (
    load_state,
    save_state,
    keyword_match_local,
    FIELDNAMES,
)

KEYWORDS_FILE      = os.getenv("KEYWORDS_FILE", "Keywords (2).xlsx")
RELEVANT_BIDS_XLSX = "relevant_bids.xlsx"
ALL_BIDS_CSV       = "all_bids_raw.csv"

# ── Get-Bid state ─────────────────────────────────────────────────────────────
_get_bid_state: dict = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_bid": "",
    "current_status": "",
    "excel_path": None,
    "folder": None,
    "error": None,
    "date_filter": None,
    "workers": 0,
}
_get_bid_lock = threading.Lock()

app = FastAPI(title="GeM Tender Monitor")
_INDEX_HTML = os.path.join(os.path.dirname(__file__), "templates", "index.html")

# ── Global sync state ─────────────────────────────────────────────────────────
_state: dict = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_kw": "",
    "mode": "sync",
    "results": [],
    "last_run": None,
    "error": None,
    "last_summary": None,
}
_state_lock = threading.Lock()

_sse_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()


def _broadcast(msg: dict) -> None:
    with _clients_lock:
        for q in _sse_clients:
            q.put_nowait(msg)


# ── xlsx helpers ──────────────────────────────────────────────────────────────
def _write_relevant_xlsx(rows: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Relevant Bids"

    header_fill = PatternFill("solid", fgColor="1F3864")
    header_font = Font(color="FFFFFF", bold=True)

    ws.append(FIELDNAMES)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A2"

    for row in rows:
        ws.append([str(row.get(f) or "") for f in FIELDNAMES])

    col_widths = {
        "bid_no": 22, "title": 60, "ministry": 30, "dept": 35,
        "quantity": 10, "start_date": 20, "end_date": 20,
        "keyword": 22, "url": 50,
    }
    for col_idx, field in enumerate(FIELDNAMES, 1):
        ws.column_dimensions[ws.cell(1, col_idx).column_letter].width = col_widths.get(field, 15)

    wb.save(RELEVANT_BIDS_XLSX)


def _load_relevant_xlsx() -> list[dict]:
    if os.path.exists(RELEVANT_BIDS_XLSX):
        from openpyxl import load_workbook
        wb = load_workbook(RELEVANT_BIDS_XLSX, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            return []
        headers = [str(h) for h in rows[0]]
        return [
            {k: (str(v) if v is not None else "") for k, v in zip(headers, row)}
            for row in rows[1:]
        ]
    # Fallback: old CSV
    old_csv = "relevant_tenders.csv"
    if os.path.exists(old_csv):
        with open(old_csv, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    return []


# ── Sync run ──────────────────────────────────────────────────────────────────
def _run_sync() -> None:
    try:
        # Load local DB first — stop condition for GeM scan
        existing_all: list[dict] = []
        if os.path.exists(ALL_BIDS_CSV):
            with open(ALL_BIDS_CSV, newline="", encoding="utf-8") as f:
                existing_all = list(csv.DictReader(f))
        seen_nos = {r["bid_no"] for r in existing_all}

        with _state_lock:
            _state.update(
                running=True, progress=0, total=0, error=None,
                current_kw="Scanning GeM for new bids…",
                mode="sync",
            )
        _broadcast({"type": "start", "total": 0, "mode": "sync"})

        pages_done = [0]

        def on_page(page, total_on_gem, new_count):
            pages_done[0] = page
            with _state_lock:
                _state["progress"] = page
                _state["current_kw"] = f"Page {page} · {new_count} new bids found"
            _broadcast({
                "type": "progress",
                "n": page,
                "total": 0,
                "keyword": f"Page {page} · {new_count} new bids found",
            })

        from gem_client import sync_fetch_new_bids
        new_bids = sync_fetch_new_bids(seen_nos, on_page=on_page)

        # ── Append new bids to all_bids_raw.csv ───────────────────────────────
        with _state_lock:
            _state["current_kw"] = f"Adding {len(new_bids)} new bids to {ALL_BIDS_CSV} …"

        all_bids = existing_all + new_bids

        with open(ALL_BIDS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_bids)

        # ── Re-filter full dataset against keywords ────────────────────────────
        with _state_lock:
            _state["current_kw"] = "Filtering against keywords…"

        positive, negative = load_keywords(KEYWORDS_FILE)
        neg_lower = [n.lower() for n in negative]

        matched: list[dict] = []
        for bid in all_bids:
            kw = keyword_match_local(bid, positive)
            if kw and not any(n in (bid.get("title") or "").lower() for n in neg_lower):
                bid_copy = dict(bid)
                bid_copy["keyword"] = kw
                matched.append(bid_copy)

        _write_relevant_xlsx(matched)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_state({"last_run": now, "mode": "sync", "total_bids": len(matched)})

        summary = {
            "mode": "sync",
            "keywords_searched": pages_done[0],
            "raw": len(new_bids),
            "deduped": len(new_bids),
            "relevant": len(matched),
            "capped_count": 0,
            "timestamp": now,
        }
        with _state_lock:
            _state.update(running=False, results=matched, last_run=now, last_summary=summary)

        _broadcast({"type": "done", **summary})

    except Exception as exc:
        with _state_lock:
            _state.update(running=False, error=str(exc))
        _broadcast({"type": "error", "msg": str(exc)})


# ── Get-Bid run (download docs + build Excel) ─────────────────────────────────
def _run_get_bid(date_str: str) -> None:
    try:
        with _get_bid_lock:
            _get_bid_state.update(
                running=True, progress=0, error=None,
                current_bid="", current_status="", date_filter=date_str,
                excel_path=None, folder=None,
            )

        with _state_lock:
            all_results = list(_state["results"])

        bids = [r for r in all_results if (r.get("end_date") or "")[:10] == date_str]

        with _get_bid_lock:
            _get_bid_state["total"] = len(bids)

        if not bids:
            with _get_bid_lock:
                _get_bid_state.update(running=False)
            return

        from pdf_analyzer import get_bid_documents, _optimal_workers

        with _get_bid_lock:
            _get_bid_state["workers"] = _optimal_workers(len(bids))

        counter = [0]
        counter_lock = threading.Lock()

        def on_progress(bid_no, status):
            with counter_lock:
                if status in ("done", "error"):
                    counter[0] += 1
                n = counter[0]
            with _get_bid_lock:
                _get_bid_state.update(
                    current_bid=bid_no,
                    current_status=status,
                    progress=n,
                )

        results, excel_path = get_bid_documents(bids, date_str, on_progress)
        folder = os.path.join("bid_documents", date_str)

        with _get_bid_lock:
            _get_bid_state.update(running=False, excel_path=excel_path, folder=folder)

    except Exception as exc:
        with _get_bid_lock:
            _get_bid_state.update(running=False, error=str(exc))


# ── Load existing data on startup ─────────────────────────────────────────────
def _load_on_startup() -> None:
    rows = _load_relevant_xlsx()
    if not rows:
        return
    src = RELEVANT_BIDS_XLSX if os.path.exists(RELEVANT_BIDS_XLSX) else "relevant_tenders.csv"
    mtime = datetime.fromtimestamp(os.path.getmtime(src)).isoformat() if os.path.exists(src) else None
    with _state_lock:
        _state["results"] = rows
        _state["last_run"] = mtime
    print(f"Loaded {len(rows):,} existing results from {src}")


_load_on_startup()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    with open(_INDEX_HTML, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/sync")
def start_sync():
    with _state_lock:
        if _state["running"]:
            return JSONResponse({"status": "already_running"}, status_code=409)
    threading.Thread(target=_run_sync, daemon=True).start()
    return {"status": "started", "mode": "sync"}


@app.get("/status")
def status():
    with _state_lock:
        return {
            "running":      _state["running"],
            "progress":     _state["progress"],
            "total":        _state["total"],
            "current_kw":   _state["current_kw"],
            "mode":         _state["mode"],
            "last_run":     _state["last_run"],
            "result_count": len(_state["results"]),
            "error":        _state["error"],
            "last_summary": _state["last_summary"],
        }


@app.get("/results")
def results():
    with _state_lock:
        return JSONResponse({
            "results":  _state["results"],
            "last_run": _state["last_run"],
        })


@app.get("/analyze/dates")
def analyze_dates():
    with _state_lock:
        res = list(_state["results"])
    dates = sorted(set(
        (r.get("end_date") or "")[:10]
        for r in res
        if (r.get("end_date") or "")[:10]
    ))
    return JSONResponse({"dates": dates})


@app.get("/download")
def download():
    if not os.path.exists(RELEVANT_BIDS_XLSX):
        return JSONResponse({"error": "No data yet. Run Sync first."}, status_code=404)
    return FileResponse(
        RELEVANT_BIDS_XLSX,
        filename="relevant_bids.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/get-bid")
def start_get_bid(date: str):
    with _get_bid_lock:
        if _get_bid_state["running"]:
            return JSONResponse({"status": "already_running"}, status_code=409)
    threading.Thread(target=_run_get_bid, args=(date,), daemon=True).start()
    return {"status": "started", "date": date}


@app.get("/get-bid/status")
def get_bid_status():
    with _get_bid_lock:
        return {
            "running":        _get_bid_state["running"],
            "progress":       _get_bid_state["progress"],
            "total":          _get_bid_state["total"],
            "current_bid":    _get_bid_state["current_bid"],
            "current_status": _get_bid_state["current_status"],
            "excel_path":     _get_bid_state["excel_path"],
            "folder":         _get_bid_state["folder"],
            "error":          _get_bid_state["error"],
            "date_filter":    _get_bid_state["date_filter"],
            "workers":        _get_bid_state["workers"],
        }


@app.get("/get-bid/download")
def download_get_bid():
    with _get_bid_lock:
        path = _get_bid_state.get("excel_path")
        date = _get_bid_state.get("date_filter", "bids")
    if not path or not os.path.exists(path):
        return JSONResponse({"error": "No Excel file ready yet."}, status_code=404)
    return FileResponse(
        path,
        filename=f"bids_{date}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/progress")
async def progress_stream(request: Request):
    q: queue.Queue = queue.Queue()
    with _clients_lock:
        _sse_clients.append(q)

    async def generator():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await loop.run_in_executor(None, lambda: q.get(timeout=5))
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg.get("type") in ("done", "error"):
                        break
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _clients_lock:
                try:
                    _sse_clients.remove(q)
                except ValueError:
                    pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
