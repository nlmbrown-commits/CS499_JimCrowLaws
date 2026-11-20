"""
test_chatbot.py — Tests for the archive chatbot (chatbot/).

Run:
    pytest tests/test_chatbot.py -v
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from chatbot.archive import Archive, parse_years, query_terms
from chatbot.bot import ArchiveChat


def _law(law_id, title, year, text, key_provisions=(), summary="", category="education"):
    return {"law_id": law_id, "title": title, "year": year, "full_text": text,
            "key_provisions": list(key_provisions), "summary": summary, "category": category}


def _statute(statute_id, citation, title, laws, unit_type="section", needs_review=False, category="education"):
    years = [l["year"] for l in laws]
    return {"statute_id": statute_id, "statute_citation": citation, "title": title, "unit_type": unit_type,
            "printings": len(laws), "first_year": min(years), "last_year": max(years), "category": category,
            "racial_indicator": "explicit", "needs_review": needs_review, "grouped_by": [],
            "citations": [f"{citation}, {y}" for y in years], "law_ids": [l["law_id"] for l in laws],
            "summary": laws[0]["summary"]}


@pytest.fixture
def laws_file(tmp_path):
    coaches = [_law("c1", "§ 795. Separate coaches.", 1903,
                    "§ 795. Every railroad shall furnish separate coaches for white and colored passengers.",
                    ["§ 795. Every railroad shall furnish separate coaches for white and colored passengers."],
                    "Requires railroads to provide separate coaches.", "public_accommodations"),
               _law("c2", "§ 795. Separate coaches.", 1922,
                    "§ 795. Every railroad shall furnish separate coaches for white and colored passengers.",
                    summary="Requires separate coaches.", category="public_accommodations")]
    day_law = [_law("d1", "AN ACT to prohibit white and colored persons from attending the same school.", 1904,
                    "AN ACT to prohibit white and colored persons from attending the same school. Fine of $1,000.",
                    ["It shall be unlawful for any school to receive both white and negro pupils."],
                    "Makes it unlawful to teach white and colored pupils in the same school.")]
    marriage = [_law("m1", "AN ACT in relation to the marriage of negroes and mulattoes.", 1866,
                     "AN ACT in relation to the marriage of negroes and mulattoes.",
                     summary="Regulates marriage of Black Kentuckians.", category="marriage")]
    asylum = [_law("a1", "AN ACT for rebuilding Western Lunatic Asylum.", 1866,
                   "AN ACT for rebuilding Western Lunatic Asylum. Negro and mulatto lunatics; negro patients.",
                   summary="Funds the asylum.", category="other")]
    data = {
        "statistics": {"flagged_pages": 10, "unique_laws": 5},
        "laws": coaches + day_law + marriage + asylum,
        "statutes": [
            _statute("s_coach", "Ky. Stat. § 795", "§ 795. Separate coaches.", coaches, category="public_accommodations"),
            _statute("s_day", "Acts 1904, ch. 85", day_law[0]["title"], day_law, unit_type="act"),
            _statute("s_marriage", "Acts 1866, ch. 1", marriage[0]["title"], marriage, unit_type="act", category="marriage"),
            _statute("s_asylum", "Acts 1866, ch. 2", asylum[0]["title"], asylum, unit_type="act",
                     needs_review=True, category="Other"),
        ],
    }
    path = tmp_path / "laws.json"
    path.write_text(json.dumps(data))
    return path


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.messages = reply, None

    def chat(self, messages):
        self.messages = messages
        return type("R", (), {"message": type("M", (), {"content": self.reply})()})()


class TestParseYears:
    @pytest.mark.parametrize("question,expected", [
        ("What happened in 1904?", (1904, 1904)),
        ("laws in the 1890s", (1890, 1899)),
        ("before 1900", (None, 1899)),
        ("after 1920", (1921, None)),
        ("after the 1920s", (1930, None)),
        ("between 1890 and 1910", (1890, 1910)),
        ("from 1880s to 1890s", (1880, 1899)),
        ("1904-1910", (1904, 1910)),
        ("no years here", (None, None)),
    ])
    def test_year_phrases(self, question, expected):
        assert parse_years(question) == expected


class TestArchive:
    def test_search_finds_the_right_statute(self, laws_file):
        archive = Archive(laws_file)
        top, _ = archive.search("Which laws required separate railroad coaches?")[0]
        assert top.statute_id == "s_coach"
        assert top.printings == 2 and top.years == [1903, 1922]

    def test_year_filter_uses_printing_years(self, laws_file):
        archive = Archive(laws_file)
        ids = {s.statute_id for s, _ in archive.search("separate coaches", 1920, 1929)}
        assert ids == {"s_coach"}  # printed 1903 and 1922
        assert archive.search("separate coaches", 1950, 1960) == []

    def test_synonym_only_match_ranks_below_asked_words(self, laws_file):
        # "interracial" expands to negro/mulatto; the asylum act repeats those but never says marriage
        results = Archive(laws_file).search("interracial marriage")
        assert results[0][0].statute_id == "s_marriage"

    def test_synonyms_weigh_less(self):
        terms = query_terms("black schools")
        assert terms["black"] == 1.0 and terms["color"] < 1.0  # terms are stemmed

    def test_facts_count_exactly(self, laws_file):
        facts = Archive(laws_file).facts("separate schools", 1860, 1869)
        assert facts["statutes_in_year_range"] == 2
        assert facts["by_category"] == {"marriage": 1, "other_or_uncategorized": 1}
        assert facts["dataset"]["distinct_statutes"] == 4


class TestArchiveChat:
    def test_retrieval_only_without_llm(self, laws_file):
        bot = ArchiveChat(Archive(laws_file), cleaner=False)
        bot._llm_error = "no key"
        result = bot.answer("separate railroad coaches")
        assert result["mode"] == "retrieval_only"
        assert result["sources"][0]["statute_id"] == "s_coach"
        assert result["sources"][0]["number"] == 1

    def test_answer_cites_sources_and_sends_context(self, laws_file):
        llm = FakeLLM("Railroads had to provide separate coaches [1]. Not in sources [9].")
        bot = ArchiveChat(Archive(laws_file), llm=llm, cleaner=False)
        result = bot.answer("Which laws required separate railroad coaches?")
        assert result["mode"] == "llm"
        assert result["sources"][0]["cited"] is True
        assert not any(s["cited"] for s in result["sources"][1:])
        prompt = llm.messages[-1].content
        assert "ARCHIVE FACTS" in prompt and "[1] Ky. Stat. § 795" in prompt
        assert "Law's own text" in prompt and "may describe a neighbouring law" in prompt

    def test_follow_up_keeps_topic_and_years(self, laws_file):
        llm = FakeLLM("ok")
        bot = ArchiveChat(Archive(laws_file), llm=llm, cleaner=False)
        history = [{"role": "user", "content": "What did the Day Law of 1904 do?"},
                   {"role": "assistant", "content": "It segregated schools [1]."}]
        result = bot.answer("Tell me more", history)
        assert result["facts"]["year_filter"] == {"from": 1904, "to": 1904}
        assert result["sources"][0]["statute_id"] == "s_day"
        roles = [m.role.value if hasattr(m.role, "value") else m.role for m in llm.messages]
        assert roles == ["system", "user", "assistant", "user"]

    def test_empty_question(self, laws_file):
        assert ArchiveChat(Archive(laws_file), cleaner=False).answer("  ")["mode"] == "empty"


class TestChatEndpoint:
    @pytest.fixture
    def client(self, laws_file, monkeypatch):
        pytest.importorskip("flask")
        pytest.importorskip("psycopg2")
        import api_server
        monkeypatch.setattr(api_server, "_chat",
                            ArchiveChat(Archive(laws_file), llm=FakeLLM("Separate coaches [1]."), cleaner=False))
        return api_server.app.test_client()

    def test_chat_returns_answer_and_sources(self, client):
        response = client.post("/chat", json={"message": "separate coaches", "history": []})
        assert response.status_code == 200
        body = response.get_json()
        assert body["answer"] == "Separate coaches [1]."
        assert body["sources"][0]["citation"] == "Ky. Stat. § 795"

    def test_chat_requires_a_message(self, client):
        assert client.post("/chat", json={}).status_code == 400
        assert client.post("/chat", data="not json").status_code == 400

    def test_chat_ignores_malformed_history(self, client):
        response = client.post("/chat", json={"message": "separate coaches", "history": "nonsense"})
        assert response.status_code == 200
