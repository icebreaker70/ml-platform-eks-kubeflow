#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/../../scripts/env.sh"

[[ -n "${BUCKET:-}" && -n "${IMAGE_MLFLOW:-}" ]] || die "terraform output이 필요합니다 (make infra)"

log "MLflow ${MLFLOW_VERSION} + ${POSTGRES_IMAGE} 설치"
kubectl create namespace mlflow --dry-run=client -o yaml | kubectl apply -f -
if ! kubectl -n mlflow get secret mlflow-db >/dev/null 2>&1; then
  kubectl -n mlflow create secret generic mlflow-db --from-literal=password="$(openssl rand -hex 16)"
fi

BUCKET="${BUCKET}" IMAGE_MLFLOW="${IMAGE_MLFLOW}" POSTGRES_IMAGE="${POSTGRES_IMAGE}" AWS_REGION="${AWS_REGION}" \
  envsubst '${BUCKET} ${IMAGE_MLFLOW} ${POSTGRES_IMAGE} ${AWS_REGION}' \
  < "$(dirname "$0")/manifests.yaml.tmpl" | kubectl apply -f -

kubectl -n mlflow rollout status statefulset/postgres --timeout=300s
kubectl -n mlflow rollout status deploy/mlflow --timeout=600s
log "MLflow 준비 완료 — 내부: http://mlflow.mlflow.svc.cluster.local:5000/mlflow"
