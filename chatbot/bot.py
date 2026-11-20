"""
bot.py — Answer questions about Kentucky's Jim Crow laws from the archive.

Each question is answered in three steps:
  1. find the statutes that best match it (archive.Archive.search), honouring any
     year range in the question ("in the 1890s", "before 1904");
  2. compute exact counts for that range, so "how many" questions get real numbers;
  3. ask the project's configured LLM (LLM_PROVIDER / LLM_MODEL in .env, the same
     settings doc_processing uses) to answer from those sources only, citing them.

Without an LLM configured the bot still returns the matching sources.
"""

import json
import re
import sys
from pathlib import Path
from typing import List, Optional

from .archive import Archive, parse_years

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "doc_processing"))

TOP_K = 8
EXCERPT_CHARS = 1200
HISTORY_MESSAGES = 6     # earlier turns sent to the model, for follow-up questions
# Popular names for laws, expanded to the words the statutes themselves use.
NAMED_LAWS = {
    "day law": "prohibit white and colored persons attending same school",
    "separate coach": "separate coaches white colored passengers railroad",
}
FOLLOW_UP_WORDS = 8      # a question this short probably leans on the previous one

SYSTEM_PROMPT = """\
You are the research assistant for a public digital archive of Kentucky's Jim Crow
laws (1865–1966), built from scanned and OCR'd statute books. You answer questions
for students, researchers and the public.

Rules:
- Answer ONLY from the SOURCES and ARCHIVE FACTS given with each question. If they do
  not answer it, say so plainly and suggest how to rephrase or what to search for.
  Do not fill gaps from general knowledge; you may add widely known historical context
  (for example that the 1904 school-segregation act is known as the Day Law) only when
  you label it as context and a source supports the substance.
- Cite sources with bracketed numbers, e.g. [2], after the claims they support.
- Each source gives the law's own text and, separately, a page summary written by an
  AI model. A page summary can describe a neighbouring law printed on the same page;
  when it disagrees with the law's own text, trust the text and do not repeat the
  summary as fact.
- For counts, use ARCHIVE FACTS exactly, and say what was counted (distinct statutes
  in this archive, by the years of the volumes they were printed in). The archive
  covers the volumes that were digitized, not every law Kentucky passed.
- Sources marked "needs human review" are uncertain findings; say so when you rely on one.
- Years are the years of the volumes the laws were printed in, not always enactment dates.
- Quote historical wording accurately, including offensive racial terms, inside
  quotation marks; in your own words use respectful modern language.
- Be concise: a short direct answer, then supporting detail. No headings for short answers.
"""


