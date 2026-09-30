"""
Load positive and negative keywords from the Keywords Excel file.

Expects two sheets: "Keywords" (col 2 = keyword) and
"Negative Keywords" (col 2 = negative keyword), matching the
structure of Keywords__2_.xlsx.
"""
import openpyxl


def load_keywords(xlsx_path: str):
    wb = openpyxl.load_workbook(xlsx_path, read_only=True)

    def _col(sheet_name, col_idx=1):
        ws = wb[sheet_name]
        values = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue  # skip header row
            val = row[col_idx]
            if val:
                values.append(str(val).strip())
        return values

    positive = _col("Keywords")
    negative = _col("Negative Keywords")
    return positive, negative


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "Keywords__2_.xlsx"
    pos, neg = load_keywords(path)
    print(f"Loaded {len(pos)} positive keywords, {len(neg)} negative keywords")
    print("Sample positive:", pos[:5])
    print("Sample negative:", neg[:5])
