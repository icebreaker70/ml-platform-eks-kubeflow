import os
import sys

import pytest
import yaml

from lawrag import steps
from lawrag.chunk import Chunk
from lawrag.evaluate import load_qa
from lawrag.pdfparse import Article, Unit

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
PDF = os.path.join(ROOT, "docs", "개인정보 보호법(법률)(제21445호)(20260911).pdf")


def test_article_and_chunk_roundtrip():
    a = Article(part="본문", number=24, branch=2, title="t", chapter="c", section="", status="현행",
                effective_from="2026-09-11", effective_note="", page=3,
                units=[Unit("para", "①", "① 문장")], history=["<개정 2020. 2. 4.>"])
    assert Article.from_dict(a.to_dict()) == a
    c = Chunk("id", "법", "1", "본문", "24의2", "제24조의2", "t", "c", "", "현행", "2026-09-11", "", [1], [], 3, "[h]", "b")
    assert Chunk.from_dict(c.to_dict()) == c


def test_dsn_from_password(monkeypatch):
    monkeypatch.delenv("RAGDB_DSN", raising=False)
    monkeypatch.setenv("RAGDB_PASSWORD", "pw")
    assert steps.dsn("h") == "postgresql://rag:pw@h:5432/rag"
    monkeypatch.delenv("RAGDB_PASSWORD")
    with pytest.raises(RuntimeError):
        steps.dsn()


@pytest.mark.skipif(not os.path.exists(PDF), reason="법률 PDF가 없습니다")
def test_parse_then_chunk_through_artifact_dirs(tmp_path):
    """parse-pdf → chunk를 디렉터리로 넘겨도 로컬 직접 실행과 같은 결과 (143청크)."""
    pytest.importorskip("pypdf")
    info, md = steps.parse(PDF, str(tmp_path / "law"))
    assert info["meta"]["law_no"] == "21445" and info["stats"]["articles_future"] == 9
    assert "시행 예정" in md and "제28조의12(2027-03-09)" in md
    n, md2 = steps.chunk(str(tmp_path / "law"), str(tmp_path / "chunks"))
    assert n == 143 and "제75조" in md2
    meta, chunks = steps.load_chunks(str(tmp_path / "chunks"))
    assert meta.effective == "2026-09-11" and len(chunks) == 143


def test_eval_generation_with_fake_rag_api():
    def post(question):
        return {"answer": "제24조의2에 따라 만 14세 미만 … 법정대리인의 동의", "refused": False, "skipped_llm": False,
                "citations": [{"key": "24의2", "status": "retrieved"}], "timings": {"generate_s": 1.0}, "model": "fake"}

    m, md = steps.eval_generation("v", "http://unused", "token", db=None, post=post)
    assert m["model"] == "fake" and m["citation_accuracy"] == 1.0
    assert m["false_refusal_rate"] == 0.0 and m["refusal_accuracy"] == 0.0   # 답 없는 질문도 답했으므로
    assert md.count("## ") == len(load_qa())


def test_pipeline_compiles_with_expected_dag(tmp_path, monkeypatch):
    pytest.importorskip("kfp")
    sys.path.insert(0, os.path.join(ROOT, "pipelines"))
    from kfp import compiler

    from rag_index_pipeline import rag_index_pipeline

    out = tmp_path / "p.yaml"
    compiler.Compiler().compile(rag_index_pipeline, str(out))
    spec, plat = list(yaml.safe_load_all(out.read_text()))
    names = set()

    def walk(dag):
        for name, t in dag["tasks"].items():
            names.add(name)
            comp = spec["components"][t["componentRef"]["name"]]
            if "dag" in comp:
                walk(comp["dag"])

    walk(spec["root"]["dag"])
    assert {"parse-pdf", "chunk", "embed", "load-index", "eval-retrieval", "eval-generation",
            "promote", "promote-2"} <= names
    tasks = spec["root"]["dag"]["tasks"]
    assert tasks["embed"]["cachingOptions"].get("enableCache") is True
    assert not tasks["load-index"]["cachingOptions"].get("enableCache")
    execs = plat["platforms"]["kubernetes"]["deploymentSpec"]["executors"]
    assert "secretAsEnv" in execs["exec-load-index"] and "secretAsEnv" not in execs["exec-embed"]


# ── 로컬 pgvector 통합 (RAGDB_TEST=1 + make rag-db-local, bge-m3 첫 실행 시 내려받음) ─────────────
@pytest.mark.skipif(not os.environ.get("RAGDB_TEST"), reason="RAGDB_TEST=1 + make rag-db-local 필요")
def test_steps_end_to_end_local(tmp_path):
    from lawrag import store

    db = os.environ.get("RAGDB_DSN", store.DEFAULT_DSN)
    steps.parse(PDF, str(tmp_path / "law"))
    steps.chunk(str(tmp_path / "law"), str(tmp_path / "chunks"))
    info = steps.embed(str(tmp_path / "chunks"), "BAAI/bge-m3", str(tmp_path / "emb"))
    assert info["count"] == 143 and info["dim"] == 1024
    conn = store.connect(db)
    active = store.get_active(conn)
    version = steps.load_index(str(tmp_path / "chunks"), str(tmp_path / "emb"), "pytest00", db)
    try:
        assert store.get_active(conn) == active            # 적재만으로는 승격되지 않는다
        m, _ = steps.eval_retrieval(version, "BAAI/bge-m3", db)
        assert m["recall_at_5"] >= 0.8
        assert steps.promote(version, db) == (active or "")
        assert store.get_active(conn) == version
    finally:
        if active:
            store.promote(conn, active)                      # 원래 서비스 중 색인으로 되돌린다
        conn.execute("DELETE FROM index_versions WHERE index_version = %s", (version,))


def test_mlflow_log_reuses_run_per_index_version(tmp_path):
    mlflow = pytest.importorskip("mlflow")
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    r1 = steps.mlflow_log(uri, "21445-20260911-abc", {"pdf_sha256": "x"}, {"chunks": 143})
    r2 = steps.mlflow_log(uri, "21445-20260911-abc", {}, {"retrieval_recall_at_5": 0.97, "flag": True})
    r3 = steps.mlflow_log(uri, "21445-20260911-def", {}, {})
    assert r1 == r2 != r3
    run = mlflow.MlflowClient(uri).get_run(r1)
    assert run.data.metrics == {"chunks": 143.0, "retrieval_recall_at_5": 0.97}   # bool은 지표로 남기지 않는다
    assert run.data.params == {"pdf_sha256": "x"} and run.data.tags["index_version"] == "21445-20260911-abc"
