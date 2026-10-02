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
    group_editions,
    extract_from_file,
    is_index_page,
    segment,
    strip_running_header,
    summarize_statutes,
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


def _law(law_id, volume, year, unit_type, number, text, needs_review=False):
    return {
        "law_id": law_id, "source_filename": f"{volume}.pdf", "volume_title": volume, "year": year,
        "unit_type": unit_type, "number": number, "title": text[:40], "citation": f"{volume}, {year}, § {number}",
        "category": "education", "racial_indicator": "explicit", "summary": "", "needs_review": needs_review,
        "duplicate_of": None, "full_text": text,
    }


SEPARATE_SCHOOLS = ("Said board of education shall provide and maintain, out of the funds levied, "
                    "separate schools for white and colored pupils, and shall employ teachers for each, "
                    "and no white child shall attend a school for colored children nor any colored child "
                    "a school for white children within the limits of the said city.")


class TestGroupEditions:
    def test_same_ky_statutes_number_across_editions(self):
        laws = [
            _law("a", "The Kentucky Statutes, Third Edition", 1903, "section", "4477", "§ 4477. Free tuition to white children."),
            _law("b", "The Kentucky Statutes, Sixth Edition", 1922, "section", "4477", "§ 4477. A note on a case about tuition."),
            _law("c", "Common School Laws of Kentucky", 1916, "section", "155", "§ 155. (§ 4477 Ky. St.) Free Tuition."),
        ]
        group_editions(laws)
        assert len({l["statute_id"] for l in laws}) == 1
        assert laws[0]["statute_citation"] == "Ky. Stat. § 4477"
        assert laws[0]["statute_years"] == "1903–1922"

    def test_same_text_under_misread_number(self):
        laws = [
            _law("a", "The Kentucky Statutes, Third Edition", 1903, "section", "45214", "§ 45214. " + SEPARATE_SCHOOLS),
            _law("b", "The Kentucky Statutes, Fourth Edition", 1909, "section", "4521a", "§ 4521a. " + SEPARATE_SCHOOLS),
        ]
        group_editions(laws)
        assert laws[0]["statute_id"] == laws[1]["statute_id"]

    def test_parallel_sections_in_one_volume_stay_apart(self):
        laws = [
            _law("a", "The Kentucky Statutes, Third Edition", 1903, "section", "3470", "§ 3470. " + SEPARATE_SCHOOLS),
            _law("b", "The Kentucky Statutes, Third Edition", 1903, "section", "3596", "§ 3596. " + SEPARATE_SCHOOLS),
            _law("c", "The Kentucky Statutes, Sixth Edition", 1922, "section", "3470", "§ 3470. " + SEPARATE_SCHOOLS),
        ]
        possible = group_editions(laws)
        assert laws[0]["statute_id"] == laws[2]["statute_id"]
        assert laws[1]["statute_id"] != laws[0]["statute_id"]
        assert {p["law_id_a"] for p in possible} | {p["law_id_b"] for p in possible} >= {"b"}

    def test_acts_with_similar_wording_stay_apart(self):
        laws = [
            _law("a", "Acts 1920", 1920, "act", "95", "AN ACT appropriating money for colored schools."),
            _law("b", "Acts 1930", 1930, "act", "164", "AN ACT appropriating money for colored schools."),
            _law("c", "Acts 1930 v2", 1930, "act", "164", "AN ACT appropriating money for colored schools."),
        ]
        group_editions(laws)
        assert laws[0]["statute_id"] != laws[1]["statute_id"]
        assert laws[1]["statute_id"] == laws[2]["statute_id"]

    def test_statute_settled_if_any_printing_is(self):
        laws = [
            _law("a", "The Kentucky Statutes, Third Edition", 1903, "section", "4477", "§ 4477. Tuition.", needs_review=True),
            _law("b", "The Kentucky Statutes, Sixth Edition", 1922, "section", "4477", "§ 4477. Tuition."),
        ]
        group_editions(laws)
        [statute] = summarize_statutes(laws)
        assert statute["printings"] == 2 and statute["needs_review"] is False


class TestTitleCleaner:
    @classmethod
    def setup_class(cls):
        import pytest
        pytest.importorskip("wordninja")
        from text_cleanup import TitleCleaner
        domain = TitleCleaner.domain_words(
            ["Garrard County schools", "Cloverport school district", "Breckinridge County"] * 5
            + ["the said county shall provide schools for colored children"] * 50
        )
        cls.cleaner = TitleCleaner(domain)

    def test_splits_glued_words(self):
        assert (self.cleaner.clean("Be itenactedby theGeneral Assembly of theCommonwealth ofKentucky :")
                == "Be it enacted by the General Assembly of the Commonwealth of Kentucky:")

    def test_rejoins_line_break_hyphens(self):
        assert self.cleaner.clean("in the life- timeof theparties") == "in the lifetime of the parties"
        assert self.cleaner.clean("any religiousdenom- ination") == "any religious denomination"

    def test_keeps_catchline_dashes(self):
        assert (self.cleaner.clean("§ 283. Coloreddeafmutes- termsofadmission- appropriation.")
                == "§ 283. Colored deaf mutes—terms of admission—appropriation.")

    def test_fixes_section_symbol_and_spacing(self):
        assert self.cleaner.clean("$ 4534 . Appropriation for teachers .") == "§ 4534. Appropriation for teachers."

    def test_leaves_place_names_alone(self):
        assert self.cleaner.clean("public schools in Cloverport, Garrard county") == "public schools in Cloverport, Garrard county"

    def test_shall_not_hall(self):
        assert self.cleaner.clean("the purposeshallbe") == "the purpose shall be"


def test_shorten_cuts_at_word_boundary():
    from text_cleanup import shorten
    assert shorten("Separate schools for white and colored children", 20) == "Separate schools for…"
