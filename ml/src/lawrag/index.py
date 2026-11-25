"""색인 적재 · 승격 · 검색 CLI (로컬 확인용, 파이프라인의 embed · load-index · promote 단계와 같은 코드).

    python -m lawrag.index load --pdf "docs/개인정보 보호법(…).pdf" --promote
    python -m lawrag.index search "주민등록번호를 수집해도 되나요?"
    python -m lawrag.index versions

DB 주소는 RAGDB_DSN (기본: make rag-db-local 컨테이너).
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime

from . import store
from .chunk import DEFAULT_MAX_CHARS, chunk_law
from .embed import Embedder
from .pdfparse import parse_pdf
from .retrieve import PgBackend, retrieve


def make_version(law_no: str, effective: str, suffix: str) -> str:
    """21445-20260911-<suffix>  (suffix: 파이프라인 실행 ID 앞 8자, 로컬은 시각)."""
    return f"{law_no}-{effective.replace('-', '')}-{suffix}"


def cmd_load(args) -> None:
    law = parse_pdf(args.pdf)
    chunks = chunk_law(law, args.max_chars)
    t0 = time.time()
    emb = Embedder(args.model)
    t1 = time.time()
    vecs = emb.encode([c.text for c in chunks], show_progress=True)
    t2 = time.time()
    version = make_version(law.meta.law_no, law.meta.effective, args.suffix or datetime.now().strftime("local%m%d%H%M"))
    conn = store.connect(args.dsn)
    store.init_schema(conn)
    store.load_index(conn, version, law.meta, emb.name, chunks, vecs)
    store.set_metrics(conn, version, {"embed_seconds": round(t2 - t1, 1), "model_load_seconds": round(t1 - t0, 1)})
    print(f"적재: {version} — 청크 {len(chunks)}개, 모델 로딩 {t1 - t0:.1f}초, 임베딩 {t2 - t1:.1f}초")
    if args.promote:
        store.promote(conn, version)
        print(f"승격: active_index = {version}")


def cmd_search(args) -> None:
    conn = store.connect(args.dsn)
    version = args.version or store.get_active(conn)
    if not version:
        raise SystemExit("서비스 중인 색인이 없습니다 — load --promote 먼저")
    emb = Embedder(args.model)
    t0 = time.time()
    hits = retrieve(args.question, emb.encode_query, PgBackend(conn), version, k=args.k)
    print(f"색인 {version}, {time.time() - t0:.2f}초\n")
    for i, h in enumerate(hits, 1):
        c = h.chunk
        print(f"{i}. [{h.source} {h.score:.3f}] {c.header}  (p.{c.page})")
        print("   " + c.body[:160].replace("\n", " ") + ("…" if len(c.body) > 160 else ""))


def cmd_versions(args) -> None:
    conn = store.connect(args.dsn)
    for v in store.list_versions(conn):
        mark = "*" if v["active"] else " "
        print(f"{mark} {v['index_version']}  {v['created_at']:%Y-%m-%d %H:%M}  청크 {v['chunks']}  "
              f"{v['embed_model']}  {v['metrics']}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=None)
    p.add_argument("--model", default=None, help="임베딩 모델 (기본 EMBED_MODEL 또는 BAAI/bge-m3)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("load", help="PDF → 청크 → 임베딩 → 새 색인 버전 적재")
    s.add_argument("--pdf", required=True)
    s.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    s.add_argument("--suffix", default=None)
    s.add_argument("--promote", action="store_true")
    s.set_defaults(func=cmd_load)

    s = sub.add_parser("search", help="질문 → 근거 청크 top-k")
    s.add_argument("question")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--version", default=None, help="기본: 서비스 중인 색인")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("versions", help="색인 버전 목록 (* = 서비스 중)")
    s.set_defaults(func=cmd_versions)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
