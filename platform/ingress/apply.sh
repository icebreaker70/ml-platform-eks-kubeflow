#!/usr/bin/env bash
# ALB Ingress 생성 후 주소가 나올 때까지 기다린다.
set -euo pipefail
source "$(dirname "$0")/../../scripts/env.sh"

[[ -n "${ALLOWED_CIDRS:-}" ]] || die "ALLOWED_CIDRS가 없습니다 (terraform output allowed_cidrs)"

log "ALB Ingress 적용 (허용 IP: ${ALLOWED_CIDRS})"
[[ -n "${OWNER:-}" ]] || die "OWNER 태그 값이 없습니다 (terraform output owner)"
ALLOWED_CIDRS="${ALLOWED_CIDRS}" CLUSTER_NAME="${CLUSTER_NAME}" OWNER="${OWNER}" \
  envsubst '${ALLOWED_CIDRS} ${CLUSTER_NAME} ${OWNER}' <"$(dirname "$0")/alb-ingress.yaml.tmpl" | kubectl apply -f -

log "ALB 프로비저닝 대기 (2~3분)"
for i in $(seq 1 60); do
  HOST="$(alb_host)"
  [[ -n "${HOST}" ]] && break
  sleep 10
done
[[ -n "${HOST:-}" ]] || die "ALB 주소를 얻지 못했습니다: kubectl -n istio-system describe ingress kubeflow-alb"

log "ALB: http://${HOST}"
log "타깃이 healthy가 되고 DNS가 전파되기까지 1~3분 더 걸릴 수 있습니다."
