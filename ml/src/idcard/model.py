"""검출 모델 정의: torchvision Faster R-CNN MobileNetV3-Large FPN (Apache-2.0)."""

from __future__ import annotations

from torchvision.models.detection import (
    FasterRCNN_MobileNet_V3_Large_FPN_Weights,
    fasterrcnn_mobilenet_v3_large_fpn,
)
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor

# 입력 해상도: 합성 이미지(1024x768) 기준으로 짧은 변 640까지 줄여 속도를 높인다.
MODEL_KWARGS = {"min_size": 640, "max_size": 960, "box_detections_per_img": 50}

# MLflow run에 저장하는 state_dict 경로. builder 이미지(torchmetrics 없음)에서도 import되도록 여기에 둔다.
STATE_DICT_ARTIFACT = "state_dict/model.pt"


def build_model(num_classes: int, pretrained: bool = True):
    """pretrained=True: COCO 사전학습 가중치에서 시작 (학습용).
    pretrained=False: 가중치 다운로드 없이 구조만 생성 (서빙 시 state_dict 로드용)."""
    if pretrained:
        model = fasterrcnn_mobilenet_v3_large_fpn(
            weights=FasterRCNN_MobileNet_V3_Large_FPN_Weights.DEFAULT, **MODEL_KWARGS
        )
        in_features = model.roi_heads.box_predictor.cls_score.in_features
        model.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes)
        return model
    return fasterrcnn_mobilenet_v3_large_fpn(
        weights=None, weights_backbone=None, num_classes=num_classes, **MODEL_KWARGS
    )
