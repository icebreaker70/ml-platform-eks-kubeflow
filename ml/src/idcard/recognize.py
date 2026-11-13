"""추론 파이프라인: 검출 → 필드 영역 OCR → 후처리. BentoML 서비스와 로컬 테스트가 함께 사용한다."""

from __future__ import annotations

import time

import numpy as np
import torch
from PIL import Image
from torchvision.transforms.functional import pil_to_tensor

from .labels import CARD_CLASS_IDS, CARD_TYPE_KO, CLASSES, TEXT_FIELDS
from .postprocess import postprocess_field

OCR_ALLOWLIST = {
    "id_number": "0123456789-",
    "issue_date": "0123456789. ABCDEFGHIJKLMNOPQRSTUVWXYZ",
}


class Recognizer:
    def __init__(self, model, reader=None, device: str = "cpu"):
        """model: torchvision 검출 모델(eval 모드), reader: easyocr.Reader (None이면 OCR 생략)."""
        self.model = model.eval().to(device)
        self.reader = reader
        self.device = device

    @torch.no_grad()
    def detect(self, image: Image.Image, score_threshold: float = 0.5) -> list[dict]:
        tensor = (pil_to_tensor(image.convert("RGB")).float() / 255.0).to(self.device)
        out = self.model([tensor])[0]
        dets = []
        for box, label, score in zip(out["boxes"].tolist(), out["labels"].tolist(), out["scores"].tolist()):
            if score >= score_threshold:
                dets.append({"label": CLASSES[label], "label_id": label, "score": round(score, 4),
                             "box": [round(v, 1) for v in box]})
        return dets

    def _ocr(self, image: Image.Image, box: list[float], field: str, card_type: str | None) -> tuple[str, float]:
        x0, y0, x1, y1 = box
        mx, my = (x1 - x0) * 0.04, (y1 - y0) * 0.12
        crop = image.crop((max(0, x0 - mx), max(0, y0 - my), min(image.width, x1 + mx), min(image.height, y1 + my)))
        allow = OCR_ALLOWLIST.get(field)
        if field == "id_number" and card_type == "card_passport":
            allow = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        results = self.reader.readtext(np.asarray(crop.convert("RGB")), detail=1, paragraph=False, allowlist=allow)
        if not results:
            return "", 0.0
        # 위→아래, 왼→오른쪽 순서로 이어 붙인다.
        results.sort(key=lambda r: (round(min(p[1] for p in r[0]) / 20), min(p[0] for p in r[0])))
        text = " ".join(r[1] for r in results)
        conf = float(np.mean([r[2] for r in results]))
        return text, conf

    def predict(self, image: Image.Image, score_threshold: float = 0.5, run_ocr: bool = True) -> dict:
        t0 = time.perf_counter()
        image = image.convert("RGB")
        dets = self.detect(image, score_threshold)

        cards = [d for d in dets if d["label_id"] in CARD_CLASS_IDS]
        card = max(cards, key=lambda d: d["score"]) if cards else None
        card_type = card["label"] if card else None

        fields: dict[str, dict] = {}
        for cls in TEXT_FIELDS + ["field_photo"]:
            cand = [d for d in dets if d["label"] == cls]
            if not cand:
                continue
            best = max(cand, key=lambda d: d["score"])
            name = cls.removeprefix("field_")
            entry = {"box": best["box"], "score": best["score"]}
            if run_ocr and self.reader is not None and cls in TEXT_FIELDS:
                raw, conf = self._ocr(image, best["box"], name, card_type)
                entry.update(postprocess_field(name, raw, card_type))
                entry["ocr_confidence"] = round(conf, 4)
            fields[name] = entry

        return {
            "card_type": card_type,
            "card_type_ko": CARD_TYPE_KO.get(card_type) if card_type else None,
            "card_score": card["score"] if card else None,
            "card_box": card["box"] if card else None,
            "fields": fields,
            "num_detections": len(dets),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
