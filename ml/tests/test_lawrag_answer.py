import numpy as np

from lawrag.answer import ask, clean, extract_citations, future_notices, is_refusal, verify_citations
from lawrag.chunk import Chunk
from lawrag.prompt import NOT_FOUND, SYSTEM, build_messages
from lawrag.store import Hit


def _chunk(key, part="본문", status="현행", body="본문", eff="2026-09-11"):
    label = f"제{key.replace('의', '조의')}" if "의" in key else f"제{key}조"
    return Chunk(chunk_id=f"{part}{key}{status}", law_name="개인정보 보호법", law_no="21445", part=part,
                 article_key=key, article_label=label, title="제목", chapter="", section="", status=status,
                 effective_from=eff, effective_note="", paragraphs=[], items=[], page=3,
                 header=f"[개인정보 보호법 {label}(제목)]", body=body)


class FakeBackend:
    def __init__(self, hits):
        self.hits = hits

    def search(self, index_version, qvec, k):
        return self.hits[:k]

    def get_article(self, index_version, part, key):
        return [h.chunk for h in self.hits if h.chunk.part == part and h.chunk.article_key == key]


class FakeLLM:
    model = "fake"

    def __init__(self, text):
        self.text, self.messages = text, None

    def chat(self, messages, **kw):
        self.messages = messages
        return clean(self.text), {"completion_tokens": 10}


def test_extract_citations():
    a = ("「개인정보 보호법」 제24조의2제1항과 제15조, 같은 법 제 24 조 의 2에 따르면 … 부칙 제1조 "
         "「행정기본법」 제20조와 「인공지능 발전과 신뢰 기반 조성 등에 관한 기본법」 제2조제3호는 제외")
    assert extract_citations(a) == ["24의2", "15", "부칙1"]


def test_verify_citations():
    hits = [Hit(_chunk("24의2", body="① 제24조제1항에도 불구하고"), 0.7, "vector")]
    cites = verify_citations("제24조의2와 제24조, 제99조를 보면", hits)
    assert [(c.key, c.status) for c in cites] == [("24의2", "retrieved"), ("24", "referenced"), ("99", "unsupported")]


def test_clean_and_refusal():
    assert clean("<think>생각</think>\n답입니다") == "답입니다"
    assert clean("답<think>끝나지 않은 생각") == "답"
    assert is_refusal(f"그 법에는 없습니다. {NOT_FOUND}") and not is_refusal("제15조에 따르면 됩니다.")


def test_future_notices():
    only_future = [Hit(_chunk("28의12", status="시행예정", eff="2027-03-09"), 0.7, "vector")]
    cites = verify_citations("제28조의12에 따르면 가능합니다.", only_future)
    assert future_notices("제28조의12에 따르면 가능합니다.", cites, only_future) == \
        ["※ 「개인정보 보호법」 제28조의12는 2027년 3월 9일부터 시행될 예정인 조문입니다."]
    assert future_notices("제28조의12(2027년 3월 9일 시행)에 따르면", cites, only_future) == []
    mixed = [Hit(_chunk("2"), 0.7, "vector"), Hit(_chunk("2", status="시행예정", eff="2027-03-09"), 0.6, "vector")]
    notes = future_notices("제2조에 따르면", verify_citations("제2조에 따르면", mixed), mixed)
    assert len(notes) == 1 and "개정 내용" in notes[0]
    current_only = [Hit(_chunk("15"), 0.7, "vector")]
    assert future_notices("제15조", verify_citations("제15조", current_only), current_only) == []


def test_prompt_contains_context_and_rules():
    msgs = build_messages("질문?", [Hit(_chunk("15", body="① 수집할 수 있다"), 0.7, "vector")])
    assert msgs[0]["content"] == SYSTEM and NOT_FOUND in SYSTEM
    assert "[근거 1] [개인정보 보호법 제15조(제목)]\n① 수집할 수 있다" in msgs[1]["content"]
    assert msgs[1]["content"].endswith("[질문]\n질문?")


def test_ask_masks_and_verifies():
    hits = [Hit(_chunk("24의2"), 0.7, "vector")]
    llm = FakeLLM("<think>x</think>제24조의2에 따라 900101-1234567 같은 번호는 처리할 수 없습니다.")
    a = ask("900101-1234567 받아도 되나요?", lambda q: np.zeros(3), FakeBackend(hits), "v", llm)
    assert a.question == "900101-1****** 받아도 되나요?"
    assert "1234567" not in llm.messages[1]["content"]          # LLM에도 가린 질문만 보낸다
    assert "900101-1******" in a.answer and "1234567" not in a.answer
    assert [c.key for c in a.citations] == ["24의2"] and a.citations_verified and not a.refused
    assert a.retrieved[0]["page"] == 3 and not a.skipped_llm


def test_ask_skips_llm_when_irrelevant():
    llm = FakeLLM("호출되면 안 됨")
    a = ask("점심 메뉴", lambda q: np.zeros(3), FakeBackend([Hit(_chunk("74"), 0.43, "vector")]), "v", llm,
            min_score=0.48)
    assert a.refused and a.skipped_llm and a.answer == NOT_FOUND and llm.messages is None


def test_ask_direct_article_never_skipped():
    hits = [Hit(_chunk("24의2"), 1.0, "article")]
    a = ask("제24조의2 알려줘", lambda q: np.zeros(3), FakeBackend(hits), "v", FakeLLM("제24조의2는 …"), min_score=0.99)
    assert not a.skipped_llm and a.top_score == 1.0
