"""
test_extract_laws.py — Tests for grouping page-level classifications into laws.

Run:
    pytest tests/test_extract_laws.py -v
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "doc_processing"))

from extract_laws import (
    choose_mode,
    extract_from_file,
    is_index_page,
    segment,
    strip_running_header,
)


def _entry(page, text, is_jim_crow="no", title="", racial_indicator="explicit", reviewed=True):
    return {
        "entry_id": f"test_p{page}",
        "source_filename": "Test_Acts_1900.pdf",
        "page_number": page,
        "year": 1900,
        "ocr_text": text,
        "citation": f"Test Acts, 1900, p. {page}",
        "reviewed": reviewed,
        "classification": {
            "is_jim_crow": is_jim_crow,
            "confidence": 0.9,
            "category": "education" if is_jim_crow == "yes" else None,
            "title": title,
            "summary": "",
            "keywords": [],
            "racial_indicator": racial_indicator if is_jim_crow == "yes" else "none",
            "needs_human_review": False,
            "reasoning": "",
        },
    }


# Ten acts so the volume is recognised as session laws.
FILLER = [
    _entry(10 + i, f"CHAPTER {100 + i}.\nAN ACT for the benefit of person {i}.\n§ 1. Relief granted.\nApproved March 1, 1900.")
    for i in range(10)
]

SESSION_PAGES = [
    _entry(1, "LAWS OF KENTUCKY . 1\nCHAPTER 7.\nAN ACT to provide schools in the town of Hope.\n"
              "Be it enacted by the General Assembly of the Commonwealth of Kentucky:\n"
              "§ 1. A board of trustees is created.", "no"),
    _entry(2, "LAWS OF KENTUCKY . 2\n§ 2. Only white children shall be admitted to this school.\n"
              "§ 3. This act shall take effect from its passage.\nApproved March 2, 1900.\n"
              "CHAPTER 8.\nAN ACT to incorporate the Hope Bridge Company.\n"
              "§ 1. The company may build a bridge.", "yes", title="Separate schools in Hope"),
    _entry(3, "LAWS OF KENTUCKY . 3\n§ 2. Tolls shall be reasonable.\nApproved March 3, 1900.", "no"),
    _entry(4, "INDEX .\nHope, act to provide schools in 1\nHope Bridge Company, act to incorporate 2\n"
              + "\n".join(f"Entry number {i} 4{i}" for i in range(15)), "yes"),
]


def _write(tmp_path, entries, document_type="session_laws"):
    path = tmp_path / "Test_Acts_1900_results_classified.json"
    path.write_text(json.dumps({
        "source_document": {"filename": "Test_Acts_1900.pdf", "title": "Test Acts", "year": 1900,
                            "document_type": document_type},
        "entries": entries,
    }))
    return path


class TestPageCleanup:
    def test_strips_running_header(self):
        assert strip_running_header("LAWS OF KENTUCKY . 349\n§ 8. Text")[0] == "§ 8. Text"

    def test_strips_chapter_running_header(self):
        assert strip_running_header("CHAPTER 1   31\n(h) Colored hospital")[0] == "(h) Colored hospital"

    def test_keeps_real_chapter_heading(self):
        assert strip_running_header("CHAPTER 64 .\nAN ACT for ...")[0] == "CHAPTER 64 ."

    def test_detects_index_page(self):
        assert is_index_page(SESSION_PAGES[3]["ocr_text"])

    def test_appropriation_table_is_not_index(self):
        text = "\n".join(f"(x) Item {i}. For ordinary expenses ${i},000.00" for i in range(20))
        assert not is_index_page(text)


class TestSegmentation:
    def test_session_laws_use_act_mode(self):
        assert choose_mode(SESSION_PAGES + FILLER, "session_laws") == "act"

    def test_codes_use_section_mode(self):
        pages = [_entry(i, f"§ {i}. A section.\n§ {i}a. Another.") for i in range(1, 5)]
        assert choose_mode(pages, "codes") == "section"

    def test_sparse_sections_fall_back_to_pages(self):
        pages = [_entry(i, "A dictionary entry about words.") for i in range(1, 20)]
        assert choose_mode(pages, "other") == "page"

    def test_act_spans_pages_and_stops_at_approved(self):
        units, _ = segment(SESSION_PAGES[:3], "act", set())
        assert [(u.number, u.pages) for u in units] == [("7", [1, 2]), ("8", [2, 3])]
        assert "Approved March 2, 1900." in units[0].text
        assert "Hope Bridge" not in units[0].text

    def test_act_without_spaces_in_ocr(self):
        units, _ = segment([_entry(1, "CHAPTER 584 .\nANACT toprovidefora registration.\n§1. Text.")], "act", set())
        assert len(units) == 1 and units[0].number == "584"

    def test_chapter_and_act_on_one_line(self):
        units, _ = segment([_entry(1, "CHAPTER 9. AN ACT concerning colored schools.\n§ 1. Text.")], "act", set())
        assert len(units) == 1 and units[0].number == "9"


class TestExtraction:
    def test_shared_page_goes_to_law_with_racial_language(self, tmp_path):
        laws, links, excluded = extract_from_file(_write(tmp_path, SESSION_PAGES + FILLER))
        assert len(laws) == 1
        law = laws[0]
        assert law["number"] == "7"
        assert law["page_start"] == 1 and law["page_end"] == 2
        assert law["jim_crow_pages"] == [2]
        assert law["attribution"] == ["race_terms"]
        assert "white children" in law["key_provisions"][0]
        assert law["citation"] == "Test Acts, 1900, ch. 7, pp. 1–2"

    def test_index_pages_are_excluded(self, tmp_path):
        _, _, excluded = extract_from_file(_write(tmp_path, SESSION_PAGES + FILLER))
        assert [x["page_number"] for x in excluded] == [4]

    def test_consecutive_flagged_pages_become_one_law(self, tmp_path):
        pages = [
            _entry(1, "CHAPTER 7.\nAN ACT to establish a school for colored children.\n§ 1. Text.", "yes"),
            _entry(2, "§ 2. The colored school shall be free.", "yes"),
            _entry(3, "§ 3. More text.\nApproved March 2, 1900.", "yes"),
        ]
        laws, links, _ = extract_from_file(_write(tmp_path, pages + FILLER))
        assert len(laws) == 1
        assert laws[0]["jim_crow_pages"] == [1, 2, 3]
        assert len(links) == 3

    def test_reviewer_fields_carry_over(self, tmp_path):
        pages = [_entry(1, "CHAPTER 7.\nAN ACT for negro schools.\nApproved March 2, 1900.", "yes")]
        pages[0]["reviewer_note"] = "checked against scan"
        laws, _, _ = extract_from_file(_write(tmp_path, pages + FILLER))
        assert laws[0]["reviewed"] is True
        assert laws[0]["reviewer_notes"] == ["checked against scan"]
