"""Kubeflow 파이프라인 단계 구현 (idcard-ml 이미지: 데이터 생성/검증/학습/평가/등록).

KFP 컴포넌트 함수는 얇게 두고 실제 로직은 여기 둔다. 로컬에서도 같은 함수를 호출해 테스트할 수 있다.
"""

from __future__ import annotations

import json
import os
import tempfile

from .storage import download_tar, upload_dir_as_tar

WORK_DIR = os.environ.get("IDCARD_WORK_DIR", "/tmp/idcard")


def _dataset_base(bucket: str, dataset_version: str) -> str:
    if bucket:
        return f"s3://{bucket}/datasets/idcard/{dataset_version}"
    return os.path.abspath(os.path.join("artifacts", "datasets", dataset_version))


def _fetch_split(dataset_uri: str, split: str) -> str:
    dest = os.path.join(WORK_DIR, os.path.basename(dataset_uri.rstrip("/")), split)
    if not os.path.exists(os.path.join(dest, "annotations.json")):
        download_tar(f"{dataset_uri}/{split}.tar.gz", dest)
    return dest


# ---------------------------------------------------------------------------
def data_gen(bucket: str, dataset_version: str, num_train: int, num_val: int, seed: int) -> str:
    from .datagen import generate_dataset

    base = _dataset_base(bucket, dataset_version)
    stats = {}
    for split, n, s in (("train", num_train, seed), ("val", num_val, seed + 100_000)):
        with tempfile.TemporaryDirectory() as tmp:
            stats[split] = generate_dataset(tmp, n, s)
            upload_dir_as_tar(tmp, f"{base}/{split}.tar.gz")
        print(f"[data-gen] {split}: {stats[split]['num_images']} images → {base}/{split}.tar.gz", flush=True)
    return base


# ---------------------------------------------------------------------------
def validate_data(dataset_uri: str, min_images_per_card: int = 10) -> tuple[bool, str]:
    """데이터셋 품질 검사. (통과 여부, Markdown 리포트) 반환."""
    from .labels import CARD_CLASS_IDS, CLASSES

    lines = ["# 데이터셋 검증 리포트", "", f"- dataset: `{dataset_uri}`", ""]
    ok = True
    for split in ("train", "val"):
        root = _fetch_split(dataset_uri, split)
        with open(os.path.join(root, "annotations.json"), encoding="utf-8") as f:
            coco = json.load(f)
        per_img: dict[int, list] = {img["id"]: [] for img in coco["images"]}
        bad_boxes = 0
        for a in coco["annotations"]:
            per_img[a["image_id"]].append(a)
            _, _, w, h = a["bbox"]
            bad_boxes += int(w <= 1 or h <= 1)
        no_card = sum(1 for anns in per_img.values()
                      if sum(a["category_id"] in CARD_CLASS_IDS for a in anns) != 1)
        counts = {c: 0 for c in CLASSES[1:]}
        for a in coco["annotations"]:
            counts[CLASSES[a["category_id"]]] += 1
        split_ok = bad_boxes == 0 and no_card == 0 and all(
            counts[c] >= min_images_per_card for c in ("card_rrc", "card_driver", "card_passport"))
        ok &= split_ok
        lines += [f"## {split} ({'통과' if split_ok else '실패'})", "",
                  f"- 이미지 수: {len(coco['images'])}", f"- 박스 수: {len(coco['annotations'])}",
                  f"- 크기가 비정상인 박스: {bad_boxes}", f"- 카드 박스가 1개가 아닌 이미지: {no_card}", "",
                  "| 클래스 | 박스 수 |", "|---|---:|"]
        lines += [f"| {c} | {n} |" for c, n in counts.items()]
        lines.append("")
    return ok, "\n".join(lines)


# ---------------------------------------------------------------------------
def train(dataset_uri: str, mlflow_uri: str, epochs: int, batch_size: int, lr: float) -> dict:
    from . import train as train_mod

    os.environ["MLFLOW_TRACKING_URI"] = mlflow_uri
    train_dir = _fetch_split(dataset_uri, "train")
    val_dir = _fetch_split(dataset_uri, "val")
    return train_mod.train(train_dir, val_dir, epochs=epochs, batch_size=batch_size, lr=lr,
                           num_workers=int(os.environ.get("IDCARD_NUM_WORKERS", "2")),
                           extra_params={"dataset_uri": dataset_uri})


# ---------------------------------------------------------------------------
def load_state_dict_model(mlflow_uri: str, run_id: str):
    import mlflow
    import torch

    from .labels import NUM_CLASSES
    from .model import build_model
    from .model import STATE_DICT_ARTIFACT

    mlflow.set_tracking_uri(mlflow_uri)
    path = mlflow.artifacts.download_artifacts(run_id=run_id, artifact_path=STATE_DICT_ARTIFACT)
    model = build_model(NUM_CLASSES, pretrained=False)
    model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
    return model.eval()


def evaluate(dataset_uri: str, mlflow_uri: str, run_id: str) -> dict:
    import mlflow
    import torch

    from .evaluate import evaluate_model

    model = load_state_dict_model(mlflow_uri, run_id)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    metrics = evaluate_model(model, _fetch_split(dataset_uri, "val"), device=device,
                             num_workers=int(os.environ.get("IDCARD_NUM_WORKERS", "2")))
    with mlflow.start_run(run_id=run_id):
        mlflow.log_metrics({f"eval_{k}": v for k, v in metrics.items()})
    print(json.dumps(metrics, indent=2), flush=True)
    return metrics


# ---------------------------------------------------------------------------
def register(mlflow_uri: str, run_id: str, model_uri: str, model_name: str, map50: float,
             alias: str = "staging") -> str:
    import mlflow
    from mlflow import MlflowClient

    mlflow.set_tracking_uri(mlflow_uri)
    mv = mlflow.register_model(model_uri, model_name, tags={"run_id": run_id})
    client = MlflowClient()
    client.set_model_version_tag(model_name, mv.version, "eval_map50", f"{map50:.4f}")
    client.set_registered_model_alias(model_name, alias, mv.version)
    print(f"[register] {model_name} v{mv.version} (alias={alias}, run={run_id})", flush=True)
    return str(mv.version)
