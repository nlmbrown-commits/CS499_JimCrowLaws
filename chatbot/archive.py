"""
archive.py — Search the extracted Jim Crow laws for the chatbot.

Loads law_results/laws.json (written by doc_processing/extract_laws.py) and
indexes its distinct statutes, each with every printing of the same law grouped.
A statute is searchable by its cleaned title, the LLM's summaries, the
provisions that use racial language, its citations, and the opening of its text.

Pure Python (BM25), so it runs anywhere the API server does.
"""

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_LAWS_PATH = Path(__file__).parent.parent / "law_results" / "laws.json"

TOKEN_RE = re.compile(r"[a-z]+|\d{4}")
STOPWORDS = set("""
a about above after again all also am an and any are as at be because been before being
between both but by can could did do does doing during each few for from further had has
have having he her here hers him his how i if in into is it its itself just law laws me
more most my no nor not now of off on once only or other our out over own same she should
so some such than that the their them then there these they this those through to too
under until up very was we were what when where which while who whom why will with would
you your kentucky ky statute statutes act acts section sections tell show find give list
please many much did does
""".split())
# Words a question uses for what the statutes call something else.
SYNONYMS = {
    "black": ["colored", "negro"],
    "african": ["colored", "negro"],
    "segregation": ["separate"],
    "segregate": ["separate"],
    "segregated": ["separate"],
    "interracial": ["white", "negro", "mulatto"],
    "intermarriage": ["marriage", "white", "negro"],
    "miscegenation": ["marriage", "white", "negro"],
    "train": ["railroad", "coach"],
    "trains": ["railroad", "coach"],
    "railway": ["railroad", "coach"],
    "transportation": ["railroad", "coach"],
    "school": ["schools"],
    "education": ["school", "schools"],
    "prison": ["penitentiary", "jail"],
    "voting": ["election", "vote", "registration"],
    "vote": ["election", "registration"],
    "poll": ["capitation"],
}
SYNONYM_WEIGHT = 0.4    # a synonym counts for less than a word the question actually uses

TEXT_CHARS = 1500       # opening text indexed per statute; long acts would otherwise dominate
SNIPPET_CHARS = 700
MAIN_CATEGORIES = {"education", "voting", "labor", "incarceration", "public_accommodations",
                   "marriage", "housing", "taxation", "slavery", "law_enforcement"}
BM25_K1, BM25_B = 1.5, 0.75
MIN_COVERAGE_FACTOR = 0.2   # score multiplier for a statute matching none of the asked words
# The page summary counts for no more than the text: when a page held two laws,
# both inherit its summary, which describes only one of them.
FIELD_WEIGHTS = {"title": 3, "summary": 1, "provisions": 2, "category": 2, "text": 1}


def _stem(word: str) -> str:
    """Light suffix stripping so 'schools'/'school', 'married'/'marriage' meet."""
    for suffix, keep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + keep
    return word


def tokenize(text: str) -> List[str]:
    return [_stem(t) for t in TOKEN_RE.findall((text or "").lower()) if t not in STOPWORDS]


def query_terms(question: str) -> Dict[str, float]:
    """Search terms with weights: the question's own words 1.0, their synonyms less."""
    words = [w for w in TOKEN_RE.findall(question.lower()) if w not in STOPWORDS and not w.isdigit()]
    terms: Dict[str, float] = {}
    for w in words:
        terms[_stem(w)] = 1.0
    for w in words:
        for syn in SYNONYMS.get(w, []):
            terms.setdefault(_stem(syn), SYNONYM_WEIGHT)
    return terms


def normalize_category(category: Optional[str]) -> str:
    """'Public Facilities' and 'public facilities' are one category."""
    return re.sub(r"[\s/]+", "_", (category or "uncategorized").strip().lower())


# ---------------------------------------------------------------------------
# Year phrases: "in 1904", "in the 1890s", "before 1900", "between 1890 and 1910"
# ---------------------------------------------------------------------------