class ArchiveChat:
    def __init__(self, archive: Optional[Archive] = None, llm=None, cleaner=None):
        self.archive = archive or Archive()
        self._llm = llm
        self._llm_error: Optional[str] = None
        self._cleaner = cleaner

    # -- dependencies ------------------------------------------------------------

    @property
    def llm(self):
        if self._llm is None and self._llm_error is None:
            try:
                from processor import build_llm
                self._llm = build_llm()
            except Exception as exc:  # missing key, package or provider: retrieval-only mode
                self._llm_error = f"{type(exc).__name__}: {exc}"
        return self._llm

    @property
    def cleaner(self):
        """Repairs OCR word boundaries in the excerpts the model reads, if wordninja is installed."""
        if self._cleaner is None:
            try:
                from text_cleanup import TitleCleaner, wordninja
                if wordninja is None:
                    raise ImportError("wordninja")
                words = TitleCleaner.domain_words(
                    s.title + " " + s.all_summaries for s in self.archive.statutes)
                self._cleaner = TitleCleaner(words)
            except ImportError:
                self._cleaner = False
        return self._cleaner or None

    # -- answering ---------------------------------------------------------------

    @staticmethod
    def previous_question(question: str, history: List[dict]) -> Optional[str]:
        """The question a short follow-up ("tell me more", "what about the 1920s?") leans on."""
        previous = [m["content"] for m in history if m.get("role") == "user"]
        if previous and len(question.split()) <= FOLLOW_UP_WORDS:
            return previous[-1]
        return None

    def retrieval_query(self, question: str, history: List[dict]) -> str:
        previous = self.previous_question(question, history)
        query = f"{previous} {question}" if previous else question
        for name, wording in NAMED_LAWS.items():
            if name in query.lower():
                query += " " + wording
        return query

    def years_for(self, question: str, history: List[dict]):
        """A follow-up keeps the previous question's years unless it names its own."""
        years = parse_years(question)
        previous = self.previous_question(question, history)
        if years == (None, None) and previous:
            years = parse_years(previous)
        return years

    def excerpt(self, statute) -> str:
        text = statute.key_provisions[0] if statute.key_provisions else statute.text
        text = re.sub(r"\s+", " ", text).strip()[:EXCERPT_CHARS]
        if self.cleaner:
            text = self.cleaner.clean(text)
        return text

    def build_context(self, question: str, results, facts: dict) -> str:
        blocks = []
        for n, (s, _) in enumerate(results, 1):
            status = "needs human review" if s.needs_review else "reviewed finding"
            printed = "; ".join(s.citations[:6]) + (f"; and {len(s.citations) - 6} more" if len(s.citations) > 6 else "")
            blocks.append(
                f"[{n}] {s.citation}\n"
                f"Title: {s.title}\n"
                f"Type: {s.unit_type} | Category: {s.category or 'uncategorized'} | "
                f"Printed: {', '.join(map(str, s.years))} ({s.printings} printing{'s' if s.printings != 1 else ''}) | {status}\n"
                f"Printed in: {printed}\n"
                f"Law's own text (OCR excerpt): {self.excerpt(s)}\n"
                f"Page summary (AI-written, may describe a neighbouring law): {s.summary}"
            )
        sources = "\n\n".join(blocks) if blocks else "(no matching statutes found)"
        return (f"ARCHIVE FACTS (exact counts):\n{json.dumps(facts, indent=1)}\n\n"
                f"SOURCES:\n{sources}\n\nQUESTION: {question}")

    def answer(self, question: str, history: Optional[List[dict]] = None) -> dict:
        question = (question or "").strip()
        history = [m for m in (history or []) if m.get("role") in ("user", "assistant") and m.get("content")]
        if not question:
            return {"answer": "Please ask a question about Kentucky's Jim Crow laws.", "sources": [], "mode": "empty"}

        year_from, year_to = self.years_for(question, history)
        results = self.archive.search(self.retrieval_query(question, history), year_from, year_to, limit=TOP_K)
        facts = self.archive.facts(question, year_from, year_to)
        sources = [s.to_source() for s, _ in results]
        for n, src in enumerate(sources, 1):
            src["number"] = n
            if self.cleaner:
                src["snippet"] = self.cleaner.clean(src["snippet"])

        llm = self.llm
        if llm is None:
            return {
                "answer": ("The AI model isn't configured, so here are the statutes that best match "
                           "your question. Set LLM_PROVIDER and its API key in .env to get written answers."),
                "sources": sources, "facts": facts, "mode": "retrieval_only",
                "llm_error": self._llm_error,
            }

        from llama_index.core.llms import ChatMessage
        messages = [ChatMessage(role="system", content=SYSTEM_PROMPT)]
        messages += [ChatMessage(role=m["role"], content=m["content"]) for m in history[-HISTORY_MESSAGES:]]
        messages.append(ChatMessage(role="user", content=self.build_context(question, results, facts)))
        response = llm.chat(messages)
        text = (response.message.content or "").strip()
        cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 0 < int(n) <= len(sources)})
        for src in sources:
            src["cited"] = src["number"] in cited
        return {"answer": text, "sources": sources, "facts": facts, "mode": "llm"}
