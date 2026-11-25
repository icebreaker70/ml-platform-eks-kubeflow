"""조문(Article) → 검색용 청크(Chunk).

- 기본은 조 하나 = 청크 하나.
- 조가 max_chars보다 길면 항(①) 단위 묶음으로 나눈다. 항 하나가 그래도 길면 호(1.) 묶음으로 나누고
  각 조각 앞에 그 항의 첫 문장("다음 각 호의 어느 하나에 해당하는 자에게는 …")을 반복한다.
- 삭제된 조는 청크를 만들지 않는다.
- 청크 머리말 `[개인정보 보호법 제3장 … 제24조의2(주민등록번호 처리의 제한) 제1항]`을 본문 앞에 붙여 임베딩한다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .pdfparse import CIRCLED, Article, LawMeta, ParsedLaw, Unit

DEFAULT_MAX_CHARS = 1500  # 약 800 토큰 (bge-m3 토크나이저, 한국어 기준 대략)


@dataclass
class Chunk:
    chunk_id: str             # 21445-L24_2-C-01
    law_name: str
    law_no: str
    part: str                 # 본문 | 부칙
    article_key: str          # 24의2
    article_label: str        # 제24조의2
    title: str
    chapter: str
    section: str
    status: str               # 현행 | 시행예정
    effective_from: str
    effective_note: str
    paragraphs: list[int]     # 이 청크에 든 항 번호 (조 전체면 [])
    items: list[str]          # 호 단위로 나눈 경우 호 번호 (아니면 [])
    page: int
    header: str
    body: str

    @property
    def text(self) -> str:
        """임베딩 · 프롬프트에 쓰는 텍스트."""
        return f"{self.header}\n{self.body}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["text"] = self.text
        return d


def _para_no(label: str) -> int:
    return CIRCLED.index(label) + 1


def _blocks(units: list[Unit]) -> list[list[Unit]]:
    """항 단위 묶음. 항이 없는 조(제2조 정의처럼 호만 있는 조)는 [머리 문장] + 호 하나씩."""
    if any(u.kind == "para" for u in units):
        blocks: list[list[Unit]] = []
        for u in units:
            if u.kind == "para" or not blocks:
                blocks.append([u])
            else:
                blocks[-1].append(u)
        return blocks
    return _item_blocks(units)


def _item_blocks(units: list[Unit]) -> list[list[Unit]]:
    """첫 단위(머리 문장 또는 항 첫 문장) 뒤의 호(+목)를 하나씩 묶는다. 머리 문장은 첫 묶음에 둔다."""
    blocks: list[list[Unit]] = [[units[0]]]
    for u in units[1:]:
        if u.kind == "item" or len(blocks) == 1:
            blocks.append([u])
        else:
            blocks[-1].append(u)
    return blocks


def _size(units: list[Unit]) -> int:
    return sum(len(u.text) + 1 for u in units)


def _pack(blocks: list[list[Unit]], limit: int) -> list[list[list[Unit]]]:
    """연속한 묶음을 limit 이하로 채운다 (묶음 하나가 limit보다 크면 그 묶음만 따로)."""
    groups: list[list[list[Unit]]] = []
    size = 0
    for b in blocks:
        s = _size(b)
        if groups and size + s <= limit:
            groups[-1].append(b)
            size += s
        else:
            groups.append([b])
            size = s
    return groups


def _header(meta: LawMeta, a: Article, addenda_title: str, paras: list[int], items: list[str]) -> str:
    where = addenda_title if a.part == "부칙" else " ".join(x for x in (a.chapter, a.section) if x)
    loc = f"{a.label}({a.title})"
    if paras:
        loc += f" 제{paras[0]}항" if len(paras) == 1 else f" 제{paras[0]}항~제{paras[-1]}항"
    if items:
        loc += f" 제{items[0]}호" if len(items) == 1 else f" 제{items[0]}호~제{items[-1]}호"
    head = f"[{meta.name} {where} {loc}]"
    if a.status == "시행예정":
        scope = f"({a.effective_note})" if a.effective_note and a.effective_note != a.label else ""
        head += f" · {a.effective_from} 시행 예정{scope}"
    return head


def chunk_article(meta: LawMeta, a: Article, addenda_title: str = "", max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    if a.status == "삭제":
        return []

    # (항 번호 목록, 호 번호 목록, 단위 목록)
    pieces: list[tuple[list[int], list[str], list[Unit]]] = []
    if _size(a.units) <= max_chars:
        pieces.append(([], [], a.units))
    else:
        has_para = any(u.kind == "para" for u in a.units)
        for group in _pack(_blocks(a.units), max_chars):
            units = [u for b in group for u in b]
            paras = [_para_no(b[0].label) for b in group if b[0].kind == "para"]
            if _size(units) <= max_chars or len(group) > 1:
                pieces.append((paras if has_para else [], [], units))
                continue
            # 항(또는 머리 문장 + 호) 하나가 너무 길다 → 호 묶음으로 나누고 첫 문장을 반복
            lead, *rest = _item_blocks(units)
            for sub in _pack(rest, max_chars - _size(lead)):
                sub_units = [u for b in sub for u in b]
                items = [b[0].label for b in sub if b[0].kind == "item"]
                pieces.append((paras, items, lead + sub_units))

    part_code = "B" if a.part == "부칙" else "L"
    status_code = "F" if a.status == "시행예정" else "C"
    key = a.key.replace("의", "_")
    chunks = []
    for n, (paras, items, units) in enumerate(pieces, start=1):
        chunks.append(Chunk(
            chunk_id=f"{meta.law_no}-{part_code}{key}-{status_code}-{n:02d}",
            law_name=meta.name, law_no=meta.law_no, part=a.part,
            article_key=a.key, article_label=a.label, title=a.title,
            chapter=a.chapter, section=a.section, status=a.status,
            effective_from=a.effective_from, effective_note=a.effective_note,
            paragraphs=paras, items=items, page=a.page,
            header=_header(meta, a, addenda_title, paras, items),
            body="\n".join(u.text for u in units),
        ))
    return chunks


def chunk_law(law: ParsedLaw, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    chunks = [c for a in law.articles for c in chunk_article(law.meta, a, law.addenda_title, max_chars)]
    ids = [c.chunk_id for c in chunks]
    if len(ids) != len(set(ids)):
        raise ValueError("chunk_id가 중복됩니다")
    return chunks
