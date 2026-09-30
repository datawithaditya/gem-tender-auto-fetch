"""
PDF analysis module for GeM bid documents.

Downloads PDFs from bid URLs, extracts structured fields using pdfplumber,
and returns per-bid summaries suitable for sales team review.
"""
import os
import re
import threading
import time
import requests

_thread_local = threading.local()

PDF_CACHE_DIR = "bid_pdfs"


def _make_session() -> requests.Session:
    from gem_client import HEADERS, SESSION_URL, REQUEST_DELAY_SECONDS
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(SESSION_URL, timeout=8)
        time.sleep(REQUEST_DELAY_SECONDS)
    except Exception:
        pass
    return s


def _thread_session() -> requests.Session:
    """Return a per-thread session, creating it on first use."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = _make_session()
    return _thread_local.session


def download_pdf(url: str, bid_no: str, session: requests.Session) -> str | None:
    """Download PDF to local cache. Returns local path or None on failure."""
    os.makedirs(PDF_CACHE_DIR, exist_ok=True)
    fname = bid_no.replace("/", "_") + ".pdf"
    path = os.path.join(PDF_CACHE_DIR, fname)
    if os.path.exists(path):
        return path
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "pdf" not in ct.lower() and len(resp.content) < 1000:
            return None
        with open(path, "wb") as f:
            f.write(resp.content)
        return path
    except Exception as exc:
        print(f"  PDF download failed ({bid_no}): {exc}")
        return None


def _find(text: str, pattern: str, flags: int = 0, default: str = "") -> str:
    m = re.search(pattern, text, flags)
    if not m:
        return default
    return " ".join(m.group(1).split())


def _extract_bid_to_ra(text: str) -> str:
    m = re.search(r"Bid\s+to\s+RA\s+[Ee]nabled\s*[:\-]?\s*(Yes|No)", text, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_emd(text: str) -> tuple[str, str]:
    """Returns (emd_required, emd_detail)."""
    exempt = re.search(r"EMD\s+Exemption\s+Reason\s*[:\-]?\s*([^\n]+)", text, re.IGNORECASE)
    if exempt:
        return "Exempted", exempt.group(1).strip()[:120]
    amt = re.search(r"EMD\s+(?:Amount|Value)\s*[:\-]?\s*([\d,₹Rs. ]+)", text, re.IGNORECASE)
    if amt:
        return "Yes", amt.group(1).strip()[:60]
    yn = re.search(r"EMD\s+Required\s*[:\-]?\s*(Yes|No)", text, re.IGNORECASE)
    if yn:
        return yn.group(1), ""
    if re.search(r"EMD\s+Detail", text, re.IGNORECASE):
        return "Yes", ""
    return "", ""


def _extract_text(path: str) -> str:
    """Extract full text from a PDF using pypdfium2 (15x faster than pdfplumber)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    text = ""
    for page in doc:
        textpage = page.get_textpage()
        text += textpage.get_text_range()
    doc.close()
    return text


