"""검출 모델 평가: mAP(torchmetrics) + 신분증 종류 분류 정확도."""

from __future__ import annotations

import torch
from torch.utils.data import DataLoader
from torchmetrics.detection import MeanAveragePrecision

from .dataset import IdCardDataset, collate_fn
from .labels import CARD_CLASS_IDS


def predicted_card_type(pred: dict) -> int | None:
    """카드 클래스 박스 중 점수가 가장 높은 클래스 id."""
    best, best_score = None, -1.0
    for label, score in zip(pred["labels"].tolist(), pred["scores"].tolist()):
        if label in CARD_CLASS_IDS and score > best_score:
            best, best_score = label, score
    return best


@torch.no_grad()
def evaluate_model(model, val_root: str, device: str = "cpu", batch_size: int = 4,
                   num_workers: int = 2) -> dict[str, float]:
    ds = IdCardDataset(val_root, train=False)
    loader = DataLoader(ds, batch_size=batch_size, shuffle=False, num_workers=num_workers,
                        collate_fn=collate_fn)
    model.eval().to(device)
    metric = MeanAveragePrecision(iou_type="bbox", class_metrics=True)
    correct = total = 0

    for images, targets in loader:
        images = [img.to(device) for img in images]
        preds = [{k: v.cpu() for k, v in p.items()} for p in model(images)]
        tgts = [{"boxes": t["boxes"], "labels": t["labels"]} for t in targets]
        metric.update(preds, tgts)
        for p, t in zip(preds, targets):
            gt_card = next((label for label in t["labels"].tolist() if label in CARD_CLASS_IDS), None)
            total += 1
            correct += int(gt_card is not None and predicted_card_type(p) == gt_card)

    r = metric.compute()
    out = {
        "map": float(r["map"]),
        "map50": float(r["map_50"]),
        "map75": float(r["map_75"]),
        "card_type_accuracy": correct / max(total, 1),
    }
    # 클래스별 AP (MLflow에서 어떤 필드가 약한지 확인하는 용도)
    if "classes" in r and r["map_per_class"].numel() > 1:
        from .labels import CLASSES

        for cls_id, ap in zip(r["classes"].tolist(), r["map_per_class"].tolist()):
            out[f"ap_{CLASSES[cls_id]}"] = float(ap)
    return out
