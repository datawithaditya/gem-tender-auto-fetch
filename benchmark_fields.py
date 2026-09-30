"""
Verify that pypdfium2-based extraction captures the same fields as pdfplumber.
Usage: python benchmark_fields.py
"""
import glob, os, re

PDF_CACHE = "bid_pdfs"

FIELDS = [
    ("Bid Number",        r"Bid Number\s*[:\-]?\s*(GEM/\d+/[BR]/\d+)"),
    ("End Date",          r"Bid End Date/Time\s+(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})"),
    ("Ministry",          r"Ministry/State Name\s+(.+)"),
    ("Department",        r"Department Name\s+(.+)"),
    ("Organisation",      r"Organisation Name\s+(.+)"),
    ("Total Quantity",    r"Total Quantity\s+(\d[\d,]*)"),
    ("MSE Preference",    r"MSE Purchase Preference\s+(Yes|No)"),
    ("Estimated Value",   r"Estimated Bid Value\s+([\d,]+)"),
    ("Delivery Days",     r"Delivery\s*Days?\s+(\d+)"),
    ("GST",               r"GST@?(\d+)%"),
]

def extract_pdfplumber(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)

def extract_pypdfium2(path):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    text = ""
    for page in doc:
        textpage = page.get_textpage()
        text += textpage.get_text_range()
    doc.close()
    return text

def check_fields(text):
    results = {}
    for name, pattern in FIELDS:
        m = re.search(pattern, text)
        results[name] = m.group(1).strip()[:60] if m else "MISSING"
    return results

if __name__ == "__main__":
    paths = glob.glob(os.path.join(PDF_CACHE, "*.pdf"))[:5]
    if not paths:
        print("No cached PDFs found.")
        raise SystemExit(1)

    for path in paths:
        fname = os.path.basename(path)
        print(f"\n{'='*60}")
        print(f"FILE: {fname}")
        print(f"{'='*60}")

        t1_fields = check_fields(extract_pdfplumber(path))
        t2_fields = check_fields(extract_pypdfium2(path))

        print(f"{'Field':<20} {'pdfplumber':<35} {'pypdfium2':<35} Match")
        print("-" * 100)
        all_match = True
        for name, _ in FIELDS:
            v1 = t1_fields[name]
            v2 = t2_fields[name]
            match = "OK" if v1 == v2 else ("DIFF" if v1 != "MISSING" and v2 != "MISSING" else "MISS")
            if match != "OK":
                all_match = False
            print(f"  {name:<18} {v1:<35} {v2:<35} {match}")
        print(f"  -> {'ALL MATCH' if all_match else 'DIFFERENCES FOUND'}")