def parse_pdf(path: str) -> dict:
    """Extract key fields from a GeM bid PDF."""
    text = _extract_text(path)

    f = lambda pat, flags=0, default="": _find(text, pat, flags, default)

    # ── Core bid details ──────────────────────────────────────────
    bid_no    = f(r"Bid Number\s*[:\-]?\s*(GEM/\d+/[BR]/\d+)")
    end_date  = f(r"Bid End Date/Time\s+(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})")
    ministry  = f(r"Ministry/State Name\s+(.+)")
    dept      = f(r"Department Name\s+(.+)")
    org       = f(r"Organisation Name\s+(.+)")
    office    = f(r"Office Name\s+(.+)")
    qty       = f(r"Total Quantity\s+(\d[\d,]*)")
    bid_type  = f(r"Type of Bid\s+(.+)")
    eval_meth = f(r"Evaluation Method\s+(.+)")
    boq_title = f(r"BOQ Title\s+(.+)")
    est_value = f(r"Estimated Bid Value\s+([\d,]+)")

    # ── Item category (multi-line until next known label) ─────────
    item_cat = f(
        r"Item Category\s+(.+?)(?=GeMARPTS|Searched String|Searched Result)",
        flags=re.DOTALL,
    )

    # ── Grievance / contact — extract all emails ──────────────────
    grievance_block = f(
        r"Grievance redressal\s+(.{0,300}?)(?=Total Quantity|कुल मात्रा|कुल मा)",
        flags=re.DOTALL,
    )
    grievance_emails = re.findall(r"[\w.\-+]+@[\w.\-]+\.\w+", grievance_block)
    grievance = "; ".join(grievance_emails) if grievance_emails else grievance_block.strip()[:120]

    # ── Documents required ────────────────────────────────────────
    docs = f(
        r"Document required\s+from seller\s+(.+?)(?=\*In case|In case any|EMD Detail|बड)",
        flags=re.DOTALL,
    )

    # ── MSE & limited tender ──────────────────────────────────────
    mse      = f(r"MSE Purchase Preference\s+(Yes|No)", default="No")
    limited  = f(r"Limited Tender Applicable\s+(Yes|No)", default="No")

    # ── Delivery days — require 2+ digits to avoid matching page/row "1" ──
    delivery = f(r"Delivery\s*Days?\s+(\d{2,})")

    # ── GST rate ──────────────────────────────────────────────────
    gst = f(r"GST@?(\d+)%")
    gst_str = gst + "%" if gst else ""

    # ── ATC contact email (from clarification clause) ─────────────
    atc_emails = re.findall(
        r"(?:contact|clarification)[^@\n]{0,60}?([\w.\-]+@[\w.\-]+\.(?:gov\.in|in|com|org))",
        text, re.IGNORECASE
    )
    atc_contact = "; ".join(dict.fromkeys(atc_emails))  # dedupe, preserve order

    bid_to_ra = _extract_bid_to_ra(text)
    emd_req, emd_detail = _extract_emd(text)

    return {
        "bid_no":          bid_no,
        "bid_end_date":    end_date,
        "ministry":        ministry,
        "department":      dept,
        "organisation":    org,
        "office":          office,
        "total_quantity":  qty,
        "item_category":   item_cat,
        "grievance":       grievance,
        "documents":       docs,
        "mse_preference":  mse,
        "limited_tender":  limited,
        "delivery_days":   delivery,
        "gst_rate":        gst_str,
        "atc_contact":     atc_contact,
        "evaluation_method": eval_meth,
        "bid_type":        bid_type,
        "bid_to_ra":       bid_to_ra,
        "boq_title":       boq_title,
        "estimated_value": est_value,
        "emd_required":    emd_req,
        "emd_detail":      emd_detail,
    }


