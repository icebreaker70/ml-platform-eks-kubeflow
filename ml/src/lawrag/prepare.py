"""PDF → 조문 · 청크 JSONL + 파싱 리포트 (로컬 확인용, 파이프라인의 parse-pdf · chunk 단계와 같은 코드).

    PYTHONPATH=ml/src python -m lawrag.prepare --pdf "docs/개인정보 보호법(법률)(제21445호)(20260911).pdf" --out data/law
"""

from __future__ import annotations

import argparse
import json
import os
import statistics

from .chunk import DEFAULT_MAX_CHARS, Chunk, chunk_law
from .pdfparse import ParsedLaw, parse_pdf


def _span(c: Chunk) -> str:
    """청크가 담은 범위: "제2항 제1호~제14의4호"."""
    p = c.paragraphs
    out = "" if not p else f"제{p[0]}항" if len(p) == 1 else f"제{p[0]}항~제{p[-1]}항"
    if c.items:
        i = c.items
        out += (" " if out else "") + (f"제{i[0]}호" if len(i) == 1 else f"제{i[0]}호~제{i[-1]}호")
    return out or "전체"


def report_markdown(law: ParsedLaw, chunks: list[Chunk]) -> str:
    m, st = law.meta, law.stats()
    lens = [len(c.text) for c in chunks]
    lines = [
        f"# 파싱 리포트 — {m.name} (법률 제{m.law_no}호)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| 시행일 · 공포일 | {m.effective} · {m.promulgated} ({m.revision}) |",
        f"| 쪽 수 · SHA-256 | {m.pages} · `{m.sha256[:16]}…` |",
        f"| 본문 조 | {st['articles_body']} (현행 {st['articles_current']}, 시행예정 {st['articles_future']}, "
        f"삭제 {st['articles_deleted']}) |",
        f"| 부칙 조 | {st['articles_addenda']} ({law.addenda_title}) |",
        f"| 제거한 머리말 · 서문 줄 | {st['removed_header_lines']} |",
        f"| 청크 | {len(chunks)} (현행 {sum(c.status == '현행' for c in chunks)}, "
        f"시행예정 {sum(c.status == '시행예정' for c in chunks)}) |",
        f"| 청크 길이(자) | 최소 {min(lens)} · 중앙값 {int(statistics.median(lens))} · 최대 {max(lens)} |",
        "",
        "## 시행 예정 조문",
        "",
        "| 조 | 제목 | 시행일 | 범위 |",
        "|---|---|---|---|",
    ]
    for a in law.articles:
        if a.status == "시행예정":
            lines.append(f"| {a.label} | {a.title} | {a.effective_from} | {a.effective_note} |")
    lines += ["", "## 삭제된 조", ""]
    lines += [f"- {a.label} ({a.chapter})" for a in law.articles if a.status == "삭제"] or ["- 없음"]
    split: dict[tuple, list[Chunk]] = {}
    for c in chunks:
        split.setdefault((c.part, c.article_label, c.status), []).append(c)
    lines += ["", "## 여러 청크로 나눈 조", ""]
    for (part, label, status), cs in split.items():
        if len(cs) > 1:
            lines.append(f"- {part} {label} ({status}): {len(cs)}개 — " + ", ".join(_span(c) for c in cs))
    return "\n".join(lines) + "\n"


def write_jsonl(path: str, rows) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pdf", required=True)
    p.add_argument("--out", default="data/law")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    args = p.parse_args(argv)

    law = parse_pdf(args.pdf)
    chunks = chunk_law(law, args.max_chars)
    os.makedirs(args.out, exist_ok=True)
    write_jsonl(os.path.join(args.out, "articles.jsonl"), (a.to_dict() for a in law.articles))
    write_jsonl(os.path.join(args.out, "chunks.jsonl"), (c.to_dict() for c in chunks))
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(law.meta.__dict__, f, ensure_ascii=False, indent=2)
    report = report_markdown(law, chunks)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    print(f"→ {args.out}/articles.jsonl, chunks.jsonl, meta.json, report.md")


if __name__ == "__main__":
    main()