def parse_years(question: str) -> Tuple[Optional[int], Optional[int]]:
    q = question.lower()
    m = re.search(r"(?:between|from)\s+(1[89]\d\d)s?\s+(?:and|to|-|–|through)\s+(1[89]\d\d)(s?)", q)
    if m:
        end = int(m.group(2)) + (9 if m.group(3) else 0)
        return int(m.group(1)), end
    m = re.search(r"(1[89]\d\d)\s*(?:-|–|to)\s*(1[89]\d\d)", q)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(?:before|prior to|until|up to)\s+(?:the\s+)?(1[89]\d\d)(s?)", q)
    if m:
        return None, int(m.group(1)) - 1
    m = re.search(r"\b(?:after|since|following)\s+(?:the\s+)?(1[89]\d\d)(s?)", q)
    if m:
        start = int(m.group(1)) + (10 if m.group(2) else 1)
        return start, None
    m = re.search(r"\b(1[89]\d)0s\b", q)
    if m:
        decade = int(m.group(1)) * 10
        return decade, decade + 9
    m = re.search(r"\b(1[89]\d\d)\b", q)
    if m:
        return int(m.group(1)), int(m.group(1))
    return None, None


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------

@dataclass
class Statute:
    statute_id: str
    citation: str
    title: str
    unit_type: str
    category: Optional[str]
    racial_indicator: Optional[str]
    first_year: Optional[int]
    last_year: Optional[int]
    printings: int
    needs_review: bool
    summary: str
    citations: List[str]
    key_provisions: List[str]
    text: str
    years: List[int] = field(default_factory=list)
    all_summaries: str = ""   # the LLM's summaries of every printing, for search

    def snippet(self) -> str:
        source = self.key_provisions[0] if self.key_provisions else self.text
        source = re.sub(r"\s+", " ", source).strip()
        return source[:SNIPPET_CHARS] + ("…" if len(source) > SNIPPET_CHARS else "")

    def in_years(self, year_from: Optional[int], year_to: Optional[int]) -> bool:
        if year_from is None and year_to is None:
            return True
        lo, hi = year_from or 0, year_to or 9999
        return any(lo <= y <= hi for y in self.years)

    def to_source(self) -> dict:
        return {
            "statute_id": self.statute_id,
            "citation": self.citation,
            "title": self.title,
            "unit_type": self.unit_type,
            "category": self.category,
            "years": (f"{self.first_year}" if self.first_year == self.last_year
                      else f"{self.first_year}–{self.last_year}"),
            "printings": self.printings,
            "needs_review": self.needs_review,
            "summary": self.summary,
            "snippet": self.snippet(),
            "printed_in": self.citations,
        }


