"""
text_cleanup.py — Make OCR'd statute titles readable.

OCR of the older volumes drops spaces ("Be itenactedby theGeneral Assembly"),
keeps line-break hyphens ("Com- monwealth") and spaces punctuation oddly
("Kentucky :", "$ 4534 ."). TitleCleaner repairs those while leaving words it
cannot confidently split untouched.

Glued words are split with wordninja's English word-frequency model, extended
with a domain vocabulary (place names, legal terms) gathered from the LLM's
page titles and summaries, which are well spaced.

    pip install wordninja
"""

import gzip
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

try:
    import wordninja
except ImportError:  # segmentation is skipped; the other repairs still run
    wordninja = None

WORD_RE = re.compile(r"[A-Za-z]+")
LETTERS_RE = re.compile(r"[A-Za-z]+(?:'[a-z]+)?")
# "Com- monwealth" / "commis-\nsioners": a word broken at a line end
BROKEN_WORD_RE = re.compile(r"\b([A-Za-z]+)-\s+([a-z][A-Za-z]*|(?<=[A-Z]-\s)[A-Z]{2,}(?![a-z]))")
# Running heads and page numbers that leak into a title: "# GRADED SCHOOLS—TREASURER. 71"
LEAKED_HEADER_RE = re.compile(r"\s*#+\s*[A-Z][A-Z .,'—-]{5,}\.?\s*\d*\s*")
SINGLE_LETTER_WORDS = {"a", "i"}
MIN_GLUED_LENGTH = 6      # shorter unknown tokens are names or abbreviations; leave them
MIN_DOMAIN_COUNT = 3      # a word must recur in the LLM text to join the vocabulary
PROMOTED_WORDS = 300      # this many of the commonest statute words outrank general English


