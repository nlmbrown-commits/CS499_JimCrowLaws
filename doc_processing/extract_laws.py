"""
extract_laws.py — Rebuild page-level classifications into complete laws.

The processor classifies one page at a time, so a single act that runs over
five pages shows up as five "Jim Crow" entries, and a page that holds the end
of one act and the start of another is credited to both. This script segments
every volume into its actual statutory units and maps each flagged page onto
the unit(s) it belongs to:

  * session laws  → one law per act   (CHAPTER n. / AN ACT ... → Approved ...)
  * compiled codes → one law per section (§ 4363. / SEC. 12. → next section)

Each flagged page is attributed to the unit on that page that contains racial
language, or, when the page is implicit, to the unit that best matches the
page's LLM title/keywords. Index and table-of-contents pages are set aside.
The reviewer's page-level labels (reviewed, reviewer_note, category) are
carried onto the law.

Usage:
    python3 doc_processing/extract_laws.py
    python3 doc_processing/extract_laws.py --verify   # re-classify each law's
                                                      # full text with the LLM

Output (law_results/):
    laws.json        one record per law, with full text and provenance
    laws.csv         same, flattened for spreadsheets
    statutes.csv     one row per distinct statute (all printings of it grouped)
    possible_matches.csv  section pairs that may be the same statute, for review
    page_to_law.csv  every flagged page → the law it was attributed to
    excluded_pages.csv  flagged pages that are not laws (index/contents)
"""

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from text_cleanup import TitleCleaner, shorten, wordninja  # noqa: E402

ROOT = Path(__file__).parent.parent
RESULTS_DIR = ROOT / "doc_processing_results"
OUT_DIR = ROOT / "law_results"

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# "CHAPTER 64." / "# CHAPTER 399" / "CHAPTER CCXII."
CHAPTER_RE = re.compile(r"^\s*#*\s*CHAP(?:TER|\.)\s+([\dIVXLC]+)\b")
# "AN ACT to ..." — case-sensitive so running prose ("an act to amend") is skipped.
# Some volumes lose the spaces in OCR ("ANACT toprovide..."), so the space is optional.
ACT_RE = re.compile(r"^\s*#*\s*(?:CHAP(?:TER|\.)\s+[\dIVXLC]+\s*\.?\s*)?AN\s*ACT(?![A-Z])")
# "§ 11." / "$ 4363." (OCR for §) / "SEC. 12." / "Section 4363-8."
SECTION_RE = re.compile(
    r"^\s*#*\s*(?:§+|\$|SEC\.?|SECTION)\s*(\d+[a-zA-Z]?(?:-\d+[a-zA-Z]?)?)\s*\.(?!\d)",
    re.IGNORECASE,
)
# "ARTICLE VII." — a heading in codes and constitutions
ARTICLE_RE = re.compile(r"^\s*#*\s*ARTICLE\s+([\dIVXLC]+)\b", re.IGNORECASE)
# "Approved March 25, 1908." — the last line of a session-law act
APPROVED_RE = re.compile(r"^\s*(?:Approved|Became a law)\b.*\d{4}", re.IGNORECASE)
# Running page headers: "LAWS OF KENTUCKY . 349", "169 COMMONWEALTH OF KENTUCKY"
RUNNING_HEADER_RE = re.compile(r"^\s*(?:\d+\s+[A-Z][A-Z .,'&-]{6,}|[A-Z][A-Z .,'&-]{6,}\.?\s*\d+)\s*$")
# "CHAPTER 1   31" — later volumes print the current chapter and page number atop every page
CHAPTER_HEADER_RE = re.compile(r"^\s*(?:\d+\s+)?CHAP(?:TER|\.)\s+[\dIVXLC]+\s*\.?\s+\d+\s*$")
# "INDEX .", "viii CONTENTS", "72 [Doc. No 7.] COMMON SCHOOL LAWS - INDEX ."
INDEX_HEADER_RE = re.compile(r"\b(?:INDEX|CONTENTS)\b")
# Index entries end in a page reference ("...Paducah, act to charter the 54");
# dollar amounts in appropriation tables must not count.
ENDS_WITH_NUMBER_RE = re.compile(r"(?:^|[\s.,])\d+(?:\s*(?:,|to|-)\s*\d+)*\s*$")

