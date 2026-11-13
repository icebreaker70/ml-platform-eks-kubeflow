"""BentoML 서비스: 신분증 검출(학습 모델) + EasyOCR + 후처리(마스킹).

KServe InferenceService의 커스텀 predictor 컨테이너로 배포된다.
  POST /recognize       (multipart: image 파일)
  POST /recognize_b64   (JSON: {"image_b64": "...", "score_threshold": 0.5})
  GET  /readyz          (KServe/Knative readiness)
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path

import bentoml
from PIL import Image as PILImage
from PIL.Image import Image


def _model_tag() -> str:
    tag_file = Path(__file__).with_name("model_tag.txt")
    if tag_file.exists():
        return tag_file.read_text().strip()
    return os.environ.get("IDCARD_MODEL_TAG", "idcard-detector:latest")


@bentoml.service(
    name="idcard-recognizer",
    resources={"cpu": "2", "memory": "4Gi"},
    traffic={"timeout": 120, "concurrency": 4},
)
class IdCardRecognizer:
    detector = bentoml.models.BentoModel(_model_tag())

    def __init__(self) -> None:
        import easyocr
        import torch

        from idcard.labels import NUM_CLASSES
        from idcard.model import build_model
        from idcard.recognize import Recognizer

        torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "2")))
        model = build_model(NUM_CLASSES, pretrained=False)
        state = torch.load(self.detector.path_of("model.pt"), map_location="cpu", weights_only=True)
        model.load_state_dict(state)

        reader = easyocr.Reader(
            ["ko", "en"],
            gpu=False,
            model_storage_directory=self.detector.path_of("easyocr"),
            user_network_directory="/tmp/easyocr_user",
            download_enabled=False,
            verbose=False,
        )
        self.recognizer = Recognizer(model, reader, device="cpu")
        with open(self.detector.path_of("meta.json")) as f:
            self.meta = json.load(f)

    def _run(self, image: Image, score_threshold: float, run_ocr: bool) -> dict:
        result = self.recognizer.predict(image, score_threshold=score_threshold, run_ocr=run_ocr)
        result["model"] = {"name": self.meta.get("mlflow_model"), "version": self.meta.get("mlflow_version")}
        return result

    @bentoml.api
    def recognize(self, image: Image, score_threshold: float = 0.5, run_ocr: bool = True) -> dict:
        return self._run(image, score_threshold, run_ocr)

    @bentoml.api
    def recognize_b64(self, image_b64: str, score_threshold: float = 0.5, run_ocr: bool = True) -> dict:
        image = PILImage.open(io.BytesIO(base64.b64decode(image_b64)))
        return self._run(image, score_threshold, run_ocr)
