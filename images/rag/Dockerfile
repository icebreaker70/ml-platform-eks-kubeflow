# rag: 6주차 rag-api(FastAPI) · 색인 Job · 가중치 캐시 Job 공용 이미지 (CPU 전용)
# 빌드 컨텍스트: 저장소 루트  (make images-rag)
#
# 모델 가중치는 이미지에 넣지 않는다. 실행할 때 init container가 S3 models/에서 받아 EMBED_MODEL 경로에 둔다.
FROM python:3.12-slim

ARG TORCH_VERSION=2.14.1

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src:/app \
    MLFLOW_DISABLE_AGENT_HINT=1 \
    HF_HOME=/tmp/hf \
    TOKENIZERS_PARALLELISM=false

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# CUDA가 없는 CPU 휠을 먼저 설치한다 (기본 PyPI 휠은 CUDA 라이브러리를 함께 받아 수 GB가 늘어남)
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_VERSION}"
COPY ml/requirements-rag.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# lawrag가 쓰는 주민번호 마스킹(idcard.postprocess)은 표준 라이브러리만 쓴다
COPY ml/src /app/src
COPY ml/rag_api /app/rag_api

EXPOSE 8000
CMD ["uvicorn", "rag_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