RACE_RE = re.compile(
    r"\b(?:colou?red|negro(?:es)?|mulatto|white|black|african|race|races|racial|"
    r"slaves?|slavery|freedm[ae]n|persons? of colou?r|miscegenation|separate\s+(?:schools?|coach|cars?))\b",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[a-z]{4,}")
STOPWORDS = {
    "that", "this", "with", "from", "shall", "said", "such", "have", "which", "been",
    "their", "they", "them", "there", "other", "into", "upon", "same", "under", "being",
    "kentucky", "commonwealth", "act", "acts", "section", "sections", "regulations", "law",
    "laws", "public", "town", "city", "county", "state", "general", "assembly",
}

# Documents whose statutes are cited by section rather than by act.
SECTION_DOCUMENT_TYPES = {"codes", "state_constitution", "constitution", "civil_laws", "criminal_laws"}
MIN_ACTS_FOR_ACT_MODE = 10
MIN_ACTS_PER_PAGE = 0.1
# Below this many section headings per page the volume isn't organised by section
# (e.g. the Judicial Dictionary), so each flagged page stands alone.
MIN_SECTIONS_PER_PAGE = 0.3
MAX_SECTION_PAGES = 8  # a code section longer than this means a missed heading
LONG_LAW_PAGES = 40    # an act longer than this is worth a human look
MAX_KEY_PROVISIONS = 5


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Line:
    page: int
    text: str


@dataclass
class Unit:
    kind: str                     # "act" | "section" | "page"
    number: Optional[str]         # chapter or section number
    heading: str                  # enclosing ARTICLE/CHAPTER heading (codes)
    lines: List[Line] = field(default_factory=list)
    closed: bool = False          # an "Approved ..." line (or next unit) ended it

    @property
    def pages(self) -> List[int]:
        return sorted({l.page for l in self.lines})

    def text_on(self, page: int) -> str:
        return "\n".join(l.text for l in self.lines if l.page == page)

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)


# ---------------------------------------------------------------------------
# Page cleanup
# ---------------------------------------------------------------------------

def is_heading_line(line: str) -> bool:
    return bool(CHAPTER_RE.match(line) or ACT_RE.match(line) or SECTION_RE.match(line) or ARTICLE_RE.match(line))


def strip_running_header(text: str) -> List[str]:
    """Drop the running page header (first non-empty line) if it looks like one."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if CHAPTER_HEADER_RE.match(line) or (RUNNING_HEADER_RE.match(line) and not is_heading_line(line)):
            return lines[:i] + lines[i + 1:]
        break
    return lines


def is_index_page(text: str) -> bool:
    """True for back-of-book index and table-of-contents pages."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return False
    if any(len(l) < 70 and INDEX_HEADER_RE.search(l) for l in lines[:2]):
        return True
    if len(lines) < 15:
        return False
    numeric = sum(1 for l in lines if len(l) < 70 and "$" not in l and ENDS_WITH_NUMBER_RE.search(l))
    return numeric / len(lines) >= 0.5


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def choose_mode(entries: List[dict], document_type: Optional[str]) -> str:
    """'act' for session-law volumes, 'section' for compiled codes, 'page' otherwise."""
    lines = [l for e in entries for l in (e.get("ocr_text") or "").split("\n")]
    pages = max(len(entries), 1)
    acts = sum(1 for l in lines if ACT_RE.match(l))
    if (document_type not in SECTION_DOCUMENT_TYPES
            and acts >= MIN_ACTS_FOR_ACT_MODE and acts / pages >= MIN_ACTS_PER_PAGE):
        return "act"
    sections = sum(1 for l in lines if SECTION_RE.match(l))
    return "section" if sections / pages >= MIN_SECTIONS_PER_PAGE else "page"


