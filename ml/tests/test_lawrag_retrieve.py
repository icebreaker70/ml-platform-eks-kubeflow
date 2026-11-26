import os

import numpy as np
import pytest

from lawrag.chunk import Chunk
from lawrag.retrieve import article_refs, retrieve
from lawrag.store import Hit


def _chunk(cid, key, part="본문", status="현행"):
    return Chunk(chunk_id=cid, law_name="법", law_no="1", part=part, article_key=key, article_label=f"제{key}조",
                 title="t", chapter="", section="", status=status, effective_from="2026-09-11", effective_note="",
                 paragraphs=[], items=[], page=1, header=f"[{cid}]", body="b")


class FakeBackend:
    def __init__(self, articles, ranked):
        self.articles, self.ranked, self.calls = articles, ranked, []

    def search(self, index_version, qvec, k):
        self.calls.append(k)
        return [Hit(c, 0.9 - i * 0.1, "vector") for i, c in enumerate(self.ranked[:k])]

    def get_article(self, index_version, part, key):
        return self.articles.get((part, key), [])


def test_article_refs():
    assert article_refs("제24조의2와 제 15 조, 부칙 제1조, 다시 제24조 의 2") == \
        [("본문", "24의2"), ("본문", "15"), ("부칙", "1")]
    assert article_refs("주민등록번호를 수집해도 되나요?") == []


def test_direct_article_first_then_vector_without_duplicates():
    a = _chunk("A", "24의2")
    b, c, d = _chunk("B", "15"), _chunk("C", "16"), _chunk("D", "22")
    be = FakeBackend({("본문", "24의2"): [a]}, ranked=[a, b, c, d])
    hits = retrieve("제24조의2 알려줘", lambda q: np.zeros(3), be, "v", k=3)
    assert [(h.chunk.chunk_id, h.source) for h in hits] == [("A", "article"), ("B", "vector"), ("C", "vector")]
    assert be.calls == [4]  # 직접 매칭 1개만큼 더 받아 중복을 빼고 채운다


def test_direct_article_chunk_limit():
    parts = [_chunk(f"P{i}", "75") for i in range(5)]
    be = FakeBackend({("본문", "75"): parts}, ranked=[_chunk("X", "1")])
    hits = retrieve("제75조", lambda q: np.zeros(3), be, "v", k=5, max_article_chunks=3)
    assert [h.chunk.chunk_id for h in hits] == ["P0", "P1", "P2", "X"]


# ── 로컬 pgvector 통합 (make rag-db-local 후 RAGDB_TEST=1) ─────────────────
@pytest.mark.skipif(not os.environ.get("RAGDB_TEST"), reason="RAGDB_TEST=1 + make rag-db-local 필요")
def test_store_roundtrip():
    from lawrag import store
    from lawrag.pdfparse import LawMeta

    conn = store.connect()
    store.init_schema(conn)
    meta = LawMeta("법", "1", "2026-09-11", "2026-03-10", "일부개정", 1, "sha")
    chunks = [_chunk("T1", "1"), _chunk("T2", "2"), _chunk("T3", "2", status="시행예정")]
    vecs = np.eye(3, 1024, dtype=np.float32)
    version = "test-roundtrip"
    active = store.get_active(conn)
    try:
        store.load_index(conn, version, meta, "fake", chunks, vecs)
        hits = store.search(conn, version, vecs[1], k=2)
        assert hits[0].chunk.chunk_id == "T2" and hits[0].score == pytest.approx(1.0)
        assert [c.chunk_id for c in store.get_article(conn, version, "본문", "2")] == ["T2", "T3"]  # 현행 먼저
        store.set_metrics(conn, version, {"recall_at_5": 0.9})
        assert next(v for v in store.list_versions(conn) if v["index_version"] == version)["metrics"] == \
            {"recall_at_5": 0.9}
    finally:
        conn.execute("DELETE FROM index_versions WHERE index_version = %s", (version,))
        assert store.get_active(conn) == active
