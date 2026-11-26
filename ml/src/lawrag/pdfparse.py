"""국가법령정보센터에서 내려받은 법률 PDF를 조문 단위로 읽는다.

대상 양식 (2026-11 출력본 기준)
- 쪽마다 머리말 `법제처  N  국가법령정보센터` + 법령 이름 한 줄
- 1쪽: 법령 이름, `[시행 2026. 9. 11.] [법률 제21445호, 2026. 3. 10., 일부개정]`, 담당 부서 연락처
- 본문: `제1장 총칙`, `제1절 …`, `제24조의2(주민등록번호 처리의 제한) ① …`
- 시행 예정 조문: 현행본 바로 뒤에 같은 조를 한 번 더 싣고 끝에 `[시행일: 2027. 3. 9.] 제2조`를 붙인다
- 부칙: `부칙 <제21445호,2026. 3. 10.>` 이후 조 번호가 다시 1부터 시작

텍스트 레이어가 있는 PDF만 다룬다 (스캔본 OCR은 범위 밖).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

# ── 패턴 ────────────────────────────────────────────────────────────────
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
PAGE_HEADER = re.compile(r"^법제처\s+(\d+)\s+국가법령정보센터$")
META = re.compile(
    r"\[시행\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]\s*"
    r"\[법률\s*제(\d+)호,\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.,\s*([^\]]+)\]"
)
CHAPTER = re.compile(r"^제(\d+)장(?:의(\d+))?\s+(.+)$")
SECTION = re.compile(r"^제(\d+)절\s+(.+)$")
# 조 제목 뒤에는 공백이나 줄 끝이 온다. 본문 줄이 `제28조의8(…)을 적용하지`처럼 시작하는 경우를 거르기 위한 조건.
ARTICLE = re.compile(r"^제(\d+)조(?:의(\d+))?\(([^()]*)\)(?=\s|$)\s*(.*)$")
# 삭제된 조는 제목 괄호가 없다: `제8조 삭제 <2020. 2. 4.>`
ARTICLE_DELETED = re.compile(r"^제(\d+)조(?:의(\d+))?\s+삭제\s*(<[^>]*>)?\s*$")
# 삭제된 항 · 호: `③ 삭제<2020. 2. 4.>`, `4. 삭제 <2023. 3. 14.>`
UNIT_DELETED = re.compile(rf"^(?:[{CIRCLED}]|\d+(?:의\d+)?\.)\s*삭제\s*(<[^>]*>)?\s*$")
ADDENDA = re.compile(r"^부\s*칙\s*(<[^>]*>)?\s*$")
EFFECTIVE_TAG = re.compile(r"\[시행일\s*:\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]\s*([^\[\n]*)")
DELETED = re.compile(r"^삭제\s*(<[^>]*>)?\s*$")
# 개정 이력 꼬리표: <개정 2014. 3. 24., 2020. 2. 4.>, <신설 …>, [본조신설 …], [전문개정 …], [제목개정 …], [종전 제…]
HISTORY_ANGLE = re.compile(r"<\s*(?:개정|신설|전문개정|타법개정)[^<>]*>")
HISTORY_BRACKET = re.compile(
    r"\[(?:본조신설|전문개정|제목개정|본조제목개정|종전\s|제\d+조(?:의\d+)?에서 이동)[^\[\]]*\]"
)
# 새 줄로 시작해야 하는 단위: 항(①), 호(1. / 1의2.), 목(가.), 꼬리표
UNIT_START = re.compile(rf"^(?:[{CIRCLED}]|\d+(?:의\d+)?\.\s|[가-하]\.\s|\[|<)")


class ParseError(ValueError):
    """PDF 양식이 예상과 달라 계속 진행하면 잘못된 색인이 만들어지는 경우."""


@dataclass
class LawMeta:
    name: str                 # 개인정보 보호법
    law_no: str               # 21445
    effective: str            # 2026-09-11
    promulgated: str          # 2026-03-10
    revision: str             # 일부개정
    pages: int
    sha256: str


@dataclass
class Unit:
    """조문 안의 한 줄 단위. 항(①) · 호(1.) · 목(가.) 또는 머리 문장."""

    kind: str                 # head(조 제목 뒤 첫 문장) | para(항) | item(호) | sub(목)
    label: str                # "①", "3", "가", ""
    text: str


@dataclass
class Article:
    part: str                 # 본문 | 부칙
    number: int               # 24
    branch: int | None        # 2  (제24조의2)
    title: str                # 주민등록번호 처리의 제한
    chapter: str              # 제3장 개인정보의 처리
    section: str              # 제1절 … (없으면 "")
    status: str               # 현행 | 시행예정 | 삭제
    effective_from: str       # 현행: 법률 시행일, 시행예정: 표시된 날짜
    effective_note: str       # 시행예정 범위 (예: "제32조의2제1항 단서")
    page: int                 # 조 제목이 있는 쪽
    units: list[Unit] = field(default_factory=list)
    history: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """조 번호 키: "24", "24의2"."""
        return f"{self.number}의{self.branch}" if self.branch else str(self.number)

    @property
    def label(self) -> str:
        return f"제{self.number}조" + (f"의{self.branch}" if self.branch else "")

    @property
    def text(self) -> str:
        return "\n".join(u.text for u in self.units)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"], d["label"] = self.key, self.label
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        """to_dict()의 역 (파이프라인 단계 사이에 JSONL로 넘길 때)."""
        d = {k: v for k, v in d.items() if k not in ("key", "label")}
        d["units"] = [Unit(**u) for u in d.get("units", [])]
        return cls(**d)


@dataclass
class ParsedLaw:
    meta: LawMeta
    articles: list[Article]
    addenda_title: str
    removed_header_lines: int

    def body(self) -> list[Article]:
        return [a for a in self.articles if a.part == "본문"]

    def addenda(self) -> list[Article]:
        return [a for a in self.articles if a.part == "부칙"]

    def stats(self) -> dict:
        body = self.body()
        return {
            "pages": self.meta.pages,
            "articles_body": len(body),
            "articles_current": sum(a.status == "현행" for a in body),
            "articles_future": sum(a.status == "시행예정" for a in body),
            "articles_deleted": sum(a.status == "삭제" for a in body),
            "articles_addenda": len(self.addenda()),
            "removed_header_lines": self.removed_header_lines,
        }


# ── 추출 ────────────────────────────────────────────────────────────────
def extract_pages(path: str) -> list[str]:
    from pypdf import PdfReader  # 무거운 의존성은 필요할 때만 불러온다

    reader = PdfReader(path)
    pages = [p.extract_text() or "" for p in reader.pages]
    if sum(len(p.strip()) for p in pages) < 100 * max(1, len(pages)):
        raise ParseError("텍스트가 거의 없습니다 — 스캔본 PDF는 지원하지 않습니다")
    return pages


def _date(y: str, m: str, d: str) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def parse_meta(first_page: str, pages: int, sha256: str) -> LawMeta:
    m = META.search(first_page)
    if not m:
        raise ParseError("1쪽에서 `[시행 …] [법률 제…호, …]` 표시를 찾지 못했습니다")
    lines = [ln.strip() for ln in first_page.splitlines() if ln.strip()]
    # 1쪽은 `법제처 1 국가법령정보센터` / 법령 이름 / 법령 이름 / [시행 …] 순서
    name = next((ln for ln in lines if not PAGE_HEADER.match(ln) and not ln.startswith("[")), "")
    if not name:
        raise ParseError("1쪽에서 법령 이름을 찾지 못했습니다")
    return LawMeta(
        name=name,
        law_no=m.group(4),
        effective=_date(m.group(1), m.group(2), m.group(3)),
        promulgated=_date(m.group(5), m.group(6), m.group(7)),
        revision=m.group(8).strip(),
        pages=pages,
        sha256=sha256,
    )


def _page_lines(pages: list[str], law_name: str) -> tuple[list[tuple[str, int]], int]:
    """쪽 머리말과 1쪽 서문(연락처)을 빼고 (줄, 쪽 번호) 목록을 돌려준다."""
    out: list[tuple[str, int]] = []
    removed = 0
    for pno, page in enumerate(pages, start=1):
        lines = page.splitlines()
        i = 0
        # 머리말: `법제처 N 국가법령정보센터` 다음 줄이 법령 이름
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i < len(lines) and PAGE_HEADER.match(lines[i].strip()):
            removed += 1
            i += 1
            if i < len(lines) and lines[i].strip() == law_name:
                removed += 1
                i += 1
        for ln in lines[i:]:
            s = ln.strip()
            if s:
                out.append((s, pno))
    # 1쪽 서문: 첫 장 제목(제1장) 또는 첫 조(제1조) 앞은 모두 버린다
    start = next((k for k, (s, _) in enumerate(out) if CHAPTER.match(s) or ARTICLE.match(s)), None)
    if start is None:
        raise ParseError("본문 시작(제1장 또는 제1조)을 찾지 못했습니다")
    return out[start:], removed + start


def _join(lines: list[str]) -> list[str]:
    """PDF 줄바꿈을 되돌린다. 항 · 호 · 목 · 꼬리표 앞에서만 줄을 나눈다.

    한국어는 단어 중간에서 줄이 바뀌므로(`해\\n당하는`) 공백 없이 붙인다. 앞 줄이 문장 부호로 끝나면 공백을 넣는다.
    PDF 추출에서 줄 끝 공백이 사라지므로 `개인정보의\\n처리`는 `개인정보의처리`가 된다(검색 영향은 평가로 확인).
    """
    units: list[str] = []
    for s in lines:
        if not units or UNIT_START.match(s):
            units.append(s)
        else:
            prev = units[-1]
            sep = " " if prev[-1] in ".,;:)>]" or prev[-1].isascii() and s[0].isascii() else ""
            units[-1] = prev + sep + s
    return units


def _strip_history(text: str) -> tuple[str, list[str]]:
    found = HISTORY_ANGLE.findall(text) + HISTORY_BRACKET.findall(text)
    text = HISTORY_BRACKET.sub("", HISTORY_ANGLE.sub("", text))
    return re.sub(r"[ \t]{2,}", " ", text).strip(), [re.sub(r"\s+", " ", h) for h in found]


def _to_unit(s: str) -> Unit:
    if s[0] in CIRCLED:
        return Unit("para", s[0], s)
    if m := re.match(r"^(\d+(?:의\d+)?)\.\s", s):
        return Unit("item", m.group(1), s)
    if m := re.match(r"^([가-하])\.\s", s):
        return Unit("sub", m.group(1), s)
    return Unit("head", "", s)


def _order(part: str, number: int, branch: int | None) -> tuple:
    return (part == "부칙", number, branch or 0)


# ── 본 처리 ──────────────────────────────────────────────────────────────
def parse_pdf(path: str) -> ParsedLaw:
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    pages = extract_pages(path)
    return parse_pages(pages, sha256=sha)


def parse_pages(pages: list[str], sha256: str = "") -> ParsedLaw:
    meta = parse_meta(pages[0], len(pages), sha256)
    lines, removed = _page_lines(pages, meta.name)

    articles: list[Article] = []
    buf: list[str] = []
    cur: dict | None = None
    part, chapter, section, addenda_title = "본문", "", "", ""
    last = (False, 0, 0)

    def flush():
        nonlocal cur, buf
        if cur is not None:
            articles.append(_build(cur, buf, meta))
        cur, buf = None, []

    for s, pno in lines:
        if m := ADDENDA.match(s):
            flush()
            part, chapter, section = "부칙", "", ""
            addenda_title = s.replace(" ", "")
            last = (True, 0, 0)
            continue
        if part == "본문" and (m := CHAPTER.match(s)):
            flush()
            chapter, section = re.sub(r"\s*<[^>]*>\s*$", "", s), ""
            continue
        if part == "본문" and (m := SECTION.match(s)):
            flush()
            section = re.sub(r"\s*<[^>]*>\s*$", "", s)
            continue
        if m := ARTICLE_DELETED.match(s):
            number, branch = int(m.group(1)), int(m.group(2)) if m.group(2) else None
            order = _order(part, number, branch)
            if order >= last:
                flush()
                last = order
                articles.append(Article(
                    part=part, number=number, branch=branch, title="", chapter=chapter, section=section,
                    status="삭제", effective_from=meta.effective, effective_note="", page=pno,
                    history=[m.group(3)] if m.group(3) else [],
                ))
                continue
        if m := ARTICLE.match(s):
            number, branch = int(m.group(1)), int(m.group(2)) if m.group(2) else None
            order = _order(part, number, branch)
            # 조 번호는 줄지 않는다(같은 번호 = 시행 예정본). 줄어들면 본문 속 조 인용이 줄 첫머리에 온 경우다.
            if order >= last:
                flush()
                last = order
                cur = {"part": part, "number": number, "branch": branch, "title": m.group(3).strip(),
                       "chapter": chapter, "section": section, "page": pno}
                buf = [s]
                continue
        if cur is not None:
            buf.append(s)
    flush()

    law = ParsedLaw(meta=meta, articles=articles, addenda_title=addenda_title, removed_header_lines=removed)
    _mark_future(law)
    validate(law)
    return law


def _build(cur: dict, buf: list[str], meta: LawMeta) -> Article:
    units_raw = _join(buf)
    # 조 제목 줄에서 `제N조(제목)` 부분을 떼고 첫 문장만 남긴다
    first = ARTICLE.match(units_raw[0])
    units_raw[0] = first.group(4).strip() if first else units_raw[0]

    status, eff, note = "현행", meta.effective, ""
    text = "\n".join(u for u in units_raw if u)
    if m := EFFECTIVE_TAG.search(text):
        status, eff, note = "시행예정", _date(m.group(1), m.group(2), m.group(3)), m.group(4).strip()
        text = EFFECTIVE_TAG.sub("", text)
    text, history = _strip_history(text)
    units = []
    for u in (u.strip() for u in text.split("\n")):
        if not u:
            continue
        if UNIT_DELETED.match(u):          # 삭제된 항 · 호는 본문에서 빼고 이력에만 남긴다
            history.append(u)
            continue
        # 머리 문장이 항(①)으로 시작하면 `제24조의2(…) ① …`이므로 head가 아니라 para다 (_to_unit이 처리)
        units.append(_to_unit(u))
    if not units or len(units) == 1 and DELETED.match(units[0].text):
        status = "삭제"
    return Article(
        part=cur["part"], number=cur["number"], branch=cur["branch"], title=cur["title"],
        chapter=cur["chapter"], section=cur["section"], status=status, effective_from=eff,
        effective_note=note, page=cur["page"], units=units, history=history,
    )


def _mark_future(law: ParsedLaw) -> None:
    """같은 조가 두 번 나오면 앞은 현행, 뒤는 시행예정이어야 한다. 뒤에 표시가 없으면 양식이 다른 것."""
    seen: dict[tuple, Article] = {}
    for a in law.articles:
        k = (a.part, a.number, a.branch)
        if k in seen and a.status != "시행예정":
            raise ParseError(f"{a.part} {a.label}가 두 번 나오는데 뒤의 것에 [시행일] 표시가 없습니다")
        seen.setdefault(k, a)


def validate(law: ParsedLaw) -> None:
    """색인을 만들기 전에 반드시 통과해야 하는 검사."""
    body = law.body()
    if not body:
        raise ParseError("본문 조문이 없습니다")
    numbers = sorted({a.number for a in body})
    missing = sorted(set(range(1, numbers[-1] + 1)) - set(numbers))
    if missing:
        raise ParseError(f"빠진 조 번호가 있습니다: {missing[:10]}")
    if not law.addenda():
        raise ParseError("부칙을 찾지 못했습니다")
    for a in law.articles:
        if a.status != "삭제" and not a.text.strip():
            raise ParseError(f"{a.label}의 본문이 비어 있습니다")
        if "국가법령정보센터" in a.text:
            raise ParseError(f"{a.label}에 쪽 머리말이 남아 있습니다")