def segment(entries: List[dict], mode: str, index_pages: set) -> Tuple[List[Unit], Dict[int, List[str]]]:
    """
    Split a volume into statutory units.

    Returns the units plus the cleaned lines of every page (used for pages that
    fall outside any unit). Index pages act as hard boundaries and belong to no unit.
    """
    units: List[Unit] = []
    page_lines: Dict[int, List[str]] = {}
    current: Optional[Unit] = None
    heading = ""
    pending_chapter: Optional[Tuple[str, List[Line]]] = None  # CHAPTER line awaiting its AN ACT

    def close():
        nonlocal current
        if current is not None:
            units.append(current)
        current = None

    for entry in sorted(entries, key=lambda e: e["page_number"]):
        page = entry["page_number"]
        lines = strip_running_header(entry.get("ocr_text") or "")
        page_lines[page] = lines
        if mode == "page":
            continue
        if current is not None and current.kind == "section" and page - current.lines[0].page >= MAX_SECTION_PAGES:
            close()
        if page in index_pages:
            close()
            pending_chapter = None
            continue

        for i, raw in enumerate(lines):
            line = Line(page, raw)

            if mode == "act":
                m = CHAPTER_RE.match(raw)
                if m and not ACT_RE.match(raw):
                    pending_chapter = (m.group(1), [line])
                    continue
                if ACT_RE.match(raw):
                    close()
                    number, prefix = pending_chapter if pending_chapter else (m.group(1) if m else None, [])
                    current = Unit("act", number, "", list(prefix))
                    pending_chapter = None
                    current.lines.append(line)
                    continue
                if pending_chapter is not None:
                    # Title lines between "CHAPTER n." and "AN ACT" (usually 0–3 lines)
                    pending_chapter[1].append(line)
                    if len(pending_chapter[1]) > 6:
                        pending_chapter = None
                    continue
                if current is not None and not current.closed:
                    current.lines.append(line)
                    if APPROVED_RE.match(raw):
                        current.closed = True
                continue

            # section mode
            m = ARTICLE_RE.match(raw) or CHAPTER_RE.match(raw)
            if m:
                close()
                following = next((l.strip() for l in lines[i + 1:i + 3] if l.strip()), "")
                heading = f"{raw.strip()} {following}".strip()
                continue
            m = SECTION_RE.match(raw)
            if m:
                close()
                current = Unit("section", m.group(1), heading, [line])
                continue
            if current is not None:
                current.lines.append(line)

    close()
    for u in units:
        u.closed = u.closed or u.kind == "section"
    return units, page_lines


# ---------------------------------------------------------------------------
# Attribution: which unit on a flagged page is the Jim Crow one?
# ---------------------------------------------------------------------------

def _words(text: str) -> set:
    return {w for w in WORD_RE.findall(text.lower()) if w not in STOPWORDS}


def attribute_page(page: int, cls: dict, candidates: List[Unit]) -> Tuple[List[Unit], str]:
    """
    Pick the unit(s) a flagged page's Jim Crow finding belongs to.

    Returns (units, method) where method is one of
      sole_unit   — only one law appears on the page
      race_terms  — the law(s) on the page that use racial language
      title_match — implicit page; law whose text best matches the LLM's title/keywords
    """
    if len(candidates) == 1:
        return candidates, "sole_unit"

    racial = [u for u in candidates if RACE_RE.search(u.text_on(page))]
    if racial:
        return racial, "race_terms"

    clue = _words(" ".join([cls.get("title") or "", cls.get("summary") or "", " ".join(cls.get("keywords") or [])]))
    scored = sorted(
        candidates,
        key=lambda u: (len(clue & _words(u.text_on(page))), len(u.text_on(page))),
        reverse=True,
    )
    return scored[:1], "title_match"


# ---------------------------------------------------------------------------
# Building law records
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# Where an act's title ends: the enacting clause, a preamble, or the first section/article.
ACT_TITLE_END_RE = re.compile(
    r"\s(?:Be\s*it\s*enacted|WHEREAS|Whereas|§\s*1\b|\$\s*1\b|SEC(?:TION)?\.?\s*1\b|ARTICLE\s+I\b)")
# A code section's catchline: "§ 4442. Levy and collection of tax. Said tax ..." / "...tax.- It shall"
CATCHLINE_END_RE = re.compile(r"\.\s*[-—]?\s+(?=[A-Z(\[])|\.\s*[-—]")
MAX_TITLE_CHARS = 300
MAX_CATCHLINE_CHARS = 160


def act_title(unit: Unit) -> str:
    """'AN ACT for additional improvements ... Colored Persons.' up to the enacting clause."""
    text = re.sub(r"\s+", " ", " ".join(l.text.strip() for l in unit.lines))
    m = re.search(r"AN\s*ACT", text)
    title = text[m.start():] if m else text
    end = ACT_TITLE_END_RE.search(title)
    return (title[:end.start()] if end else title).strip()


