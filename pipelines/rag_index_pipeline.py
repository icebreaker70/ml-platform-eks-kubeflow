"""개인정보 보호법 RAG 색인 파이프라인 (KFP v2, 6주차).

parse-pdf → chunk → embed(CPU) → load-index → eval-retrieval ─┬─ Recall@5 ≥ 기준 ─┬─ (eval_generation) eval-generation ─┬─ 기준 통과 → promote
 S3 PDF      조/항    bge-m3        새 index_version             │                    │                                    └─ 미달: 종료
                                                                 │                    └─ (생성 평가 끔) ────────────────────→ promote
                                                                 └─ 미달: 종료 (서비스 중 색인 유지)

- 컴포넌트는 lawrag.steps를 부른다. 이미지는 rag 하나(IMAGE_RAG, CPU torch).
- parse-pdf · chunk · embed는 입력이 같으면 KFP 캐시를 쓴다 (같은 PDF로 다시 돌리면 임베딩을 건너뜀).
- load-index 이후는 캐시하지 않는다 (DB 상태를 바꾸므로).
- 생성 평가는 게이트웨이 경유로 rag-api를 index_version 지정으로 부른다 → law-llm이 떠 있어야 한다(make llm).
"""

import os

from kfp import dsl, kubernetes
from kfp.dsl import Dataset, Input, Markdown, Metrics, Output

IMAGE_RAG = os.environ.get("IMAGE_RAG", "rag:local")


@dsl.component(base_image=IMAGE_RAG, install_kfp_package=False)
def parse_pdf(pdf_uri: str, law: Output[Dataset], report: Output[Markdown]) -> str:
    """S3의 법률 PDF → 조문 JSONL. 양식 검사(조 번호 연속성, 중복 = 시행 예정 표시) 실패 시 중단. 반환: PDF SHA-256."""
    from lawrag import steps

    info, md = steps.parse(pdf_uri, law.path)
    law.metadata.update({k: info["meta"][k] for k in ("name", "law_no", "effective", "sha256")})
    law.metadata.update(info["stats"])
    with open(report.path, "w", encoding="utf-8") as f:
        f.write(md)
    return info["meta"]["sha256"]


@dsl.component(base_image=IMAGE_RAG, install_kfp_package=False)
def chunk(law: Input[Dataset], max_chars: int, chunks: Output[Dataset], report: Output[Markdown]) -> int:
    """조 단위 → 길면 항 → 호 단위 청크. 현행과 같은 시행 예정 청크는 뺀다. 반환: 청크 수."""
    from lawrag import steps

    n, md = steps.chunk(law.path, chunks.path, max_chars)
    chunks.metadata.update({"chunks": n, "law_no": law.metadata.get("law_no", ""), "max_chars": max_chars})
    with open(report.path, "w", encoding="utf-8") as f:
        f.write(md)
    return n


@dsl.component(base_image=IMAGE_RAG, install_kfp_package=False)
def embed(chunks: Input[Dataset], embed_model_uri: str, embeddings: Output[Dataset]) -> int:
    """bge-m3(S3 캐시) CPU 임베딩. 반환: 벡터 수."""
    from lawrag import steps

    info = steps.embed(chunks.path, embed_model_uri, embeddings.path)
    embeddings.metadata.update(info)
    return info["count"]


@dsl.component(base_image=IMAGE_RAG, install_kfp_package=False)
def load_index(chunks: Input[Dataset], embeddings: Input[Dataset], suffix: str, ragdb_host: str,
               mlflow_uri: str, pdf_sha256: str) -> str:
    """ragdb에 새 색인 버전 적재 (서비스 중 색인은 그대로). 반환: index_version."""
    from lawrag import steps

    version = steps.load_index(chunks.path, embeddings.path, suffix[:8], steps.dsn(ragdb_host))
    steps.mlflow_log(mlflow_uri, version, {
        "pdf_sha256": pdf_sha256, "law_no": chunks.metadata.get("law_no", ""), "chunks": embeddings.metadata["count"],
        "embed_model": embeddings.metadata["model"], "max_chars": chunks.metadata.get("max_chars", ""),
    }, {"chunks": embeddings.metadata["count"], "embed_s": embeddings.metadata["embed_s"]})
    return version


