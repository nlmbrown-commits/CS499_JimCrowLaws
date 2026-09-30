"""
export_laws_xlsx.py — Export the law extraction results to an Excel workbook.

Run after extract_laws.py:
    python3 doc_processing/export_laws_xlsx.py

Output: law_results/jim_crow_laws.xlsx with one sheet per view:
    Summary                          counts (formulas over the sheets below)
    Flagged Pages                    every page the LLM classified is_jim_crow = "yes"
    Index or Contents Pages          flagged pages set aside because they are not laws
    Laws Before Removing Duplicates  every extracted law
    Unique Laws                      laws with duplicate_of empty
    Marked for Human Check           unique laws with needs_review = true
"""

import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).parent.parent
RESULTS_DIR = ROOT / "doc_processing_results"
LAW_DIR = ROOT / "law_results"
OUT = LAW_DIR / "jim_crow_laws.xlsx"

EXCEL_CELL_LIMIT = 32767
FONT = "Arial"
HEADER_FILL = PatternFill("solid", start_color="1F3864")

# (header, key, width)
PAGE_COLUMNS = [
    ("Source File", "source_filename", 34), ("Volume", "volume_title", 40), ("Year", "year", 7),
    ("Page", "page_number", 7), ("Citation", "citation", 45), ("Page Title (LLM)", "title", 50),
    ("Category", "category", 18), ("Racial Indicator", "racial_indicator", 14),
    ("Confidence", "confidence", 11), ("Reviewed", "reviewed", 10), ("Reviewer Note", "reviewer_note", 30),
    ("Outcome", "outcome", 22), ("Law ID", "law_id", 45), ("Attribution", "attribution", 13),
    ("Summary (LLM)", "summary", 60), ("Page Text", "ocr_text", 80),
]
EXCLUDED_COLUMNS = [
    ("Source File", "source_filename", 34), ("Page", "page_number", 7),
    ("Reason", "reason", 26), ("Page Title (LLM)", "page_title", 60),
]
LAW_COLUMNS = [
    ("Law ID", "law_id", 45), ("Citation", "citation", 55), ("Title", "title", 60),
    ("Volume", "volume_title", 40), ("Year", "year", 7), ("Document Type", "document_type", 14),
    ("Unit Type", "unit_type", 10), ("Chapter / Section", "number", 12), ("Heading", "heading", 30),
    ("First Page", "page_start", 10), ("Last Page", "page_end", 10), ("Page Count", "page_count", 10),
    ("Flagged Pages", "jim_crow_pages", 18), ("Category", "category", 18),
    ("Racial Indicator", "racial_indicator", 14), ("Confidence", "confidence", 11),
    ("Summary (LLM)", "summary", 60), ("Key Provisions", "key_provisions", 80),
    ("Attribution", "attribution", 20), ("End Found", "boundary_found", 10), ("Reviewed", "reviewed", 10),
    ("Reviewer Notes", "reviewer_notes", 30), ("Needs Review", "needs_review", 12),
    ("Duplicate Of", "duplicate_of", 45), ("Source File", "source_filename", 34), ("Full Text", "full_text", 80),
]
WRAP_KEYS = {"title", "summary", "key_provisions", "full_text", "ocr_text", "page_title", "reviewer_note", "reviewer_notes"}


def cell_value(value):
    if isinstance(value, list):
        value = ("\n\n" if value and isinstance(value[0], str) and len(value[0]) > 80 else "; ").join(map(str, value))
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        if len(value) > EXCEL_CELL_LIMIT:
            value = value[:EXCEL_CELL_LIMIT - 40] + "\n[... truncated; see laws.json]"
    return value


def add_sheet(wb, name, columns, rows):
    ws = wb.create_sheet(name)
    for c, (header, _, width) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=c, value=header)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = width
    body = Font(name=FONT, size=10)
    wrap = Alignment(vertical="top", wrap_text=True)
    top = Alignment(vertical="top")
    for r, row in enumerate(rows, 2):
        for c, (_, key, _) in enumerate(columns, 1):
            cell = ws.cell(row=r, column=c, value=cell_value(row.get(key)))
            cell.font = body
            cell.alignment = wrap if key in WRAP_KEYS else top
            if key == "confidence":
                cell.number_format = "0.00"
        ws.row_dimensions[r].height = 45
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(len(rows) + 1, 1)}"
    return ws


