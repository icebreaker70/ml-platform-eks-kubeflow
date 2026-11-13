# idcard-builder: MLflow 등록 / Bento 빌드 / BuildKit 이미지 빌드 / KServe 배포 / 스모크 테스트
# 빌드 컨텍스트: 저장소 루트  (make images)
ARG BUILDKIT_VERSION=v0.34.1
FROM moby/buildkit:${BUILDKIT_VERSION} AS buildkit

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    IDCARD_SERVING_DIR=/app/serving \
    BENTOML_HOME=/tmp/bentoml \
    BENTOML_DO_NOT_TRACK=true \
    MLFLOW_DISABLE_AGENT_HINT=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk libgl1 libglib2.0-0 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=buildkit /usr/bin/buildctl /usr/local/bin/buildctl

WORKDIR /app
COPY ml/requirements-builder.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY ml/serving /app/serving
COPY ml/src /app/src