def section_title(unit: Unit) -> str:
    """'§ 4442. Levy and collection of tax.' — the number and its catchline."""
    text = re.sub(r"\s+", " ", " ".join(l.text.strip() for l in unit.lines[:6])).strip()
    number = SECTION_RE.match(text)
    start = number.end() if number else 0
    # Skip a compilation's cross-reference: "§ 155. (§ 4477 Ky. St.) Free Tuition..."
    ref = re.match(r"\s*\(\s*§[^)]*\)", text[start:])
    body_start = start + (ref.end() if ref else 0)
    end = CATCHLINE_END_RE.search(text, body_start + 3)
    if end and end.start() - body_start <= MAX_CATCHLINE_CHARS:
        return text[:end.start() + 1]
    return text


def key_provisions(unit: Unit, pages: List[int]) -> List[str]:
    """The sections of a law, on its flagged pages, that use racial language."""
    chunks, current = [], []
    for l in unit.lines:
        if SECTION_RE.match(l.text) and current:
            chunks.append(current)
            current = []
        current.append(l)
    if current:
        chunks.append(current)
    found = []
    for chunk in chunks:
        text = "\n".join(l.text for l in chunk)
        if any(l.page in pages for l in chunk) and RACE_RE.search(text):
            found.append(re.sub(r"\s+", " ", text).strip()[:1500])
    return found[:MAX_KEY_PROVISIONS]