class Archive:
    def __init__(self, laws_path: Path = DEFAULT_LAWS_PATH):
        with open(laws_path, encoding="utf-8") as f:
            data = json.load(f)
        laws_by_id = {l["law_id"]: l for l in data["laws"]}
        self.statutes: List[Statute] = []
        for s in data["statutes"]:
            printings = [laws_by_id[i] for i in s["law_ids"] if i in laws_by_id]
            provisions = list(dict.fromkeys(p for l in printings for p in l.get("key_provisions") or []))
            summaries = list(dict.fromkeys(l["summary"] for l in printings if l.get("summary")))
            first = printings[0] if printings else {}
            self.statutes.append(Statute(
                statute_id=s["statute_id"],
                citation=s["statute_citation"],
                title=s["title"],
                unit_type=s["unit_type"],
                category=s.get("category"),
                racial_indicator=s.get("racial_indicator"),
                first_year=s.get("first_year"),
                last_year=s.get("last_year"),
                printings=s["printings"],
                needs_review=s["needs_review"],
                summary=s.get("summary") or "",
                citations=s.get("citations") or [],
                key_provisions=provisions,
                text=first.get("full_text", ""),
                years=sorted({l["year"] for l in printings if l.get("year")}),
                all_summaries=" ".join(summaries),
            ))
        self.statistics = data.get("statistics", {})
        self._build_index()

    # -- index ---------------------------------------------------------------

    def _fields(self, s: Statute) -> Dict[str, str]:
        return {
            "title": s.title,
            "summary": s.all_summaries or s.summary,
            "provisions": " ".join(s.key_provisions)[:TEXT_CHARS],
            "category": (s.category or "").replace("_", " "),
            "text": s.text[:TEXT_CHARS],
        }

    def _build_index(self) -> None:
        self.doc_terms: List[Counter] = []
        for s in self.statutes:
            terms: Counter = Counter()
            for name, text in self._fields(s).items():
                for t in tokenize(text):
                    terms[t] += FIELD_WEIGHTS[name]
            self.doc_terms.append(terms)
        self.doc_len = [sum(t.values()) for t in self.doc_terms]
        self.avg_len = sum(self.doc_len) / max(len(self.doc_len), 1)
        df: Counter = Counter()
        for terms in self.doc_terms:
            df.update(terms.keys())
        n = len(self.doc_terms)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def _score(self, i: int, terms: Dict[str, float]) -> float:
        doc, length, score = self.doc_terms[i], self.doc_len[i], 0.0
        for t, weight in terms.items():
            tf = doc.get(t)
            if not tf:
                continue
            norm = tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * length / self.avg_len))
            score += weight * self.idf.get(t, 0) * norm
        # Scale by how much of the question itself the statute covers, so a statute
        # that matches only synonyms ("negro" for "interracial") ranks below one that
        # matches what was asked. Words the archive never uses don't count against it.
        asked = [t for t, w in terms.items() if w == 1.0 and t in self.idf]
        if asked:
            covered = sum(self.idf[t] for t in asked if t in doc) / sum(self.idf[t] for t in asked)
            score *= MIN_COVERAGE_FACTOR + (1 - MIN_COVERAGE_FACTOR) * covered
        return score

    # -- queries -------------------------------------------------------------

    def search(self, query: str, year_from: Optional[int] = None, year_to: Optional[int] = None,
               limit: int = 8) -> List[Tuple[Statute, float]]:
        terms = query_terms(query)
        scored = []
        for i, s in enumerate(self.statutes):
            if not s.in_years(year_from, year_to):
                continue
            score = self._score(i, terms) if terms else 0.0
            if score > 0 or not terms:
                # Settled findings first among equals
                scored.append((s, score - (0.01 if s.needs_review else 0)))
        scored.sort(key=lambda x: (-x[1], x[0].first_year or 9999))
        return scored[:limit]

    @staticmethod
    def _category_counts(statutes: List[Statute]) -> Dict[str, int]:
        """Counts for the main categories; the long tail of one-off labels is pooled."""
        counts = Counter(normalize_category(s.category) for s in statutes)
        main = {c: n for c, n in counts.most_common() if c in MAIN_CATEGORIES}
        rest = sum(n for c, n in counts.items() if c not in MAIN_CATEGORIES)
        if rest:
            main["other_or_uncategorized"] = rest
        return main

    def facts(self, question: str, year_from: Optional[int] = None, year_to: Optional[int] = None) -> dict:
        """Exact counts the model can quote instead of estimating."""
        in_range_idx = [i for i, s in enumerate(self.statutes) if s.in_years(year_from, year_to)]
        in_range = [self.statutes[i] for i in in_range_idx]
        words = {w for w in TOKEN_RE.findall(question.lower())
                 if w not in STOPWORDS and not w.isdigit() and len(w) > 2}
        mentioning = {}
        for w in sorted(words):
            alternatives = {_stem(w)} | {_stem(x) for x in SYNONYMS.get(w, [])}
            mentioning[w] = sum(1 for i in in_range_idx if alternatives & self.doc_terms[i].keys())
        decades = Counter(f"{(s.first_year // 10) * 10}s" for s in in_range if s.first_year)
        return {
            "dataset": {
                "flagged_pages": self.statistics.get("flagged_pages"),
                "distinct_statutes": len(self.statutes),
                "unique_printings": self.statistics.get("unique_laws"),
                "volumes_years": f"{min(s.first_year for s in self.statutes if s.first_year)}–"
                                 f"{max(s.last_year for s in self.statutes if s.last_year)}",
            },
            "year_filter": {"from": year_from, "to": year_to},
            "statutes_in_year_range": len(in_range),
            "settled_in_year_range": sum(not s.needs_review for s in in_range),
            "by_category": self._category_counts(in_range),
            "by_type": dict(Counter(s.unit_type for s in in_range)),
            "by_decade_first_printed": dict(sorted(decades.items())),
            "statutes_mentioning_each_question_word": mentioning,
        }
