# idcard-ml: 데이터 생성 / 검증 / 학습(GPU) / 평가 컴포넌트 이미지
# 빌드 컨텍스트: 저장소 루트  (make images)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    IDCARD_WORK_DIR=/tmp/idcard \
    MLFLOW_DISABLE_AGENT_HINT=1 \
    TORCH_HOME=/opt/torch

RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk libgl1 libglib2.0-0 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY ml/requirements-ml.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# COCO 사전학습 가중치를 이미지에 미리 받아 둔다 (학습 시작 시 다운로드 생략)
RUN python -c "from torchvision.models.detection import FasterRCNN_MobileNet_V3_Large_FPN_Weights as W; W.DEFAULT.get_state_dict(progress=False)"

COPY ml/src /app/src