def clean_text(unit: Unit) -> str:
    text = unit.text.replace("\x00", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def make_citation(volume: str, year, unit: Unit) -> str:
    pages = unit.pages
    span = f"p. {pages[0]}" if len(pages) == 1 else f"pp. {pages[0]}–{pages[-1]}"
    if unit.kind == "act" and unit.number:
        return f"{volume}, {year}, ch. {unit.number}, {span}"
    if unit.kind == "section" and unit.number:
        return f"{volume}, {year}, § {unit.number}, {span}"
    return f"{volume}, {year}, {span}"


def build_law(unit: Unit, flagged: List[Tuple[dict, str]], source: dict, filename: str) -> dict:
    """flagged: [(page_entry, attribution_method)] for pages credited to this unit."""
    pages = unit.pages
    volume = source.get("title") or Path(filename).stem
    year = source.get("year")
    stem = slugify(Path(filename).stem.replace("_results_classified", "").replace("_results", ""))
    ident = unit.number or f"p{pages[0]}"
    law_id = f"{stem}_{unit.kind}_{slugify(ident)}_p{pages[0]}"

    classes = [e["classification"] for e, _ in flagged]
    categories = Counter(c.get("category") for c in classes if c.get("category"))
    indicators = {c.get("racial_indicator") for c in classes}
    methods = {m for _, m in flagged}
    title = act_title(unit) if unit.kind == "act" else section_title(unit)

    needs_review = (
        any(c.get("needs_human_review") for c in classes)
        or "title_match" in methods
        or not unit.closed
        or len(pages) > LONG_LAW_PAGES
    )
    notes = [e.get("reviewer_note") for e, _ in flagged if e.get("reviewer_note")]

    return {
        "law_id": law_id,
        "source_filename": filename,
        "volume_title": volume,
        "year": year,
        "document_type": source.get("document_type"),
        "unit_type": unit.kind,
        "number": unit.number,
        "heading": unit.heading or None,
        "title": title,
        "citation": make_citation(volume, year, unit),
        "page_start": pages[0],
        "page_end": pages[-1],
        "page_count": len(pages),
        "jim_crow_pages": sorted(e["page_number"] for e, _ in flagged),
        "category": categories.most_common(1)[0][0] if categories else None,
        "all_categories": sorted(categories),
        "racial_indicator": "explicit" if "explicit" in indicators else ("implicit" if "implicit" in indicators else "none"),
        "confidence": max((c.get("confidence") or 0) for c in classes),
        "summary": classes[0].get("summary"),
        "key_provisions": key_provisions(unit, [e["page_number"] for e, _ in flagged]),
        "page_titles": list(dict.fromkeys(c.get("title") for c in classes if c.get("title"))),
        "attribution": sorted(methods),
        "boundary_found": unit.closed,
        "reviewed": all(e.get("reviewed", False) for e, _ in flagged),
        "reviewer_notes": notes,
        "needs_review": needs_review,
        "full_text": clean_text(unit),
    }


def extract_from_file(path: Path, llm_text: Optional[List[str]] = None) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    Returns (laws, page_links, excluded_pages) for one classified results file.

    If llm_text is given, the LLM's page titles, summaries and keywords are appended to
    it; they are well-spaced text used to learn the vocabulary for cleaning titles.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    source = data.get("source_document", {})
    entries = data.get("entries", [])
    if llm_text is not None:
        for e in entries:
            c = e["classification"]
            llm_text.extend([c.get("title") or "", c.get("summary") or "", " ".join(c.get("keywords") or [])])
    flagged_entries = [e for e in entries if e["classification"].get("is_jim_crow") == "yes"]
    if not flagged_entries:
        return [], [], []

    filename = source.get("filename") or path.name
    index_pages = {e["page_number"] for e in entries if is_index_page(e.get("ocr_text") or "")}
    mode = choose_mode(entries, source.get("document_type"))
    units, page_lines = segment(entries, mode, index_pages)

    units_by_page: Dict[int, List[Unit]] = defaultdict(list)
    for u in units:
        for p in u.pages:
            units_by_page[p].append(u)

    credited: Dict[int, List[Tuple[dict, str]]] = defaultdict(list)  # id(unit) → pages
    unit_by_id: Dict[int, Unit] = {}
    excluded, links = [], []

    for e in flagged_entries:
        page = e["page_number"]
        if page in index_pages:
            excluded.append({"source_filename": filename, "page_number": page,
                             "reason": "index or table of contents", "page_title": e["classification"].get("title")})
            continue
        candidates = units_by_page.get(page, [])
        if not candidates:
            # Page sits outside any detected law (front matter, missed heading): keep it as its own unit.
            lines = [Line(page, t) for t in page_lines.get(page, [])]
            candidates = [Unit("page", None, "", lines, closed=False)]
            units_by_page[page] = candidates
        chosen, method = attribute_page(page, e["classification"], candidates)
        for u in chosen:
            unit_by_id[id(u)] = u
            credited[id(u)].append((e, method))

    laws = []
    for uid, flagged in credited.items():
        law = build_law(unit_by_id[uid], flagged, source, filename)
        laws.append(law)
        for e, method in flagged:
            links.append({"source_filename": filename, "page_number": e["page_number"],
                          "law_id": law["law_id"], "attribution": method})
    laws.sort(key=lambda l: l["page_start"])
    return laws, links, excluded


def clean_titles(laws: List[dict], llm_text: List[str]) -> None:
    """Repair OCR word boundaries in titles; the original is kept as title_raw."""
    if wordninja is None:
        print("  (wordninja not installed: titles get punctuation fixes only. pip install wordninja)")
    cleaner = TitleCleaner(TitleCleaner.domain_words(llm_text))
    for law in laws:
        law["title_raw"] = law["title"]
        if law["unit_type"] == "page" and law["page_titles"]:
            # A loose page has no heading of its own; the LLM's title for it reads better
            # than its first OCR line (often a running head or a fragment).
            law["title"] = law["page_titles"][0]
        else:
            law["title"] = shorten(cleaner.clean(law["title"]), MAX_TITLE_CHARS)


def mark_duplicates(laws: List[dict]) -> None:
    """Flag laws whose text is identical to an earlier one (same volume scanned twice)."""
    seen: Dict[str, str] = {}
    for law in laws:
        norm = re.sub(r"[^a-z0-9]", "", law["full_text"].lower())
        digest = hashlib.sha1(norm.encode()).hexdigest()
        law["duplicate_of"] = seen.get(digest)
        seen.setdefault(digest, law["law_id"])


def dedupe_ids(laws: List[dict]) -> None:
    counts: Counter = Counter()
    for law in laws:
        counts[law["law_id"]] += 1
        if counts[law["law_id"]] > 1:
            law["law_id"] = f"{law['law_id']}_{counts[law['law_id']]}"


# ---------------------------------------------------------------------------
# Grouping editions: the same statute reprinted in several compilations
# ---------------------------------------------------------------------------

# Editions of Carroll's/Baldwin's Kentucky Statutes (1894–1926) share one section numbering.
KY_STATUTES_VOLUME_RE = re.compile(r"kentucky statute", re.IGNORECASE)
# School-law compilations cite it: "§ 155. (§ 4477 Ky. St.) Free Tuition..."
KY_STATUTES_REF_RE = re.compile(r"\(\s*§\s*(\d+[a-z]?(?:-\d+[a-z]?)?)\s*,?\s*Ky\s*\.?\s*St", re.IGNORECASE)
OWN_NUMBER_RE = re.compile(r"^\s*#*\s*(?:§+|\$|SEC\.?|SECTION)\s*[\w-]+\s*\.", re.IGNORECASE)
CROSS_REF_RE = re.compile(r"\(\s*§[^)]*\)")

OPENING_CHARS = 600     # compare statute openings; trailing case notes differ between editions
SHINGLE = 6
SAME_TEXT = 0.6         # opening similarity at or above which two sections are one statute
POSSIBLE_MATCH = 0.4    # between this and SAME_TEXT: listed for a human to decide
COMMON_SHINGLE = 80     # shingles shared by more laws than this are boilerplate


def ky_statutes_number(law: dict) -> Optional[str]:
    """The Kentucky Statutes § number a section is (or cites), if it has one."""
    if law["unit_type"] != "section" or not law["number"]:
        return None
    if KY_STATUTES_VOLUME_RE.search(law["volume_title"] or ""):
        number = law["number"].lower()
    else:
        m = KY_STATUTES_REF_RE.search(law["full_text"][:150])
        if not m:
            return None
        number = m.group(1).lower()
    # "§ 1." of an act or constitution printed inside a code identifies nothing.
    return number if re.match(r"\d{3}", number) else None


def opening_shingles(law: dict) -> set:
    text = CROSS_REF_RE.sub("", OWN_NUMBER_RE.sub("", law["full_text"]))
    s = re.sub(r"[^a-z]", "", text.lower())[:OPENING_CHARS]  # OCR drops spaces, so ignore them
    return {s[i:i + SHINGLE] for i in range(len(s) - SHINGLE + 1)}


def group_editions(laws: List[dict]) -> List[dict]:
    """
    Assign every law a statute_id shared by all printings of the same statute.

    Two printings are linked when
      * both carry the same Kentucky Statutes § number (an edition's own number,
        or a school-law compilation's "(§ 4477 Ky. St.)" reference);
      * two code sections or pages open with near-identical text (catches renumbering
        and OCR-misread numbers such as "45214" for "4521a"); or
      * two acts are the same chapter of the same year's session laws.

    Acts are otherwise never merged: each act is a separate enactment, even when its
    wording repeats an earlier one. Similar text never merges two laws that would put
    two printings from the same volume into one statute: the charters for each class
    of city carry near-identical "separate schools" sections that are distinct laws,
    and a single volume prints them side by side. Those pairs, and pairs with only
    moderately similar openings, are returned as possible matches for review.
    Exact duplicates follow their original.
    """
    unique = [l for l in laws if not l.get("duplicate_of")]
    parent = list(range(len(unique)))
    volumes = [{l["source_filename"]} for l in unique]  # per root: volumes in the group
    how: Dict[int, set] = defaultdict(set)

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b, reason, guarded=False):
        ra, rb = find(a), find(b)
        if ra != rb:
            if guarded and volumes[ra] & volumes[rb]:
                return False
            parent[rb] = ra
            volumes[ra] |= volumes[rb]
        how[a].add(reason)
        how[b].add(reason)
        return True

    by_key: Dict[Tuple, List[int]] = defaultdict(list)
    for i, law in enumerate(unique):
        number = ky_statutes_number(law)
        if number:
            by_key[("ky_st", number)].append(i)
        if law["unit_type"] == "act" and law["number"]:
            by_key[("act", law["year"], law["number"].upper())].append(i)
    for key, members in by_key.items():
        for other in members[1:]:
            union(members[0], other, "same Ky. St. section" if key[0] == "ky_st" else "same act")

    # Text similarity between code sections and pages, via an inverted shingle index.
    textual = [i for i, l in enumerate(unique) if l["unit_type"] in ("section", "page")]
    shingles = {i: opening_shingles(unique[i]) for i in textual}
    index: Dict[str, List[int]] = defaultdict(list)
    for i in textual:
        for s in shingles[i]:
            index[s].append(i)
    shared: Counter = Counter()
    for members in index.values():
        if len(members) > COMMON_SHINGLE:
            continue
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                shared[(members[x], members[y])] += 1

    pairs = sorted(((n / len(shingles[a] | shingles[b]), a, b) for (a, b), n in shared.items()), reverse=True)
    possible = []
    for sim, a, b in pairs:
        if sim < POSSIBLE_MATCH:
            break
        if sim >= SAME_TEXT and union(a, b, "same text", guarded=True):
            continue
        if unique[a]["source_filename"] != unique[b]["source_filename"]:
            possible.append((sim, a, b))

    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(unique)):
        groups[find(i)].append(i)

    statute_of: Dict[str, str] = {}
    for members in groups.values():
        members.sort(key=lambda i: (unique[i]["year"] or 9999, unique[i]["law_id"]))
        first = unique[members[0]]
        numbers = sorted({ky_statutes_number(unique[i]) for i in members} - {None})
        statute_id = f"statute_{first['law_id']}"
        statute_citation = (f"Ky. Stat. § {numbers[0]}" if len(numbers) == 1
                            else f"Ky. Stat. §§ {', '.join(numbers)}" if numbers else first["citation"])
        years = sorted({unique[i]["year"] for i in members if unique[i]["year"]})
        for i in members:
            law = unique[i]
            law["statute_id"] = statute_id
            law["statute_citation"] = statute_citation
            law["statute_printings"] = len(members)
            law["statute_years"] = f"{years[0]}–{years[-1]}" if len(years) > 1 else (str(years[0]) if years else None)
            law["grouped_by"] = sorted(how[i])
            statute_of[law["law_id"]] = statute_id
    for law in laws:
        if law.get("duplicate_of"):
            original = next(l for l in unique if l["law_id"] == law["duplicate_of"])
            for k in ("statute_id", "statute_citation", "statute_printings", "statute_years", "grouped_by"):
                law[k] = original[k]

    return [
        {"similarity": round(sim, 2),
         "law_id_a": unique[a]["law_id"], "citation_a": unique[a]["citation"], "statute_id_a": unique[a]["statute_id"],
         "law_id_b": unique[b]["law_id"], "citation_b": unique[b]["citation"], "statute_id_b": unique[b]["statute_id"]}
        for sim, a, b in sorted(possible, reverse=True)
        if unique[a]["statute_id"] != unique[b]["statute_id"]
    ]