class TitleCleaner:
    def __init__(self, domain_words: Optional[Counter] = None):
        self.vocab = set()
        self.promoted = set()
        self.model = None
        if wordninja is None:
            return
        path = Path(wordninja.__file__).parent / "wordninja" / "wordninja_words.txt.gz"
        with gzip.open(path, "rt", encoding="utf-8") as f:
            words = f.read().split()
        known = set(words)
        base = wordninja.LanguageModel(str(path))
        extra = [w for w, n in self._lowercase(domain_words).most_common()
                 if n >= MIN_DOMAIN_COUNT and w not in known and len(w) > 2
                 and (self._mostly_capitalised(w, domain_words) or not self._splits_cleanly(base, w, known))]
        # The commonest words of the statutes ("shall", "said", "county") go first so
        # "purposeshallbe" splits as "purpose shall be", not "purposes hall be".
        lower = self._lowercase(domain_words)
        promoted = [w for w, _ in lower.most_common(PROMOTED_WORDS) if w in known]
        promoted_set = set(promoted)
        self.promoted = promoted_set
        words = [w for w in words if w not in promoted_set]
        # Other domain words go in at a moderate rank: common enough to win a split,
        # not so common that they outrank everyday English.
        rank = min(len(words), 20000)
        ordered = promoted + words[:rank] + extra + words[rank:]
        self.vocab = set(ordered)
        model_path = Path(__file__).parent / ".title_words.txt.gz"
        with gzip.open(model_path, "wt", encoding="utf-8") as f:
            f.write("\n".join(ordered))
        self.model = wordninja.LanguageModel(str(model_path))
        model_path.unlink()

    @staticmethod
    def domain_words(texts: Iterable[str]) -> Counter:
        """Word counts (case kept) from well-spaced text such as the LLM's titles and summaries."""
        counts: Counter = Counter()
        for text in texts:
            counts.update(WORD_RE.findall(text or ""))
        return counts

    @staticmethod
    def _lowercase(counts: Optional[Counter]) -> Counter:
        lower: Counter = Counter()
        for w, n in (counts or Counter()).items():
            lower[w.lower()] += n
        return lower

    @staticmethod
    def _mostly_capitalised(word: str, counts: Counter) -> bool:
        """Proper names (Garrard, Cloverport) are written capitalised."""
        capital = counts.get(word.capitalize(), 0) + counts.get(word.upper(), 0)
        return capital > counts.get(word, 0)

    @staticmethod
    def _splits_cleanly(model, word: str, known: set) -> bool:
        """True for glued OCR that leaked into the LLM text ("shallbe" → "shall be")."""
        pieces = model.split(word)
        return len(pieces) > 1 and all(p in known and (len(p) > 1 or p in SINGLE_LETTER_WORDS) for p in pieces)

    def known(self, word: str) -> bool:
        return word.lower() in self.vocab

    def _split_token(self, token: str) -> str:
        """Split one run of letters if it is glued words; otherwise return it unchanged."""
        if self.model is None or len(token) < 4:
            return token
        if self.known(token):
            # "ofthe", "forthe" are in the general word list but are two statute words
            pieces = self.model.split(token)
            if (len(pieces) > 1 and token.lower() not in self.promoted
                    and all(p.lower() in self.promoted for p in pieces)):
                return " ".join(pieces)
            return token
        if len(token) < MIN_GLUED_LENGTH:
            return token
        pieces = self.model.split(token)
        if len(pieces) < 2 or "".join(pieces) != token:
            return token
        if any(not self.known(p) or (len(p) == 1 and p.lower() not in SINGLE_LETTER_WORDS) for p in pieces):
            return token  # not confident: a proper name or OCR garbage, keep as is
        return " ".join(pieces)

    def _join_broken_words(self, text: str) -> str:
        def join(m):
            head, tail = m.group(1), m.group(2)
            if not self.vocab or self.known(head + tail):
                return head + tail
            # Either side may be glued to its neighbours ("religiousdenom- inationmay"):
            # rejoin the longest real word that spans the break and leave the rest
            # for the word splitter. It must be at least as long as the whole words
            # already on each side, or "mutes- terms" (a catchline dash) would be
            # glued into some fragment.
            head_word = max((len(head) - i for i in range(len(head)) if self.known(head[i:])), default=0)
            tail_word = max((k for k in range(1, len(tail) + 1) if self.known(tail[:k])), default=0)
            best = None
            for i in range(len(head)):
                for k in range(len(tail), 0, -1):
                    word = head[i:] + tail[:k]
                    if len(word) >= 5 and self.known(word) and (best is None or len(word) > len(best[2])):
                        best = (i, k, word)
                        break
            if best and len(best[2]) >= head_word + tail_word:
                i, k, word = best
                return head[:i] + word + tail[k:]
            # A catchline dash ("Tax- amount that may be imposed") rather than a line break
            return f"{head}—{tail}"
        return BROKEN_WORD_RE.sub(join, text)

    def clean(self, text: str) -> str:
        if not text:
            return text
        text = re.sub(r"\*\*|__", "", text)                       # markdown bold from OCR
        text = LEAKED_HEADER_RE.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"^\$\s*(?=\d)", "§ ", text)                 # "$ 4534" → "§ 4534"
        text = re.sub(r"^§\s*(?=\d)", "§ ", text)
        # "theGeneral", "ofKentucky", "cityofMays- ville": split where a capital starts a word
        text = re.sub(r"(?<=[a-z]{2})(?=[A-Z][a-z])", " ", text)
        text = self._join_broken_words(text)
        text = LETTERS_RE.sub(lambda m: self._split_token(m.group(0)), text)
        text = self._join_broken_words(text)                      # splits can expose new ones
        # "purposeshallbe" → "purposes hall be": statutes say "shall", not "hall"
        text = re.sub(r"\b([A-Za-z]+)s hall(?= [a-z]|[,;:])",
                      lambda m: f"{m.group(1)} shall" if self.known(m.group(1)) else m.group(0), text)
        text = re.sub(r"\s+([.,;:)\]])", r"\1", text)              # "Kentucky :" → "Kentucky:"
        text = re.sub(r"([(\[])\s+", r"\1", text)
        text = re.sub(r"([,;:])(?=[A-Za-z])", r"\1 ", text)        # "fund,from" → "fund, from"
        text = re.sub(r"\s*—\s*", "—", text)
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip()


def shorten(text: str, limit: int) -> str:
    """Cut at a word boundary, never mid-word."""
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(" ", 1)[0].rstrip(",;:—-")  # +1: a word ending at the limit is whole
    return cut + "…"
