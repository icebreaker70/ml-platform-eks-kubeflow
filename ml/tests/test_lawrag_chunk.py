import os

import pytest

from lawrag.chunk import chunk_article, chunk_law
from lawrag.pdfparse import Article, LawMeta, Unit, parse_pdf

PDF = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "개인정보 보호법(법률)(제21445호)(20260911).pdf")
META = LawMeta("테스트법", "100", "2026-09-11", "2026-03-10", "일부개정", 1, "")


def _article(units, status="현행", **kw):
    base = dict(part="본문", number=24, branch=2, title="제목", chapter="제3장 처리", section="",
                status=status, effective_from="2026-09-11", effective_note="", page=1, units=units)
    base.update(kw)
    return Article(**base)


def test_short_article_is_one_chunk():
    a = _article([Unit("para", "①", "① 첫째."), Unit("para", "②", "② 둘째.")])
    [c] = chunk_article(META, a)
    assert c.chunk_id == "100-L24_2-C-01"
    assert c.header == "[테스트법 제3장 처리 제24조의2(제목)]"
    assert c.paragraphs == [] and c.text.endswith("① 첫째.\n② 둘째.")


def test_long_article_splits_by_paragraph():
    units = [Unit("para", ch, f"{ch} " + "가" * 50) for ch in "①②③④"]
    cs = chunk_article(META, _article(units), max_chars=120)
    assert [c.paragraphs for c in cs] == [[1, 2], [3, 4]]
    assert cs[1].header.endswith("제24조의2(제목) 제3항~제4항]")


def test_long_paragraph_splits_by_item_and_repeats_lead():
    lead = Unit("para", "②", "② 다음 각 호의 자에게는 과태료를 부과한다.")
    items = [Unit("item", str(i), f"{i}. " + "나" * 40) for i in range(1, 7)]
    cs = chunk_article(META, _article([lead] + items), max_chars=150)
    assert len(cs) > 1
    assert all(c.body.startswith(lead.text) for c in cs)
    assert cs[0].items[0] == "1" and cs[-1].items[-1] == "6"
    assert "제2항 제1호~" in cs[0].header


def test_future_and_deleted():
    fut = _article([Unit("head", "", "문장.")], status="시행예정", effective_from="2027-03-09", effective_note="제24조의2")
    [c] = chunk_article(META, fut)
    assert c.chunk_id.endswith("-F-01") and c.header.endswith("· 2027-03-09 시행 예정")
    part = _article([Unit("head", "", "문장.")], status="시행예정", effective_from="2027-07-01",
                    effective_note="제24조의2제1항 단서")
    assert chunk_article(META, part)[0].header.endswith("시행 예정(제24조의2제1항 단서)")
    assert chunk_article(META, _article([], status="삭제")) == []


@pytest.fixture(scope="module")
def chunks():
    if not os.path.exists(PDF):
        pytest.skip("법률 PDF가 없습니다")
    pytest.importorskip("pypdf")
    return chunk_law(parse_pdf(PDF))


def test_real_pdf_future_dedupe(chunks):
    # 제75조 시행 예정본은 제2항제15호만 바뀌었다 → 그 호가 든 청크만 남는다
    fut75 = [c for c in chunks if c.article_key == "75" and c.status == "시행예정"]
    assert [(c.paragraphs, c.items[0]) for c in fut75] == [([2], "15")]
    assert sum(c.status == "시행예정" for c in chunks) == 9


def test_real_pdf_chunks(chunks):
    assert len(chunks) == 143
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    assert all(c.status in ("현행", "시행예정") for c in chunks)
    assert max(len(c.body) for c in chunks) <= 1500
    by = {}
    for c in chunks:
        by.setdefault((c.part, c.article_key, c.status), []).append(c)
    assert len(by[("본문", "75", "현행")]) == 4 and len(by[("본문", "64의2", "현행")]) == 3
    assert by[("부칙", "1", "현행")][0].header.startswith("[개인정보 보호법 부칙<제21445호,2026.3.10.> 제1조(시행일)")
