"""영화 추천(MovieLens, BPR 행렬 분해) 학습 파이프라인 — TensorBoard 실습용 (KFP v2).

prepare-data ─→ train-sweep (임베딩 차원 16 → 32 → 64를 한 파드에서 차례로 학습)
                  └ PVC(movierec-tb)의 movierec/<실행 이름>-dim<N>/ 에 TensorBoard 로그를 쓴다
                    → Kubeflow Tensorboard 인스턴스(logspath: pvc://movierec-tb/movierec)가 같은 PVC를 읽는다

GPU는 쓰지 않는다 (10만 건 규모라 CPU로 차원당 수 초~수십 초).
gp3(EBS) PVC는 ReadWriteOnce라 한 번에 한 노드에만 붙는다. 그래서 학습을 파드 하나로 묶었다.
"""

import os
from typing import List

from kfp import dsl, kubernetes
from kfp.dsl import Dataset, Input, Markdown, Metrics, Output

IMAGE_ML = os.environ.get("IMAGE_ML", "idcard-ml:local")
TB_MOUNT = "/tensorboard"          # 학습 파드가 PVC를 붙이는 경로
TB_SUBDIR = "movierec"             # PVC 안의 로그 폴더 (Tensorboard logspath의 하위 경로와 같아야 한다)


@dsl.component(base_image=IMAGE_ML, install_kfp_package=False)
def movierec_prepare(dataset: Output[Dataset], report: Output[Markdown]) -> int:
    """MovieLens(ml-latest-small)를 내려받아 KFP 아티팩트로 넘긴다. 반환: 상호작용 수."""
    from movierec import data as D

    D.download(dataset.path)
    ds = D.load(dataset.path)
    stats = ds.stats()
    dataset.metadata.update(stats)
    with open(report.path, "w", encoding="utf-8") as f:
        f.write("| 항목 | 값 |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in stats.items()))
    if stats["interactions"] < 50_000:
        raise RuntimeError(f"데이터가 예상보다 적습니다: {stats}")
    return stats["interactions"]


@dsl.component(base_image=IMAGE_ML, install_kfp_package=False)
def movierec_train(dataset: Input[Dataset], emb_dims: List[int], epochs: int, lr: float, mlflow_uri: str,
                   tb_dir: str, run_tag: str, metrics: Output[Metrics]) -> float:
    """임베딩 차원별로 차례로 학습하고 tb_dir/<run_tag>-dim<N>/ 에 TensorBoard 로그를 남긴다. 반환: 가장 높은 HR@10."""
    from movierec import train as T

    result = T.run(dataset.path, tb_dir, [int(d) for d in emb_dims], epochs=epochs, lr=lr,
                   mlflow_uri=mlflow_uri, run_prefix=f"{run_tag}-")
    for name, m in result.items():
        dim = name.rsplit("-", 1)[-1]
        metrics.log_metric(f"hr_at_10_{dim}", round(m["hr@10"], 4))
        metrics.log_metric(f"ndcg_at_10_{dim}", round(m["ndcg@10"], 4))
    metrics.metadata["tensorboard_runs"] = ", ".join(result)
    return max(m["hr@10"] for m in result.values())


def _pod(task, cpu: str = "500m", mem: str = "1Gi", mem_limit: str = "3Gi"):
    task.set_cpu_request(cpu).set_memory_request(mem).set_memory_limit(mem_limit)
    kubernetes.add_pod_annotation(task, "sidecar.istio.io/inject", "false")
    kubernetes.set_image_pull_policy(task, "Always")
    return task


@dsl.pipeline(name="movie-recommendation", description="MovieLens BPR 행렬 분해 학습 + TensorBoard 로그(PVC)")
def movierec_pipeline(
    emb_dims: List[int] = [16, 32, 64],
    epochs: int = 20,
    lr: float = 0.005,
    mlflow_uri: str = "http://mlflow.mlflow.svc.cluster.local:5000/mlflow",
    tb_pvc: str = "movierec-tb",
):
    prep = _pod(movierec_prepare())
    prep.set_caching_options(True)

    tr = _pod(movierec_train(dataset=prep.outputs["dataset"], emb_dims=emb_dims, epochs=epochs, lr=lr,
                             mlflow_uri=mlflow_uri, tb_dir=f"{TB_MOUNT}/{TB_SUBDIR}",
                             run_tag=dsl.PIPELINE_JOB_NAME_PLACEHOLDER), cpu="1")
    tr.set_display_name("train-sweep")
    tr.set_caching_options(False)
    kubernetes.mount_pvc(tr, pvc_name=tb_pvc, mount_path=TB_MOUNT)