def summarize_statutes(laws: List[dict]) -> List[dict]:
    """One row per statute: its printings, years, and whether any printing is settled."""
    by_statute: Dict[str, List[dict]] = defaultdict(list)
    for law in laws:
        if not law.get("duplicate_of"):
            by_statute[law["statute_id"]].append(law)
    rows = []
    for statute_id, members in by_statute.items():
        members.sort(key=lambda l: (l["year"] or 9999, l["law_id"]))
        first = members[0]
        categories = Counter(l["category"] for l in members if l["category"])
        rows.append({
            "statute_id": statute_id,
            "statute_citation": first["statute_citation"],
            "title": first["title"],
            "unit_type": first["unit_type"],
            "printings": len(members),
            "first_year": first["year"],
            "last_year": members[-1]["year"],
            "category": categories.most_common(1)[0][0] if categories else None,
            "racial_indicator": "explicit" if any(l["racial_indicator"] == "explicit" for l in members) else first["racial_indicator"],
            # Settled if any printing was attributed without doubt.
            "needs_review": all(l["needs_review"] for l in members),
            "grouped_by": sorted({g for l in members for g in l["grouped_by"]}),
            "citations": [l["citation"] for l in members],
            "law_ids": [l["law_id"] for l in members],
            "summary": first["summary"],
        })
    rows.sort(key=lambda r: (r["first_year"] or 9999, r["statute_id"]))
    return rows


