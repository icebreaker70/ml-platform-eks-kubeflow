"""파이프라인 컴파일 + Kubeflow에 실행 제출.

  python pipelines/run.py --compile-only              # idcard_pipeline.yaml 생성
  python pipelines/run.py --host http://<ALB>/pipeline  # 실행 제출 (M2M 토큰 사용)

필요한 값은 scripts/env.sh가 terraform output에서 읽어 환경 변수로 넘겨준다.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def m2m_token(namespace: str) -> str:
    """프로필 네임스페이스 default-editor SA 토큰 (EKS OIDC issuer가 서명 → Istio가 검증)."""
    return subprocess.check_output(
        ["kubectl", "-n", namespace, "create", "token", "default-editor", "--duration", "2h"], text=True
    ).strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("KFP_HOST"), help="예: http://<ALB DNS>/pipeline")
    ap.add_argument("--namespace", default=os.environ.get("KF_PROFILE_NS", "kubeflow-user-example-com"))
    ap.add_argument("--experiment", default="idcard-recognition")
    ap.add_argument("--compile-only", action="store_true")
    ap.add_argument("--output", default=os.path.join(HERE, "idcard_pipeline.yaml"))
    ap.add_argument("--dataset-version", default="v1")
    ap.add_argument("--num-train", type=int, default=3000)
    ap.add_argument("--num-val", type=int, default=500)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--map50-threshold", type=float, default=0.6)
    args = ap.parse_args()

    # 컴포넌트 import 전에 이미지 환경 변수가 설정돼 있어야 한다.
    for var in ("IMAGE_ML", "IMAGE_BUILDER"):
        if not os.environ.get(var):
            print(f"[warn] {var}가 비어 있습니다 (scripts/env.sh를 source 하세요)", file=sys.stderr)

    from kfp import compiler

    from idcard_pipeline import idcard_pipeline

    compiler.Compiler().compile(idcard_pipeline, args.output)
    print(f"compiled → {args.output}")
    if args.compile_only:
        return

    import kfp

    if not args.host:
        sys.exit("--host 또는 KFP_HOST가 필요합니다")
    client = kfp.Client(host=args.host, existing_token=m2m_token(args.namespace), namespace=args.namespace)
    params = {
        "bucket": os.environ["BUCKET"],
        "image_repo": os.environ["IMAGE_RECOGNIZER_REPO"],
        "region": os.environ.get("AWS_REGION", "ap-northeast-2"),
        "dataset_version": args.dataset_version,
        "num_train": args.num_train,
        "num_val": args.num_val,
        "epochs": args.epochs,
        "map50_threshold": args.map50_threshold,
    }
    run = client.create_run_from_pipeline_package(
        args.output, arguments=params, experiment_name=args.experiment, namespace=args.namespace,
        run_name=f"idcard-{args.dataset_version}-e{args.epochs}",
    )
    print(f"run id: {run.run_id}")
    print(f"UI: {args.host.rsplit('/pipeline', 1)[0]}/_/pipeline/#/runs/details/{run.run_id}")


if __name__ == "__main__":
    main()
