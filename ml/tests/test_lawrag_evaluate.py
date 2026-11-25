import os
import re

import pytest

from lawrag.chunk import Chunk
from lawrag.evaluate import QA, Result, best_threshold, load_qa, metrics, ref_key
from lawrag.store import Hit

PDF = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "개인정보 보호법(법률)(제21445호)(20260911).pdf")


def _hit(key, part="본문", status="현행", score=0.7, source="vector"):
    c = Chunk(chunk_id=f"{part}{key}{status}", law_name="법", law_no="1", part=part, article_key=key,
              article_label="", title="", chapter="", section="", status=status, effective_from="2026-09-11",
              effective_note="", paragraphs=[], items=[], page=1, header="", body="")
    return Hit(c, score, source)


def test_ref_key():
    assert ref_key("본문", "24의2") == "24의2" and ref_key("부칙", "1") == "부칙1"


def test_result_metrics():
    q = QA("x", "복합", "?", ["2", "28의2"], expect_status="시행예정")
    r = Result(q, [_hit("15"), _hit("28의2"), _hit("2", status="시행예정")])
    assert r.recall == 1.0 and r.rank == 2 and r.status_ok is True
    r2 = Result(q, [_hit("2")])  # 현행만 찾음
    assert r2.recall == 0.5 and r2.status_ok is False


def test_threshold_and_metrics():
    ans = [Result(QA(f"a{i}", "", "?", ["1"]), [_hit("1", score=s)]) for i, s in enumerate([0.6, 0.7, 0.8])]
    no = [Result(QA(f"u{i}", "", "?", []), [_hit("9", score=s)]) for i, s in enumerate([0.4, 0.45])]
    t, acc = best_threshold(ans + no)
    assert 0.45 < t < 0.6 and acc == 1.0
    m = metrics(ans + no, k=5)
    assert m["recall_at_5"] == 1.0 and m["mrr_at_5"] == 1.0 and m["answerable"] == 3
    assert m["unanswerable_max_score"] == 0.45 and m["answerable_min_score"] == 0.6
    # 조문 번호 직접 매칭은 답 있음으로 본다
    assert Result(QA("n", "", "?", ["1"]), [_hit("1", score=1.0, source="article")]).top_vector_score == 1.0


# ── 평가 세트 자체 검사 ─────────────────────────────────────────────────────
def test_qa_set_shape():
    qas = load_qa()
    assert len(qas) >= 30
    assert len({q.id for q in qas}) == len(qas)
    assert sum(not q.answerable for q in qas) >= 3
    assert all(q.points for q in qas if q.answerable)
    assert {q.type for q in qas} >= {"상황", "개념", "복합", "조문번호", "시행예정", "부칙", "답없음"}


@pytest.fixture(scope="module")
def law_text():
    if not os.path.exists(PDF):
        pytest.skip("법률 PDF가 없습니다")
    pytest.importorskip("pypdf")
    from lawrag.pdfparse import parse_pdf

    law = parse_pdf(PDF)
    text: dict[tuple[str, str], str] = {}
    for a in law.articles:
        k = (ref_key(a.part, a.key), a.status)
        text[k] = text.get(k, "") + a.title + a.text
    return text


def test_qa_expected_articles_and_points_exist(law_text):
    """정답 조가 실제로 있고, 핵심 표현이 그 조 본문에 있는지 (띄어쓰기 무시 — PDF 줄바꿈으로 공백이 사라짐)."""
    norm = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    for q in load_qa():
        statuses = [q.expect_status] if q.expect_status else ["현행", "시행예정"]
        texts = [law_text.get((k, s), "") for k in q.expected for s in statuses]
        for k in q.expected:
            assert any((k, s) in law_text for s in statuses), f"{q.id}: 정답 조 {k}({statuses})가 없습니다"
        joined = norm("".join(texts))
        for p in q.points:
            assert norm(p) in joined, f"{q.id}: '{p}'가 정답 조 본문에 없습니다"
