#!/usr/bin/env bash
# 컴포넌트 이미지 3종을 linux/amd64로 빌드해 ECR에 푸시한다.
#   idcard-ml (CUDA torch, 약 7GB), idcard-builder (CPU torch), mlflow-server
# Apple Silicon에서는 에뮬레이션으로 빌드되므로 처음에는 20~40분 걸릴 수 있다.
set -euo pipefail
source "$(dirname "$0")/env.sh"

[[ -n "${ECR_REGISTRY:-}" ]] || die "terraform output이 필요합니다 (make infra)"
[[ $# -gt 0 ]] || set -- ml builder mlflow

log "ECR 로그인 (${ECR_REGISTRY})"
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${ECR_REGISTRY}"

build() {  # dockerfile, image, [extra args...]
  local dockerfile="$1" image="$2"; shift 2
  log "build & push ${image}"
  docker buildx build --platform linux/amd64 -f "${ROOT_DIR}/${dockerfile}" -t "${image}" --push "$@" "${ROOT_DIR}"
}

for t in "$@"; do
  case "$t" in
    ml)      build images/ml/Dockerfile "${IMAGE_ML}" ;;
    builder) build images/builder/Dockerfile "${IMAGE_BUILDER}" ;;
    mlflow)  build images/mlflow/Dockerfile "${IMAGE_MLFLOW}" --build-arg "MLFLOW_VERSION=${MLFLOW_VERSION}" ;;
    *) die "알 수 없는 대상: $t (ml|builder|mlflow)" ;;
  esac
done
log "완료"
