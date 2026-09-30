"""
Benchmark: pdfplumber vs pypdfium2 for text extraction speed
Tests on cached PDFs only (no network) to isolate parser speed.
Usage: python benchmark_parser.py
"""
import os
import time
import glob

PDF_CACHE = "bid_pdfs"


def get_cached_pdfs(limit=10) -> list[str]:
    paths = glob.glob(os.path.join(PDF_CACHE, "*.pdf"))
    print(f"Found {len(paths)} cached PDFs, using first {min(limit, len(paths))}")
    return paths[:limit]


def bench_pdfplumber(paths: list[str]) -> float:
    import pdfplumber
    t0 = time.perf_counter()
    for path in paths:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        print(f"  [pdfplumber] {os.path.basename(path)} — {len(text)} chars")
    return time.perf_counter() - t0


def bench_pypdfium2(paths: list[str]) -> float:
    import pypdfium2 as pdfium
    t0 = time.perf_counter()
    for path in paths:
        doc = pdfium.PdfDocument(path)
        text = ""
        for page in doc:
            textpage = page.get_textpage()
            text += textpage.get_text_range()
        doc.close()
        print(f"  [pypdfium2]  {os.path.basename(path)} — {len(text)} chars")
    return time.perf_counter() - t0


def bench_pymupdf(paths: list[str]) -> float:
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            print("  [pymupdf] not installed — skipping")
            return 0.0
    t0 = time.perf_counter()
    for path in paths:
        doc = fitz.open(path)
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        print(f"  [pymupdf]    {os.path.basename(path)} — {len(text)} chars")
    return time.perf_counter() - t0


if __name__ == "__main__":
    paths = get_cached_pdfs(limit=10)
    if not paths:
        print("No cached PDFs found. Run the PDF analysis first to populate bid_pdfs/")
        raise SystemExit(1)

    print("\n" + "=" * 50)
    print("pdfplumber")
    print("=" * 50)
    t1 = bench_pdfplumber(paths)
    print(f"-> {t1:.2f}s  ({t1/len(paths):.2f}s per PDF)\n")

    print("=" * 50)
    print("pypdfium2  (already installed)")
    print("=" * 50)
    t2 = bench_pypdfium2(paths)
    print(f"-> {t2:.2f}s  ({t2/len(paths):.2f}s per PDF)\n")

    print("=" * 50)
    print("pymupdf  (pip install pymupdf)")
    print("=" * 50)
    t3 = bench_pymupdf(paths)
    if t3 > 0:
        print(f"-> {t3:.2f}s  ({t3/len(paths):.2f}s per PDF)\n")

    print("=" * 50)
    print("RESULTS")
    print("=" * 50)
    print(f"  pdfplumber: {t1:.2f}s  (1.0x baseline)")
    if t2: print(f"  pypdfium2:  {t2:.2f}s  ({t1/t2:.1f}x faster)")
    if t3: print(f"  pymupdf:    {t3:.2f}s  ({t1/t3:.1f}x faster)")
