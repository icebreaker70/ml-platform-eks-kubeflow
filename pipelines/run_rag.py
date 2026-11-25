"""RAG 색인 파이프라인 컴파일 + 실행 제출.

  python pipelines/run_rag.py --compile-only
  python pipelines/run_rag.py --host http://<ALB>/pipeline [--no-generation] [--min-recall 0.8]

PDF 위치는 make rag-data가 남긴 .build/rag-pdf-key(또는 RAG_PDF_KEY), 버킷은 BUCKET (scripts/env.sh).
생성 평가(기본 켬)는 law-llm이 떠 있어야 한다 — 없으면 경고 후 --no-generation을 권한다.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from run import m2m_token  # noqa: E402


def llm_ready(namespace: str) -> bool:
    try:
        out = subprocess.run(["kubectl", "-n", namespace, "get", "isvc", "law-llm",
                              "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}"],
                             capture_output=True, text=True, timeout=20).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return out.strip() == "True"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("KFP_HOST"), help="예: http://<ALB DNS>/pipeline")
    ap.add_argument("--namespace", default=os.environ.get("KF_PROFILE_NS", "kubeflow-user-example-com"))
    ap.add_argument("--experiment", default="law-rag-index")
    ap.add_argument("--compile-only", action="store_true")
    ap.add_argument("--output", default=os.path.join(HERE, "rag_index_pipeline.yaml"))
    ap.add_argument("--pdf-key", default=os.environ.get("RAG_PDF_KEY"))
    ap.add_argument("--max-chars", type=int, default=1500)
    ap.add_argument("--min-recall", type=float, default=0.8)
    ap.add_argument("--min-citation-accuracy", type=float, default=0.9)
    ap.add_argument("--no-generation", action="store_true", help="생성 평가 없이 검색 평가만으로 승격")
    ap.add_argument("--force", action="store_true", help="law-llm이 Ready가 아니어도 생성 평가를 켠 채 제출")
    args = ap.parse_args()

    if not os.environ.get("IMAGE_RAG"):
        print("[warn] IMAGE_RAG가 비어 있습니다 (scripts/env.sh를 source 하세요)", file=sys.stderr)

    from kfp import compiler

    from rag_index_pipeline import rag_index_pipeline

    compiler.Compiler().compile(rag_index_pipeline, args.output)
    print(f"compiled → {args.output}")
    if args.compile_only:
        return

    import kfp

    if not args.host:
        sys.exit("--host 또는 KFP_HOST가 필요합니다")
    bucket = os.environ.get("BUCKET") or sys.exit("BUCKET이 비어 있습니다 (scripts/env.sh)")
    key = args.pdf_key
    if not key:
        try:
            key = open(os.path.join(ROOT, ".build", "rag-pdf-key")).read().strip()
        except OSError:
            sys.exit("PDF 위치를 모릅니다 — make rag-data 먼저 (또는 RAG_PDF_KEY=datasets/law/…/source.pdf)")
    gen = not args.no_generation
    if gen and not llm_ready(args.namespace) and not args.force:
        sys.exit("law-llm이 Ready가 아닙니다 — make llm 후 다시 실행하거나, 검색 평가만 하려면 NO_GEN=1 (--no-generation)")
    embed_slug = os.environ.get("EMBED_MODEL", "BAAI/bge-m3").rsplit("/", 1)[-1].lower()

    client = kfp.Client(host=args.host, existing_token=m2m_token(args.namespace), namespace=args.namespace)
    run = client.create_run_from_pipeline_package(
        args.output,
        arguments={"pdf_uri": f"s3://{bucket}/{key}", "embed_model_uri": f"s3://{bucket}/models/{embed_slug}",
                   "max_chars": args.max_chars, "min_recall": args.min_recall,
                   "eval_generation_enabled": gen, "min_citation_accuracy": args.min_citation_accuracy},
        experiment_name=args.experiment, namespace=args.namespace,
        run_name=f"law-rag-index-{key.split('/')[2]}{'' if gen else '-retrieval-only'}",
    )
    print(f"run id: {run.run_id}")
    print(f"UI: {args.host.rsplit('/pipeline', 1)[0]}/_/pipeline/#/runs/details/{run.run_id}")


if __name__ == "__main__":
    main()