@dsl.component(base_image=IMAGE_RAG, install_kfp_package=False)
def eval_retrieval(index_version: str, embed_model_uri: str, ragdb_host: str, mlflow_uri: str,
                   min_recall: float, metrics: Output[Metrics], report: Output[Markdown]) -> bool:
    """평가 세트(40문항)로 검색 평가. 반환: Recall@5 ≥ min_recall."""
    from lawrag import steps

    m, md = steps.eval_retrieval(index_version, embed_model_uri, steps.dsn(ragdb_host))
    for k, v in m.items():
        if isinstance(v, (int, float)):
            metrics.log_metric(k, v)
    with open(report.path, "w", encoding="utf-8") as f:
        f.write(md)
    steps.mlflow_log(mlflow_uri, index_version, {}, {f"retrieval_{k}": v for k, v in m.items()})
    ok = m["recall_at_5"] >= min_recall
    print(f"[eval-retrieval] recall@5 {m['recall_at_5']} (기준 ≥ {min_recall}) → {'통과' if ok else '미달'}")
    return ok


@dsl.component(base_image=IMAGE_RAG, install_kfp_package=False)
def eval_generation(index_version: str, rag_api_url: str, ragdb_host: str, mlflow_uri: str,
                    min_citation_accuracy: float, metrics: Output[Metrics], report: Output[Markdown]) -> bool:
    """rag-api를 index_version 지정으로 불러 생성 평가. 반환: 인용 정확도 ≥ 기준 (거절 정확도 등은 기록만).

    인용 정확도는 필요조건일 뿐이다 — 답 내용은 리포트를 사람이 읽고 핵심 표현 포함률을 함께 본다 (PLAN.md 10.6).
    """
    from pathlib import Path

    from lawrag import steps

    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
    m, md = steps.eval_generation(index_version, rag_api_url, token, steps.dsn(ragdb_host))
    for k, v in m.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            metrics.log_metric(k, v)
    with open(report.path, "w", encoding="utf-8") as f:
        f.write(md)
    steps.mlflow_log(mlflow_uri, index_version, {"llm_model": m["model"]}, {f"generation_{k}": v for k, v in m.items()})
    acc = m["citation_accuracy"] or 0.0
    ok = acc >= min_citation_accuracy
    print(f"[eval-generation] 인용 정확도 {acc} (기준 ≥ {min_citation_accuracy}), "
          f"핵심 표현 {m['points_coverage']}, 거절 {m['refusal_accuracy']} → {'통과' if ok else '미달'}")
    return ok


@dsl.component(base_image=IMAGE_RAG, install_kfp_package=False)
def promote(index_version: str, ragdb_host: str, mlflow_uri: str) -> str:
    """active_index 교체 → rag-api가 다음 질의부터 새 색인을 쓴다. 반환: 이전 색인 버전."""
    import mlflow

    from lawrag import steps

    before = steps.promote(index_version, steps.dsn(ragdb_host))
    steps.mlflow_log(mlflow_uri, index_version, {}, {})
    mlflow.set_tracking_uri(mlflow_uri)
    run = mlflow.search_runs(experiment_names=["law-rag-index"], filter_string=f"tags.index_version = '{index_version}'",
                             output_format="list", max_results=1)[0]
    mlflow.MlflowClient().set_tag(run.info.run_id, "promoted", "true")
    mlflow.MlflowClient().set_tag(run.info.run_id, "replaced", before or "(없음)")
    return before