def _optimal_workers(n_bids: int) -> int:
    """
    Pick worker count based on available CPU and RAM.
    pypdfium2 (C extension) releases the GIL, so threads give true parallelism.
    RAM estimate: ~50 MB per worker (pypdfium2 is much lighter than pdfplumber).
    """
    cpus = os.cpu_count() or 1
    try:
        import psutil
        available_mb = psutil.virtual_memory().available // (1024 * 1024)
        mem_workers = max(1, available_mb // 50)
        workers = min(cpus, mem_workers, 12)
    except ImportError:
        workers = min(cpus, 8)
    workers = max(1, workers)
    workers = min(workers, n_bids)
    print(f"PDF analysis: {n_bids} bids, {workers} workers (cpus={os.cpu_count()})")
    return workers


def _analyze_one(bid: dict, on_progress=None) -> dict:
    from gem_client import REQUEST_DELAY_SECONDS

    bid_no = bid.get("bid_no", "?")
    url    = bid.get("url", "")

    # Session is created once per thread on first call, then reused
    session = _thread_session()

    if on_progress:
        on_progress(bid_no, "downloading")

    path = download_pdf(url, bid_no, session)
    time.sleep(REQUEST_DELAY_SECONDS)

    if not path:
        if on_progress:
            on_progress(bid_no, "error")
        return {"bid_no": bid_no, "title": bid.get("title", ""), "url": url,
                "error": "PDF download failed"}

    if on_progress:
        on_progress(bid_no, "parsing")

    try:
        parsed = parse_pdf(path)
        parsed["url"]     = url
        parsed["title"]   = bid.get("title", "")
        parsed["keyword"] = bid.get("keyword", "")
        if on_progress:
            on_progress(bid_no, "done")
        return parsed
    except Exception as exc:
        if on_progress:
            on_progress(bid_no, "error")
        return {"bid_no": bid_no, "title": bid.get("title", ""),
                "url": url, "error": str(exc)}


def _write_excel(results: list[dict], path: str) -> None:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bids"

    headers = [
        "Bid No", "Title", "Ministry", "Department", "Quantity",
        "Start Date", "End Date", "Keyword",
        "Bid Type", "Bid to RA Enabled", "Evaluation Method",
        "EMD Required", "EMD Detail",
        "Delivery Days", "Estimated Value (Rs)",
        "MSE Preference", "GST Rate",
        "Documents Required", "Grievance / Contact", "URL",
    ]
    keys = [
        "bid_no", "title", "ministry", "dept", "quantity",
        "start_date", "end_date", "keyword",
        "bid_type", "bid_to_ra", "evaluation_method",
        "emd_required", "emd_detail",
        "delivery_days", "estimated_value",
        "mse_preference", "gst_rate",
        "documents", "grievance", "url",
    ]
    col_widths = [22, 42, 28, 28, 10, 14, 14, 14,
                  18, 16, 26, 14, 30,
                  12, 18, 14, 10,
                  38, 32, 45]

    hdr_fill = PatternFill("solid", fgColor="1F3864")
    hdr_font = Font(bold=True, color="FFFFFF", size=11)

    for col, (header, width) in enumerate(zip(headers, col_widths), 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col)].width = width

    ws.row_dimensions[1].height = 32

    for row_idx, result in enumerate(results, 2):
        for col, key in enumerate(keys, 1):
            val = result.get(key, "") or ""
            ws.cell(row=row_idx, column=col, value=val)

    ws.freeze_panes = "A2"
    wb.save(path)