def load_flagged_pages(laws_by_page, excluded_pages):
    rows = []
    for path in sorted(RESULTS_DIR.glob("*_classified.json")):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        source = data.get("source_document", {})
        filename = source.get("filename") or path.name
        for e in data.get("entries", []):
            c = e["classification"]
            if c.get("is_jim_crow") != "yes":
                continue
            key = (filename, e["page_number"])
            link = laws_by_page.get(key)
            outcome = ("Index or contents page (excluded)" if key in excluded_pages
                       else "Part of a law" if link else "Not attributed")
            rows.append({
                "source_filename": filename, "volume_title": source.get("title"), "year": e.get("year"),
                "page_number": e["page_number"], "citation": e.get("citation"), "title": c.get("title"),
                "category": c.get("category"), "racial_indicator": c.get("racial_indicator"),
                "confidence": c.get("confidence"), "reviewed": e.get("reviewed", False),
                "reviewer_note": e.get("reviewer_note"), "outcome": outcome,
                "law_id": link["law_id"] if link else None, "attribution": link["attribution"] if link else None,
                "summary": c.get("summary"), "ocr_text": e.get("ocr_text"),
            })
    return rows


def main():
    with open(LAW_DIR / "laws.json", encoding="utf-8") as f:
        data = json.load(f)
    laws, excluded = data["laws"], data["excluded_pages"]

    laws_by_page = {}
    for law in laws:
        for page in law["jim_crow_pages"]:
            laws_by_page[(law["source_filename"], page)] = {
                "law_id": law["law_id"], "attribution": "; ".join(law["attribution"])}
    excluded_keys = {(x["source_filename"], x["page_number"]) for x in excluded}
    flagged = load_flagged_pages(laws_by_page, excluded_keys)

    unique = [l for l in laws if not l.get("duplicate_of")]
    review = [l for l in unique if l.get("needs_review")]

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    sheets = [
        ("Flagged Pages", PAGE_COLUMNS, flagged, "Every page the LLM classified as Jim Crow"),
        ("Index or Contents Pages", EXCLUDED_COLUMNS, excluded, "Flagged pages set aside: they list laws rather than contain them"),
        ("Laws Before Removing Duplicates", LAW_COLUMNS, laws, "Every law extracted from the flagged pages"),
        ("Unique Laws", LAW_COLUMNS, unique, "Laws with no identical earlier copy (Duplicate Of is empty)"),
        ("Marked for Human Check", LAW_COLUMNS, review, "Unique laws with Needs Review = Yes"),
    ]
    for name, cols, rows, _ in sheets:
        add_sheet(wb, name, cols, rows)

    bold = Font(name=FONT, bold=True)
    body = Font(name=FONT)
    summary["A1"] = "Kentucky Jim Crow Laws — Law Extraction Results"
    summary["A1"].font = Font(name=FONT, bold=True, size=14)
    for c, h in enumerate(["Sheet", "Rows", "What it contains"], 1):
        cell = summary.cell(row=3, column=c, value=h)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
    for r, (name, _, _, desc) in enumerate(sheets, 4):
        summary.cell(row=r, column=1, value=name).font = body
        summary.cell(row=r, column=2, value=f"=COUNTA('{name}'!A:A)-1").font = body
        summary.cell(row=r, column=3, value=desc).font = body
    r = 4 + len(sheets) + 1
    summary.cell(row=r, column=1, value="Flagged pages attributed to a law").font = body
    summary.cell(row=r, column=2, value="=COUNTIF('Flagged Pages'!L:L,\"Part of a law\")").font = body
    summary.cell(row=r + 1, column=1, value="Laws by type (unique)").font = bold
    for i, t in enumerate(["act", "section", "page"]):
        summary.cell(row=r + 2 + i, column=1, value=f"  {t}").font = body
        summary.cell(row=r + 2 + i, column=2, value=f"=COUNTIF('Unique Laws'!G:G,\"{t}\")").font = body
    notes_row = r + 6
    notes = [
        "Notes",
        "Source: law_results/laws.json, produced by doc_processing/extract_laws.py from doc_processing_results/.",
        "A law is one act in session-law volumes, one numbered section (§) in compiled codes, or one page where neither structure exists.",
        f"Cells longer than Excel's {EXCEL_CELL_LIMIT:,}-character limit are truncated; laws.json has the full text.",
    ]
    for i, n in enumerate(notes):
        summary.cell(row=notes_row + i, column=1, value=n).font = bold if i == 0 else body
    summary.column_dimensions["A"].width = 36
    summary.column_dimensions["B"].width = 10
    summary.column_dimensions["C"].width = 70

    wb.save(OUT)
    print(f"Wrote {OUT}")
    for name, _, rows, _ in sheets:
        print(f"  {name:<32}: {len(rows)}")


if __name__ == "__main__":
    main()