def _pod(task, cpu: str = "500m", mem: str = "1Gi", mem_limit: str = "3Gi", db: bool = False, batch: bool = True):
    task.set_cpu_request(cpu).set_memory_request(mem).set_memory_limit(mem_limit)
    kubernetes.add_pod_annotation(task, "sidecar.istio.io/inject", "false")
    kubernetes.set_image_pull_policy(task, "Always")
    if batch:   # 일반 노드에 자리가 없으면 임시 CPU 노드(burst)로
        kubernetes.add_toleration(task, key="workload", operator="Equal", value="batch", effect="NoSchedule")
    if db:      # ragdb 비밀번호 (platform/rag/install.sh가 프로필 네임스페이스에 만든 Secret)
        kubernetes.use_secret_as_env(task, secret_name="ragdb", secret_key_to_env={"password": "RAGDB_PASSWORD"})
    return task


@dsl.pipeline(name="law-rag-index", description="개인정보 보호법 PDF → 조문 청크 → bge-m3 → pgvector → 평가 → 승격")
def rag_index_pipeline(
    pdf_uri: str,
    embed_model_uri: str,
    max_chars: int = 1500,
    min_recall: float = 0.8,
    eval_generation_enabled: bool = True,
    min_citation_accuracy: float = 0.9,
    ragdb_host: str = "ragdb.rag.svc.cluster.local",
    rag_api_url: str = "http://istio-ingressgateway.istio-system.svc.cluster.local",
    mlflow_uri: str = "http://mlflow.mlflow.svc.cluster.local:5000/mlflow",
):
    p = _pod(parse_pdf(pdf_uri=pdf_uri))
    p.set_display_name("parse-pdf").set_caching_options(True)

    c = _pod(chunk(law=p.outputs["law"], max_chars=max_chars))
    c.set_display_name("chunk").set_caching_options(True)

    e = _pod(embed(chunks=c.outputs["chunks"], embed_model_uri=embed_model_uri), cpu="2", mem="4Gi", mem_limit="6Gi")
    e.set_display_name("embed").set_caching_options(True)

    ld = _pod(load_index(chunks=c.outputs["chunks"], embeddings=e.outputs["embeddings"],
                         suffix=dsl.PIPELINE_JOB_ID_PLACEHOLDER, ragdb_host=ragdb_host, mlflow_uri=mlflow_uri,
                         pdf_sha256=p.outputs["Output"]), db=True)
    ld.set_display_name("load-index").set_caching_options(False)

    ev = _pod(eval_retrieval(index_version=ld.output, embed_model_uri=embed_model_uri, ragdb_host=ragdb_host,
                             mlflow_uri=mlflow_uri, min_recall=min_recall), cpu="1", mem="3Gi", mem_limit="5Gi", db=True)
    ev.set_display_name("eval-retrieval").set_caching_options(False)

    with dsl.If(ev.outputs["Output"] == True, name="retrieval-passed"):  # noqa: E712 (KFP 조건식)
        with dsl.If(eval_generation_enabled == True, name="with-generation"):  # noqa: E712
            g = _pod(eval_generation(index_version=ld.output, rag_api_url=rag_api_url, ragdb_host=ragdb_host,
                                     mlflow_uri=mlflow_uri, min_citation_accuracy=min_citation_accuracy), db=True)
            g.set_display_name("eval-generation").set_caching_options(False)
            with dsl.If(g.outputs["Output"] == True, name="generation-passed"):  # noqa: E712
                pr = _pod(promote(index_version=ld.output, ragdb_host=ragdb_host, mlflow_uri=mlflow_uri), db=True)
                pr.set_display_name("promote").set_caching_options(False)
        with dsl.If(eval_generation_enabled == False, name="retrieval-only"):  # noqa: E712
            pr2 = _pod(promote(index_version=ld.output, ragdb_host=ragdb_host, mlflow_uri=mlflow_uri), db=True)
            pr2.set_display_name("promote").set_caching_options(False)
