"""검출 모델 학습 + MLflow 기록.

  python -m idcard.train --train-dir data/train --val-dir data/val --epochs 2
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time

import mlflow
import torch
from torch.utils.data import DataLoader

from .dataset import IdCardDataset, collate_fn
from .evaluate import evaluate_model
from .labels import CLASSES, NUM_CLASSES
from .model import MODEL_KWARGS, STATE_DICT_ARTIFACT, build_model



def pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def train(train_dir: str, val_dir: str, epochs: int = 10, batch_size: int = 8, lr: float = 0.01,
          num_workers: int = 2, experiment: str = "idcard-detector",
          extra_params: dict | None = None) -> dict:
    """학습 후 {run_id, model_uri, metrics}를 반환한다. MLflow 추적 URI는 MLFLOW_TRACKING_URI로 지정."""
    device = pick_device()
    torch.manual_seed(0)

    train_ds = IdCardDataset(train_dir, train=True)
    loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, num_workers=num_workers,
                        collate_fn=collate_fn, pin_memory=device == "cuda")

    model = build_model(NUM_CLASSES, pretrained=True).to(device)
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(params, lr=lr, momentum=0.9, weight_decay=1e-4)
    total_steps = epochs * len(loader)
    warmup = min(200, max(1, total_steps // 10))
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lambda s: (s + 1) / warmup if s < warmup else 0.5 * (1 + math.cos(math.pi * (s - warmup) / max(1, total_steps - warmup))),
    )
    scaler = torch.amp.GradScaler("cuda", enabled=device == "cuda")

    mlflow.set_experiment(experiment)
    with mlflow.start_run() as run:
        mlflow.log_params({
            "epochs": epochs, "batch_size": batch_size, "lr": lr, "device": device,
            "num_train_images": len(train_ds), "architecture": "fasterrcnn_mobilenet_v3_large_fpn",
            **{f"model_{k}": v for k, v in MODEL_KWARGS.items()},
            **(extra_params or {}),
        })
        if device == "cuda":
            mlflow.set_tag("gpu", torch.cuda.get_device_name(0))

        step = 0
        for epoch in range(epochs):
            model.train()
            t0, running = time.time(), 0.0
            for images, targets in loader:
                images = [img.to(device, non_blocking=True) for img in images]
                targets = [{"boxes": t["boxes"].to(device), "labels": t["labels"].to(device)} for t in targets]
                with torch.autocast(device_type="cuda", enabled=device == "cuda"):
                    loss_dict = model(images, targets)
                    loss = sum(loss_dict.values())
                optimizer.zero_grad(set_to_none=True)
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
                scheduler.step()
                running += loss.item()
                step += 1
                if step % 20 == 0:
                    mlflow.log_metrics({f"train_{k}": v.item() for k, v in loss_dict.items()}, step=step)
                    mlflow.log_metric("lr", scheduler.get_last_lr()[0], step=step)
            epoch_loss = running / max(1, len(loader))
            mlflow.log_metric("epoch_loss", epoch_loss, step=epoch)
            print(f"[epoch {epoch + 1}/{epochs}] loss={epoch_loss:.4f} ({time.time() - t0:.0f}s)", flush=True)

        # 빠른 검증 지표 (정식 평가는 evaluate 단계에서 다시 수행)
        metrics = evaluate_model(model, val_dir, device=device, num_workers=num_workers)
        mlflow.log_metrics({f"val_{k}": v for k, v in metrics.items()})

        model.to("cpu").eval()
        with tempfile.TemporaryDirectory() as tmp:
            sd_path = os.path.join(tmp, "model.pt")
            torch.save(model.state_dict(), sd_path)
            mlflow.log_artifact(sd_path, artifact_path=os.path.dirname(STATE_DICT_ARTIFACT))
            with open(os.path.join(tmp, "labels.json"), "w") as f:
                json.dump({"classes": CLASSES, "model_kwargs": MODEL_KWARGS}, f)
            mlflow.log_artifact(os.path.join(tmp, "labels.json"), artifact_path="state_dict")

        info = mlflow.pytorch.log_model(
            model,
            name="model",
            # MLflow 3.x 기본값 'pt2'는 그래프 추적이 필요해 리스트 입력을 받는 검출 모델에 맞지 않는다.
            serialization_format="pickle",
            pip_requirements=[f"torch=={torch.__version__.split('+')[0]}", "torchvision"],
        )
        return {"run_id": run.info.run_id, "model_uri": info.model_uri, "metrics": metrics}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-dir", required=True)
    ap.add_argument("--val-dir", required=True)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--lr", type=float, default=0.01)
    ap.add_argument("--num-workers", type=int, default=2)
    args = ap.parse_args()
    out = train(args.train_dir, args.val_dir, args.epochs, args.batch_size, args.lr, args.num_workers)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
