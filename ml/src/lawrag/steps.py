"""색인 파이프라인(pipelines/rag_index_pipeline.py) 단계별 로직.

KFP 컴포넌트는 얇게 두고 여기를 호출한다 (4주차 idcard.steps와 같은 방식). 단계 사이에는 디렉터리로 넘긴다.
    parse-pdf   PDF(s3:// 또는 로컬) → <dir>/articles.jsonl, law.json
    chunk       articles → <dir>/chunks.jsonl
    embed       chunks + bge-m3 → <dir>/embeddings.npy
    load-index  chunks + embeddings → ragdb 새 색인 버전 (서비스 중 색인은 그대로)
    eval-retrieval / eval-generation → 지표 (+ MLflow), 승격 기준 판정
    promote     active_index 교체

DB: RAGDB_DSN, 또는 RAGDB_PASSWORD(파이프라인 파드에 Secret ragdb로 주입) + ragdb_host로 만든다.
"""

from __future__ import annotations

import json
import os
import statistics
from types import SimpleNamespace

import numpy as np

from .chunk import Chunk, chunk_law
from .pdfparse import Article, LawMeta, ParsedLaw, parse_pdf


def dsn(host: str = "ragdb.rag.svc.cluster.local") -> str:
    if os.environ.get("RAGDB_DSN"):
        return os.environ["RAGDB_DSN"]
    pw = os.environ.get("RAGDB_PASSWORD")
    if not pw:
        raise RuntimeError("RAGDB_DSN 또는 RAGDB_PASSWORD가 필요합니다")
    return f"postgresql://rag:{pw}@{host}:5432/rag"


