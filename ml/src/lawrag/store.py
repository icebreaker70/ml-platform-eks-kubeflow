"""pgvector 저장소.

테이블
- index_versions : 색인 버전마다 한 줄 (원본 PDF 해시, 임베딩 모델, 청크 수, 평가 지표)
- law_chunks     : (index_version, chunk_id) 청크 + embedding vector(1024)
- active_index   : 서비스 중인 색인 버전 한 줄 — 승격 = 이 값 교체

검색은 HNSW(cosine) 인덱스 하나를 모든 버전이 함께 쓰고, `WHERE index_version = …`로 거른다.
버전이 여러 개면 근사 검색 결과가 필터에 걸려 k개보다 적게 나올 수 있으므로
pgvector 0.8의 반복 스캔(`hnsw.iterative_scan = relaxed_order`)을 켠다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import numpy as np

from .chunk import Chunk
from .embed import DIM
from .pdfparse import LawMeta

DEFAULT_DSN = "postgresql://rag:rag@localhost:55432/rag"  # make rag-db-local

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS index_versions (
    index_version text PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    law_name      text NOT NULL,
    law_no        text NOT NULL,
    effective     date NOT NULL,
    pdf_sha256    text NOT NULL,
    embed_model   text NOT NULL,
    chunks        integer NOT NULL,
    metrics       jsonb NOT NULL DEFAULT '{{}}'::jsonb
);

CREATE TABLE IF NOT EXISTS law_chunks (
    index_version  text NOT NULL REFERENCES index_versions ON DELETE CASCADE,
    chunk_id       text NOT NULL,
    law_name       text NOT NULL,
    law_no         text NOT NULL,
    part           text NOT NULL,
    article_key    text NOT NULL,
    article_label  text NOT NULL,
    title          text NOT NULL,
    chapter        text NOT NULL,
    section        text NOT NULL,
    status         text NOT NULL,
    effective_from date NOT NULL,
    effective_note text NOT NULL,
    paragraphs     integer[] NOT NULL,
    items          text[] NOT NULL,
    page           integer NOT NULL,
    header         text NOT NULL,
    body           text NOT NULL,
    embedding      vector({DIM}) NOT NULL,
    PRIMARY KEY (index_version, chunk_id)
);
CREATE INDEX IF NOT EXISTS law_chunks_embedding_hnsw ON law_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS law_chunks_article ON law_chunks (index_version, part, article_key);

CREATE TABLE IF NOT EXISTS active_index (
    singleton     boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    index_version text NOT NULL REFERENCES index_versions,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
"""

_COLS = ("chunk_id law_name law_no part article_key article_label title chapter section status "
         "effective_from effective_note paragraphs items page header body").split()


@dataclass
class Hit:
    chunk: Chunk
    score: float              # 코사인 유사도 (1 - 거리). 조문 번호 직접 매칭이면 1.0
    source: str               # vector | article


def connect(dsn: str | None = None):
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(dsn or os.environ.get("RAGDB_DSN", DEFAULT_DSN), autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    register_vector(conn)
    return conn


def init_schema(conn) -> None:
    conn.execute(SCHEMA)


def _row_to_chunk(row) -> Chunk:
    d = dict(zip(_COLS, row))
    d["effective_from"] = d["effective_from"].isoformat()
    return Chunk(**d)


def load_index(conn, index_version: str, meta: LawMeta, embed_model: str,
               chunks: list[Chunk], embeddings: np.ndarray) -> None:
    """새 색인 버전을 적재한다. 같은 버전이 있으면 지우고 다시 넣는다(서비스 중인 버전이면 거부)."""
    if len(chunks) != len(embeddings):
        raise ValueError("청크 수와 임베딩 수가 다릅니다")
    if get_active(conn) == index_version:
        raise ValueError(f"{index_version}은 서비스 중인 색인이라 덮어쓸 수 없습니다")
    with conn.transaction():
        conn.execute("DELETE FROM index_versions WHERE index_version = %s", (index_version,))
        conn.execute(
            "INSERT INTO index_versions (index_version, law_name, law_no, effective, pdf_sha256, embed_model, chunks)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (index_version, meta.name, meta.law_no, meta.effective, meta.sha256, embed_model, len(chunks)),
        )
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO law_chunks (index_version, {', '.join(_COLS)}, embedding)"
                f" VALUES (%s, {', '.join(['%s'] * len(_COLS))}, %s)",
                [(index_version, *(getattr(c, k) for k in _COLS), e) for c, e in zip(chunks, embeddings)],
            )


def set_metrics(conn, index_version: str, metrics: dict) -> None:
    conn.execute("UPDATE index_versions SET metrics = metrics || %s::jsonb WHERE index_version = %s",
                 (json.dumps(metrics), index_version))


def promote(conn, index_version: str) -> None:
    conn.execute(
        "INSERT INTO active_index (singleton, index_version) VALUES (true, %s)"
        " ON CONFLICT (singleton) DO UPDATE SET index_version = EXCLUDED.index_version, updated_at = now()",
        (index_version,),
    )


def get_active(conn) -> str | None:
    row = conn.execute("SELECT index_version FROM active_index").fetchone()
    return row[0] if row else None


def list_versions(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT v.index_version, v.created_at, v.law_no, v.effective, v.embed_model, v.chunks, v.metrics,"
        " v.index_version = a.index_version FROM index_versions v LEFT JOIN active_index a ON true"
        " ORDER BY v.created_at"
    ).fetchall()
    keys = "index_version created_at law_no effective embed_model chunks metrics active".split()
    return [dict(zip(keys, r)) for r in rows]


def search(conn, index_version: str, qvec: np.ndarray, k: int = 5) -> list[Hit]:
    with conn.transaction():
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        conn.execute("SET LOCAL hnsw.ef_search = 100")
        rows = conn.execute(
            f"SELECT {', '.join(_COLS)}, 1 - (embedding <=> %s) AS score FROM law_chunks"
            " WHERE index_version = %s ORDER BY embedding <=> %s LIMIT %s",
            (qvec, index_version, qvec, k),
        ).fetchall()
    return [Hit(_row_to_chunk(r[:-1]), float(r[-1]), "vector") for r in rows]


def get_article(conn, index_version: str, part: str, article_key: str) -> list[Chunk]:
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM law_chunks WHERE index_version = %s AND part = %s AND article_key = %s"
        " ORDER BY status <> '현행', chunk_id",  # 현행 먼저, 그다음 시행예정
        (index_version, part, article_key),
    ).fetchall()
    return [_row_to_chunk(r) for r in rows]
