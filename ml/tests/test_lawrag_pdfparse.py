import os

import pytest

from lawrag.pdfparse import ParseError, parse_pages, parse_pdf

PDF = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "개인정보 보호법(법률)(제21445호)(20260911).pdf")

HEAD = "법제처                     {n}                     국가법령정보센터\n테스트법\n"
FIRST = HEAD.format(n=1) + "테스트법\n[시행 2026. 9. 11.] [법률 제100호, 2026. 3. 10., 일부개정]\n담당 부서 02-000-0000\n"


def _law(*body_pages: str):
    pages = [FIRST + body_pages[0]] + [HEAD.format(n=i + 2) + p for i, p in enumerate(body_pages[1:])]
    return parse_pages(pages)


# ── 가짜 쪽 텍스트로 경계 사례 ────────────────────────────────────────────
def test_meta_and_preamble_removed():
    law = _law("제1장 총칙\n제1조(목적) 이 법은 목적이 있다. <개정 2014. 3. 24.>\n제2조(정의) 뜻은 다음과 같다.\n"
               "1. “가”란 가를 말한다.\n부칙 <제100호,2026. 3. 10.>\n제1조(시행일) 공포 후 시행한다.\n")
    assert (law.meta.name, law.meta.law_no, law.meta.effective, law.meta.promulgated) == \
        ("테스트법", "100", "2026-09-11", "2026-03-10")
    a1 = law.articles[0]
    assert a1.text == "이 법은 목적이 있다."            # 개정 이력은 본문에서 빠진다
    assert a1.history == ["<개정 2014. 3. 24.>"]
    assert a1.chapter == "제1장 총칙"
    assert all("담당 부서" not in a.text for a in law.articles)
    assert [a.part for a in law.articles] == ["본문", "본문", "부칙"]


def test_page_header_and_line_join():
    law = _law("제1조(목적) 이 법은 다음 각 호의 어느 하나에 해\n",
               "당하는 경우에 적용한다.\n① 첫째 항이다.\n제2조(기타) 끝.\n부칙\n제1조(시행일) 시행.\n")
    text = law.articles[0].text
    assert "해당하는 경우에 적용한다." in text          # 쪽이 바뀐 단어 중간 줄바꿈을 공백 없이 잇는다
    assert "국가법령정보센터" not in text and "테스트법" not in text
    assert law.articles[0].units[1].kind == "para"


def test_inline_citation_at_line_start_is_not_an_article():
    law = _law("제1조(목적) 이 법은 제5조까지,\n제28조의8(국외로 위탁하는 경우에 한정한다)을 적용하지 아니한다.\n"
               "제2조(정의) 끝.\n부칙\n제1조(시행일) 시행.\n")
    assert [a.label for a in law.body()] == ["제1조", "제2조"]
    assert "제28조의8(국외로 위탁하는 경우에 한정한다)을" in law.articles[0].text


def test_future_version_and_deleted():
    law = _law("제1조(목적) 현행 문장.\n[본조신설 2020. 2. 4.]\n제1조(목적) 바뀐 문장.\n[시행일: 2027. 3. 9.] 제1조\n"
               "제2조 삭제 <2020. 2. 4.>\n제3조(기타) ① 남는 항.\n② 삭제<2020. 2. 4.>\n부칙\n제1조(시행일) 시행.\n")
    cur, fut, deleted, other = law.body()
    assert (cur.status, cur.effective_from, cur.text) == ("현행", "2026-09-11", "현행 문장.")
    assert (fut.status, fut.effective_from, fut.effective_note, fut.text) == \
        ("시행예정", "2027-03-09", "제1조", "바뀐 문장.")
    assert deleted.status == "삭제" and deleted.label == "제2조"
    assert other.text == "① 남는 항."                  # 삭제된 항은 이력으로만
    assert "② 삭제<2020. 2. 4.>" in other.history


def test_duplicate_without_effective_tag_fails():
    with pytest.raises(ParseError, match="두 번"):
        _law("제1조(목적) 하나.\n제1조(목적) 둘.\n부칙\n제1조(시행일) 시행.\n")


def test_missing_article_number_fails():
    with pytest.raises(ParseError, match="빠진 조"):
        _law("제1조(목적) 하나.\n제3조(기타) 셋.\n부칙\n제1조(시행일) 시행.\n")


def test_missing_meta_fails():
    with pytest.raises(ParseError, match="시행"):
        parse_pages(["법령 이름만 있음\n제1조(목적) 하나.\n" * 10])


# ── 실제 PDF ─────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def law():
    if not os.path.exists(PDF):
        pytest.skip("법률 PDF가 없습니다")
    pytest.importorskip("pypdf")
    return parse_pdf(PDF)


def test_real_pdf_meta(law):
    m = law.meta
    assert (m.name, m.law_no, m.effective, m.promulgated, m.pages) == \
        ("개인정보 보호법", "21445", "2026-09-11", "2026-03-10", 47)


def test_real_pdf_counts(law):
    assert law.stats() == {
        "pages": 47, "articles_body": 136, "articles_current": 125, "articles_future": 9,
        "articles_deleted": 2, "articles_addenda": 4, "removed_header_lines": 105,
    }
    assert [a.label for a in law.articles if a.status == "삭제"] == ["제8조", "제28조의6"]


def test_real_pdf_future_articles(law):
    fut = {a.label: (a.effective_from, a.effective_note) for a in law.articles if a.status == "시행예정"}
    assert fut["제2조"] == ("2027-03-09", "제2조")
    assert fut["제32조의2"] == ("2027-07-01", "제32조의2제1항 단서")
    assert fut["제75조"] == ("2027-07-01", "제75조제2항제15호")
    assert {"제28조의12", "제28조의13", "제28조의14", "제28조의15"} <= set(fut)
    # 시행 예정 제2조에만 '인공지능기술' 정의(제9호)가 있다
    cur2 = next(a for a in law.articles if a.key == "2" and a.status == "현행")
    fut2 = next(a for a in law.articles if a.key == "2" and a.status == "시행예정")
    assert "인공지능기술" not in cur2.text and "9. “인공지능기술”이란" in fut2.text


def test_real_pdf_article_24_2(law):
    a = next(a for a in law.articles if a.key == "24의2")
    assert a.title == "주민등록번호 처리의 제한"
    assert a.chapter == "제3장 개인정보의 처리" and a.section == "제2절 개인정보의 처리 제한"
    assert [u.label for u in a.units if u.kind == "para"] == ["①", "②", "③", "④"]
    assert "[본조신설 2013. 8. 6.]" in a.history


def test_real_pdf_no_noise(law):
    for a in law.articles:
        assert "국가법령정보센터" not in a.text
        assert "<개정" not in a.text and "[본조신설" not in a.text and "[시행일" not in a.text