def _jsonl(path: str, rows) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ── parse-pdf ─────────────────────────────────────────────────────────────
def parse(pdf_uri: str, out_dir: str) -> tuple[dict, str]:
    """반환: (law.json 내용, 리포트 Markdown). 양식 검사에 실패하면 ParseError로 단계가 실패한다."""
    from .index import fetch_pdf

    law = parse_pdf(fetch_pdf(pdf_uri))
    os.makedirs(out_dir, exist_ok=True)
    _jsonl(os.path.join(out_dir, "articles.jsonl"), (a.to_dict() for a in law.articles))
    info = {"meta": law.meta.__dict__, "addenda_title": law.addenda_title,
            "removed_header_lines": law.removed_header_lines, "stats": law.stats(), "pdf_uri": pdf_uri}
    with open(os.path.join(out_dir, "law.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    st, m = law.stats(), law.meta
    fut = [f"{a.label}({a.effective_from})" for a in law.articles if a.status == "시행예정"]
    md = (f"# {m.name} (법률 제{m.law_no}호, {m.effective} 시행)\n\n| 항목 | 값 |\n|---|---|\n"
          f"| 쪽 · SHA-256 | {m.pages} · `{m.sha256[:16]}…` |\n"
          f"| 본문 조 | {st['articles_body']} (현행 {st['articles_current']}, 시행예정 {st['articles_future']}, "
          f"삭제 {st['articles_deleted']}) |\n| 부칙 조 | {st['articles_addenda']} |\n"
          f"| 제거한 머리말 · 서문 줄 | {st['removed_header_lines']} |\n| 시행 예정 | {', '.join(fut) or '없음'} |\n")
    return info, md


def load_law(in_dir: str) -> ParsedLaw:
    with open(os.path.join(in_dir, "law.json"), encoding="utf-8") as f:
        info = json.load(f)
    arts = [Article.from_dict(d) for d in _read_jsonl(os.path.join(in_dir, "articles.jsonl"))]
    return ParsedLaw(meta=LawMeta(**info["meta"]), articles=arts, addenda_title=info["addenda_title"],
                     removed_header_lines=info["removed_header_lines"])


# ── chunk ─────────────────────────────────────────────────────────────────
def chunk(in_dir: str, out_dir: str, max_chars: int = 1500) -> tuple[int, str]:
    law = load_law(in_dir)
    chunks = chunk_law(law, max_chars)
    os.makedirs(out_dir, exist_ok=True)
    _jsonl(os.path.join(out_dir, "chunks.jsonl"), (c.to_dict() for c in chunks))
    with open(os.path.join(out_dir, "law.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": law.meta.__dict__, "max_chars": max_chars}, f, ensure_ascii=False, indent=2)
    lens = [len(c.body) for c in chunks]
    by_status = {s: sum(c.status == s for c in chunks) for s in ("현행", "시행예정")}
    split = sorted({c.article_label for c in chunks if c.chunk_id.endswith("-02")})
    md = (f"| 항목 | 값 |\n|---|---|\n| 청크 | {len(chunks)} (현행 {by_status['현행']}, 시행예정 {by_status['시행예정']}) |\n"
          f"| 본문 길이(자) | 최소 {min(lens)} · 중앙값 {int(statistics.median(lens))} · 최대 {max(lens)} (기준 {max_chars}) |\n"
          f"| 여러 청크로 나눈 조 | {', '.join(split) or '없음'} |\n")
    return len(chunks), md


def load_chunks(in_dir: str) -> tuple[LawMeta, list[Chunk]]:
    with open(os.path.join(in_dir, "law.json"), encoding="utf-8") as f:
        meta = LawMeta(**json.load(f)["meta"])
    return meta, [Chunk.from_dict(d) for d in _read_jsonl(os.path.join(in_dir, "chunks.jsonl"))]


# ── embed ─────────────────────────────────────────────────────────────────
def fetch_embed_model(model_uri: str, local_dir: str = "/tmp/models/embed") -> str:
    """s3:// 이면 받아서 로컬 경로를, 아니면(Hugging Face 이름 · 로컬 경로) 그대로 돌려준다."""
    if model_uri.startswith("s3://"):
        from .cache import download_prefix
        return download_prefix(model_uri, local_dir)
    return model_uri


def embed(chunks_dir: str, model_uri: str, out_dir: str) -> dict:
    import time

    from .embed import Embedder

    _, chunks = load_chunks(chunks_dir)
    t0 = time.time()
    emb = Embedder(fetch_embed_model(model_uri))
    t1 = time.time()
    vecs = emb.encode([c.text for c in chunks])
    os.makedirs(out_dir, exist_ok=True)
    np.save(os.path.join(out_dir, "embeddings.npy"), vecs)
    info = {"model": model_uri, "dim": int(vecs.shape[1]), "count": int(vecs.shape[0]),
            "model_load_s": round(t1 - t0, 1), "embed_s": round(time.time() - t1, 1)}
    with open(os.path.join(out_dir, "embed.json"), "w") as f:
        json.dump(info, f)
    return info


# ── load-index · promote ──────────────────────────────────────────────────
def load_index(chunks_dir: str, emb_dir: str, suffix: str, db: str) -> str:
    from . import store
    from .index import make_version

    meta, chunks = load_chunks(chunks_dir)
    vecs = np.load(os.path.join(emb_dir, "embeddings.npy"))
    with open(os.path.join(emb_dir, "embed.json")) as f:
        info = json.load(f)
    version = make_version(meta.law_no, meta.effective, suffix)
    conn = store.connect(db)
    store.init_schema(conn)
    store.load_index(conn, version, meta, info["model"], chunks, vecs)
    store.set_metrics(conn, version, {"embed": info})
    print(f"[load-index] {version}: 청크 {len(chunks)}개 (서비스 중: {store.get_active(conn)})", flush=True)
    return version


def promote(version: str, db: str) -> str:
    from . import store

    conn = store.connect(db)
    before = store.get_active(conn)
    store.promote(conn, version)
    print(f"[promote] active_index: {before} → {version}", flush=True)
    return before or ""


# ── 평가 ──────────────────────────────────────────────────────────────────
def eval_retrieval(version: str, model_uri: str, db: str, k: int = 5) -> tuple[dict, str]:
    from . import store
    from .embed import Embedder
    from .evaluate import load_qa, metrics, report_markdown, run
    from .retrieve import PgBackend

    conn = store.connect(db)
    emb = Embedder(fetch_embed_model(model_uri))
    results = run(load_qa(), emb.encode_query, PgBackend(conn), version, k)
    m = metrics(results, k)
    store.set_metrics(conn, version, {"retrieval": m})
    return m, report_markdown(version, results, m, k)


def _answer_from_json(d: dict) -> SimpleNamespace:
    """rag-api 응답(JSON) → evaluate.gen_metrics가 읽는 속성을 가진 객체."""
    return SimpleNamespace(answer=d["answer"], refused=d["refused"], skipped_llm=d["skipped_llm"],
                           future_notice_added=d.get("future_notice_added", False), timings=d.get("timings", {}),
                           citations=[SimpleNamespace(**c) for c in d["citations"]])


def eval_generation(version: str, rag_api_url: str, token: str, db: str | None, timeout: float = 180,
                    post=None) -> tuple[dict, str]:
    """rag-api(/rag/ask)를 index_version 지정으로 불러 승격 전 색인으로 생성 평가를 한다.

    rag-api가 쓰는 LLM(law-llm)이 떠 있어야 한다 (make llm). post는 테스트용 주입.
    """
    from . import store
    from .evaluate import gen_metrics, gen_report_markdown, load_qa

    if post is None:
        import requests

        def post(q):
            for attempt in range(3):   # law-llm 일시 오류(503) 재시도
                r = requests.post(f"{rag_api_url.rstrip('/')}/rag/ask", timeout=timeout,
                                  json={"question": q, "index_version": version},
                                  headers={"Authorization": f"Bearer {token}"})
                if r.status_code not in (502, 503, 504):
                    break
            r.raise_for_status()
            return r.json()

    pairs, model = [], ""
    for q in load_qa():
        d = post(q.question)
        model = d.get("model") or model
        pairs.append((q, _answer_from_json(d)))
        print(f"[eval-generation] {q.id}: {'거절' if d['refused'] else [c['key'] for c in d['citations']]}",
              flush=True)
    m = gen_metrics(pairs)
    if db:
        store.set_metrics(store.connect(db), version, {"generation": {**m, "model": model}})
    return {**m, "model": model}, gen_report_markdown(version, model, pairs, m)


def mlflow_log(mlflow_uri: str, version: str, params: dict, metrics: dict, experiment: str = "law-rag-index") -> str:
    """색인 버전마다 MLflow 실행 하나 (run name = index_version). 같은 이름이 있으면 이어서 기록."""
    import mlflow

    mlflow.set_tracking_uri(mlflow_uri)
    exp = mlflow.set_experiment(experiment)
    found = mlflow.search_runs([exp.experiment_id], filter_string=f"tags.index_version = '{version}'",
                               output_format="list", max_results=1)
    with mlflow.start_run(run_id=found[0].info.run_id if found else None, run_name=version) as r:
        mlflow.set_tag("index_version", version)
        if params:
            mlflow.log_params({k: str(v)[:500] for k, v in params.items()})
        num = {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if num:
            mlflow.log_metrics(num)
        return r.info.run_id
