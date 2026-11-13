"""COCO 형식 합성 데이터셋을 torchvision 검출 모델 입력으로 변환한다."""

from __future__ import annotations

import json
import os
import random

import torch
from PIL import Image, ImageEnhance
from torch.utils.data import Dataset
from torchvision.transforms.functional import pil_to_tensor


class IdCardDataset(Dataset):
    def __init__(self, root: str, train: bool = False):
        self.root = root
        self.train = train
        with open(os.path.join(root, "annotations.json"), encoding="utf-8") as f:
            coco = json.load(f)
        self.images = coco["images"]
        self.anns: dict[int, list[dict]] = {img["id"]: [] for img in self.images}
        for a in coco["annotations"]:
            self.anns[a["image_id"]].append(a)

    def __len__(self) -> int:
        return len(self.images)

    def __getitem__(self, idx: int):
        info = self.images[idx]
        img = Image.open(os.path.join(self.root, "images", info["file_name"])).convert("RGB")
        if self.train:
            # 생성 단계에서 기하 증강을 이미 했으므로 학습 시에는 색상만 가볍게 흔든다.
            img = ImageEnhance.Color(img).enhance(random.uniform(0.6, 1.4))
            img = ImageEnhance.Brightness(img).enhance(random.uniform(0.85, 1.15))

        boxes, labels = [], []
        for a in self.anns[info["id"]]:
            x, y, w, h = a["bbox"]
            boxes.append([x, y, x + w, y + h])
            labels.append(a["category_id"])

        target = {
            "boxes": torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4),
            "labels": torch.tensor(labels, dtype=torch.int64),
            "image_id": info["id"],
        }
        image = pil_to_tensor(img).float() / 255.0
        return image, target


def collate_fn(batch):
    return tuple(zip(*batch))