# ---------------------------------------------------------------------------
# Optional: re-classify each law's full text with the LLM
# ---------------------------------------------------------------------------

MAX_VERIFY_CHARS = 12000


def verify_with_llm(laws: List[dict]) -> None:
    sys.path.insert(0, str(Path(__file__).parent))
    from models import StatuteClassification
    from processor import build_program

    program = build_program()
    for i, law in enumerate(laws):
        if law.get("duplicate_of"):
            continue
        print(f"  [{i + 1}/{len(laws)}] {law['law_id']}")
        try:
            result = program(
                source_filename=law["source_filename"],
                page_number=law["page_start"],
                year=law["year"] or "unknown",
                statute_text=law["full_text"][:MAX_VERIFY_CHARS],
                schema=StatuteClassification.model_json_schema(),
            )
        except Exception as exc:  # keep going; one bad response shouldn't sink the run
            print(f"    ! {exc}")
            law["law_level_classification"] = None
            continue
        law["law_level_classification"] = result.model_dump(exclude={"document_type"})
        if result.is_jim_crow != "yes":
            law["needs_review"] = True


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "law_id", "citation", "title", "title_raw", "volume_title", "year", "document_type", "unit_type",
    "number", "heading", "page_start", "page_end", "page_count", "jim_crow_pages", "category",
    "racial_indicator", "confidence", "summary", "attribution", "boundary_found", "reviewed",
    "reviewer_notes", "needs_review", "duplicate_of", "statute_id", "statute_citation",
    "statute_printings", "statute_years", "grouped_by", "law_level_is_jim_crow", "source_filename",
    "full_text",
]
STATUTE_FIELDS = [
    "statute_id", "statute_citation", "title", "unit_type", "printings", "first_year", "last_year",
    "category", "racial_indicator", "needs_review", "grouped_by", "citations", "law_ids", "summary",
]


