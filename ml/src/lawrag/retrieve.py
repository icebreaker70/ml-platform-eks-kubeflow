"""질문 → 근거 청크 top-k.

1. 질문에 조문 번호("제24조의2", "부칙 제1조")가 있으면 그 조의 청크를 먼저 넣는다 (현행 · 시행예정 모두).
2. 나머지는 bge-m3 벡터 유사도 순으로 채운다 (이미 넣은 청크는 건너뜀).
"""

from __future__ import annotations

import re
from typing import Protocol

import numpy as np

from .chunk import Chunk
from .store import Hit

ARTICLE_REF = re.compile(r"(부칙\s*)?제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")


def article_refs(question: str) -> list[tuple[str, str]]:
    """질문 속 조문 번호 → [(part, article_key)], 순서 유지 · 중복 제거."""
    out: list[tuple[str, str]] = []
    for m in ARTICLE_REF.finditer(question):
        ref = ("부칙" if m.group(1) else "본문", m.group(2) + (f"의{m.group(3)}" if m.group(3) else ""))
        if ref not in out:
            out.append(ref)
    return out


class Backend(Protocol):
    def search(self, index_version: str, qvec: np.ndarray, k: int) -> list[Hit]: ...
    def get_article(self, index_version: str, part: str, article_key: str) -> list[Chunk]: ...


class PgBackend:
    def __init__(self, conn):
        self.conn = conn

    def search(self, index_version, qvec, k):
        from . import store
        return store.search(self.conn, index_version, qvec, k)

    def get_article(self, index_version, part, article_key):
        from . import store
        return store.get_article(self.conn, index_version, part, article_key)


def retrieve(question: str, embed_query, backend: Backend, index_version: str, k: int = 5,
             max_article_chunks: int = 3) -> list[Hit]:
    """embed_query: str → 정규화된 벡터. 조문 직접 매칭은 조 하나당 최대 max_article_chunks개."""
    hits: list[Hit] = []
    seen: set[str] = set()
    for part, key in article_refs(question):
        for c in backend.get_article(index_version, part, key)[:max_article_chunks]:
            if c.chunk_id not in seen and len(hits) < k:
                hits.append(Hit(c, 1.0, "article"))
                seen.add(c.chunk_id)
    if len(hits) < k:
        # 직접 매칭으로 채운 만큼 더 받아서 중복을 빼고 채운다
        for h in backend.search(index_version, embed_query(question), k + len(hits)):
            if h.chunk.chunk_id not in seen and len(hits) < k:
                hits.append(h)
                seen.add(h.chunk.chunk_id)
    return hits