def get_bid_documents(
    bids: list[dict],
    date_str: str,
    on_progress=None,
) -> tuple[list[dict], str]:
    """
    Download all bid documents to bid_documents/{date_str}/, parse them,
    and produce an Excel summary file.

    Returns (results, excel_path).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from gem_client import REQUEST_DELAY_SECONDS

    folder = os.path.join("bid_documents", date_str)
    os.makedirs(folder, exist_ok=True)

    if not bids:
        return [], ""

    workers = _optimal_workers(len(bids))

    # Phase 1: download documents in parallel into the date folder
    def _download_doc(bid):
        session = _thread_session()
        bid_no  = bid.get("bid_no", "?")
        url     = bid.get("url", "")
        safe    = bid_no.replace("/", "_")

        if on_progress:
            on_progress(bid_no, "downloading")

        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").lower()
            ext = ".xlsx" if ("excel" in ct or "spreadsheet" in ct or "xls" in ct) else ".pdf"
            doc_path = os.path.join(folder, safe + ext)
            with open(doc_path, "wb") as f:
                f.write(resp.content)
            # also keep in PDF cache for reuse
            if ext == ".pdf":
                cache_path = os.path.join(PDF_CACHE_DIR, safe + ".pdf")
                if not os.path.exists(cache_path):
                    os.makedirs(PDF_CACHE_DIR, exist_ok=True)
                    with open(cache_path, "wb") as f2:
                        f2.write(resp.content)
        except Exception as exc:
            print(f"  Download failed ({bid_no}): {exc}")
            doc_path = None
            ext = None

        time.sleep(REQUEST_DELAY_SECONDS)
        return bid, doc_path, ext

    doc_paths: dict[str, tuple] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fmap = {pool.submit(_download_doc, bid): bid for bid in bids}
        for fut in as_completed(fmap):
            bid, path, ext = fut.result()
            doc_paths[bid["bid_no"]] = (bid, path, ext)

    # Phase 2: parse sequentially
    results = []
    for bid in bids:
        bid_no = bid.get("bid_no", "?")
        _, path, ext = doc_paths.get(bid_no, (bid, None, None))

        if not path:
            results.append({**bid, "error": "Download failed"})
            if on_progress:
                on_progress(bid_no, "error")
            continue

        if on_progress:
            on_progress(bid_no, "parsing")

        try:
            parsed = parse_pdf(path) if ext == ".pdf" else {}
            row = {
                "bid_no":    bid.get("bid_no", ""),
                "title":     bid.get("title", ""),
                "ministry":  bid.get("ministry", ""),
                "dept":      bid.get("dept", ""),
                "quantity":  bid.get("quantity", ""),
                "start_date": bid.get("start_date", ""),
                "end_date":  bid.get("end_date", ""),
                "keyword":   bid.get("keyword", ""),
                "url":       bid.get("url", ""),
                "bid_type":          parsed.get("bid_type", ""),
                "bid_to_ra":         parsed.get("bid_to_ra", ""),
                "evaluation_method": parsed.get("evaluation_method", ""),
                "emd_required":      parsed.get("emd_required", ""),
                "emd_detail":        parsed.get("emd_detail", ""),
                "delivery_days":     parsed.get("delivery_days", ""),
                "estimated_value":   parsed.get("estimated_value", ""),
                "mse_preference":    parsed.get("mse_preference", ""),
                "gst_rate":          parsed.get("gst_rate", ""),
                "documents":         parsed.get("documents", ""),
                "grievance":         parsed.get("grievance", ""),
            }
            results.append(row)
            if on_progress:
                on_progress(bid_no, "done")
        except Exception as exc:
            results.append({**bid, "error": str(exc)})
            if on_progress:
                on_progress(bid_no, "error")

    excel_path = os.path.join(folder, f"bids_{date_str}.xlsx")
    _write_excel(results, excel_path)

    return results, excel_path


def analyze_bids(bids: list[dict], on_progress=None) -> tuple[list[dict], int]:
    """
    Two-phase pipeline:
      Phase 1 — parallel downloads via ThreadPoolExecutor (I/O-bound, thread-safe)
      Phase 2 — sequential parsing with pypdfium2 (not thread-safe; fast enough at ~0.5s/PDF)

    Returns (results, workers_used).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from gem_client import REQUEST_DELAY_SECONDS

    if not bids:
        return [], 0

    workers = _optimal_workers(len(bids))

    # ── Phase 1: download all PDFs in parallel ────────────────────
    def _download(bid):
        session = _thread_session()
        bid_no = bid.get("bid_no", "?")
        if on_progress:
            on_progress(bid_no, "downloading")
        path = download_pdf(bid.get("url", ""), bid_no, session)
        time.sleep(REQUEST_DELAY_SECONDS)
        return bid, path

    paths: dict[str, tuple[dict, str | None]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_bid = {pool.submit(_download, bid): bid for bid in bids}
        for fut in as_completed(future_to_bid):
            bid, path = fut.result()
            paths[bid["bid_no"]] = (bid, path)
            if not path and on_progress:
                on_progress(bid.get("bid_no", "?"), "error")

    # ── Phase 2: parse sequentially (pypdfium2 not thread-safe) ──
    results = []
    for bid in bids:
        bid_no = bid.get("bid_no", "?")
        saved_bid, path = paths.get(bid_no, (bid, None))

        if not path:
            results.append({"bid_no": bid_no, "title": bid.get("title", ""),
                            "url": bid.get("url", ""), "error": "PDF download failed"})
            continue

        if on_progress:
            on_progress(bid_no, "parsing")
        try:
            parsed = parse_pdf(path)
            parsed["url"]     = bid.get("url", "")
            parsed["title"]   = bid.get("title", "")
            parsed["keyword"] = bid.get("keyword", "")
            results.append(parsed)
            if on_progress:
                on_progress(bid_no, "done")
        except Exception as exc:
            results.append({"bid_no": bid_no, "title": bid.get("title", ""),
                            "url": bid.get("url", ""), "error": str(exc)})
            if on_progress:
                on_progress(bid_no, "error")

    return results, workers