def write_csv(path: Path, rows: List[dict], fields: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: "; ".join(map(str, v)) if isinstance(v, list) else v for k, v in r.items()})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--verify", action="store_true", help="re-classify each law's full text with the LLM")
    args = parser.parse_args()

    files = sorted(args.results_dir.glob("*_classified.json"))
    print(f"Reading {len(files)} classified files from {args.results_dir}")

    laws, links, excluded, llm_text = [], [], [], []
    for path in files:
        l, k, x = extract_from_file(path, llm_text)
        laws += l
        links += k
        excluded += x
    flagged_pages = len({(r["source_filename"], r["page_number"]) for r in links}) + len(excluded)

    dedupe_ids(laws)
    mark_duplicates(laws)
    clean_titles(laws, llm_text)
    possible_matches = group_editions(laws)
    if args.verify:
        print(f"Verifying {len(laws)} laws with the LLM...")
        verify_with_llm(laws)
    for law in laws:
        c = law.get("law_level_classification")
        law["law_level_is_jim_crow"] = c["is_jim_crow"] if c else None

    args.out_dir.mkdir(parents=True, exist_ok=True)
    unique = [l for l in laws if not l["duplicate_of"]]
    statutes = summarize_statutes(laws)
    stats = {
        "flagged_pages": flagged_pages,
        "excluded_pages": len(excluded),
        "laws": len(laws),
        "unique_laws": len(unique),
        "by_unit_type": dict(Counter(l["unit_type"] for l in unique)),
        "by_attribution": dict(Counter(m for l in unique for m in l["attribution"])),
        "needs_review": sum(l["needs_review"] for l in unique),
        "distinct_statutes": len(statutes),
        "distinct_statutes_by_unit_type": dict(Counter(s["unit_type"] for s in statutes)),
        "distinct_statutes_needing_review": sum(s["needs_review"] for s in statutes),
        "possible_matches": len(possible_matches),
    }
    with open(args.out_dir / "laws.json", "w", encoding="utf-8") as f:
        json.dump({"statistics": stats, "laws": laws, "statutes": statutes, "excluded_pages": excluded,
                   "possible_matches": possible_matches}, f, indent=2, ensure_ascii=False)
    write_csv(args.out_dir / "laws.csv", laws, CSV_FIELDS)
    write_csv(args.out_dir / "page_to_law.csv", links, ["source_filename", "page_number", "law_id", "attribution"])
    write_csv(args.out_dir / "excluded_pages.csv", excluded, ["source_filename", "page_number", "reason", "page_title"])
    write_csv(args.out_dir / "statutes.csv", statutes, STATUTE_FIELDS)
    write_csv(args.out_dir / "possible_matches.csv", possible_matches, list(possible_matches[0]) if possible_matches else [])

    print(f"\nFlagged pages       : {stats['flagged_pages']}")
    print(f"  index/contents    : {stats['excluded_pages']} (excluded)")
    print(f"Laws extracted      : {stats['laws']} ({stats['unique_laws']} unique)")
    for k, v in stats["by_unit_type"].items():
        print(f"  {k:<18}: {v}")
    print(f"Needs review        : {stats['needs_review']}")
    print(f"Distinct statutes   : {stats['distinct_statutes']} ({stats['distinct_statutes_needing_review']} need review)")
    print(f"Possible matches    : {stats['possible_matches']} pairs left for a human to decide")
    print(f"Output              : {args.out_dir}/")


if __name__ == "__main__":
    main()
