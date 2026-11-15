"""영화 추천 파이프라인 컴파일 + 실행 제출.

  python pipelines/run_movierec.py --compile-only
  python pipelines/run_movierec.py --host http://<ALB>/pipeline --epochs 20 --emb-dims 16,32,64
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from run import m2m_token  # noqa: E402  (같은 M2M 토큰 방식을 쓴다)


def running_tensorboards(namespace: str) -> list[str]:
    """프로필 네임스페이스에 떠 있는 Tensorboard 인스턴스 이름. 조회에 실패하면 빈 목록."""
    try:
        out = subprocess.run(["kubectl", "-n", namespace, "get", "tensorboards.tensorboard.kubeflow.org",
                              "-o", "jsonpath={.items[*].metadata.name}"],
                             capture_output=True, text=True, timeout=20, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return out.split()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("KFP_HOST"), help="예: http://<ALB DNS>/pipeline")
    ap.add_argument("--namespace", default=os.environ.get("KF_PROFILE_NS", "kubeflow-user-example-com"))
    ap.add_argument("--experiment", default="movie-recommendation")
    ap.add_argument("--compile-only", action="store_true")
    ap.add_argument("--output", default=os.path.join(HERE, "movierec_pipeline.yaml"))
    ap.add_argument("--emb-dims", default="16,32,64")
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--lr", type=float, default=0.005)
    ap.add_argument("--tb-pvc", default="movierec-tb")
    ap.add_argument("--force", action="store_true", help="Tensorboard 인스턴스가 떠 있어도 제출한다")
    args = ap.parse_args()

    if not os.environ.get("IMAGE_ML"):
        print("[warn] IMAGE_ML이 비어 있습니다 (scripts/env.sh를 source 하세요)", file=sys.stderr)

    from kfp import compiler

    from movierec_pipeline import movierec_pipeline

    compiler.Compiler().compile(movierec_pipeline, args.output)
    print(f"compiled → {args.output}")
    if args.compile_only:
        return

    import kfp

    if not args.host:
        sys.exit("--host 또는 KFP_HOST가 필요합니다")
    # 로그 PVC는 ReadWriteOnce(EBS)다. Tensorboard가 이미 PVC를 붙잡고 있으면 학습 파드가 다른 노드에
    # 배치됐을 때 볼륨을 붙이지 못해 ContainerCreating에서 멈춘다(Multi-Attach). 미리 막는다.
    tbs = running_tensorboards(args.namespace)
    if tbs and not args.force:
        sys.exit(f"Tensorboard 인스턴스가 떠 있습니다: {', '.join(tbs)}\n"
                 "로그 PVC가 ReadWriteOnce라 학습 파드가 볼륨을 붙이지 못할 수 있습니다.\n"
                 "먼저 `make tb-delete`로 인스턴스를 지우고(로그는 PVC에 남습니다) 다시 실행하세요. "
                 "그대로 진행하려면 FORCE=1 (--force).")
    client = kfp.Client(host=args.host, existing_token=m2m_token(args.namespace), namespace=args.namespace)
    dims = [int(x) for x in args.emb_dims.split(",")]
    run = client.create_run_from_pipeline_package(
        args.output,
        arguments={"emb_dims": dims, "epochs": args.epochs, "lr": args.lr, "tb_pvc": args.tb_pvc},
        experiment_name=args.experiment, namespace=args.namespace,
        run_name=f"movierec-e{args.epochs}-d{'-'.join(map(str, dims))}",
    )
    print(f"run id: {run.run_id}")
    print(f"UI: {args.host.rsplit('/pipeline', 1)[0]}/_/pipeline/#/runs/details/{run.run_id}")


if __name__ == "__main__":
    main()
