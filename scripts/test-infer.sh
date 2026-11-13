#!/usr/bin/env bash
# ALB 경유 KServe 추론 테스트
#   scripts/test-infer.sh [이미지 경로]
# 이미지를 주지 않으면 합성 신분증 1장을 생성한다 (pillow, faker 필요: make venv).
set -euo pipefail
source "$(dirname "$0")/env.sh"

HOST="$(alb_host)"; [[ -n "${HOST}" ]] || die "ALB 주소가 없습니다 (make ingress)"
ISVC="${ISVC_NAME:-idcard-recognizer}"
IMG="${1:-}"
PY="${ROOT_DIR}/.venv/bin/python"; [[ -x "${PY}" ]] || PY=python3

if [[ -z "${IMG}" ]]; then
  IMG="$(mktemp -d)/sample.jpg"
  PYTHONPATH="${ROOT_DIR}/ml/src" "${PY}" - "${IMG}" <<'EOF'
import random, sys
from faker import Faker
from idcard.datagen import generate_sample
s = generate_sample(random.Random(), Faker("ko_KR"))
s.image.save(sys.argv[1], quality=92)
print(f"sample: {sys.argv[1]} (정답: {s.card_type}, {s.texts})")
EOF
fi

TOKEN="$(kubectl -n "${KF_PROFILE_NS}" create token default-editor --duration 10m)"
URL="http://${HOST}/serving/${KF_PROFILE_NS}/${ISVC}/recognize"
# KServe 경로 라우팅은 Host가 ingressDomain(기본 example.com)일 때만 맞으므로 InferenceService URL의 Host를 보낸다.
ISVC_HOST="$(kubectl -n "${KF_PROFILE_NS}" get isvc "${ISVC}" -o jsonpath='{.status.url}' | sed -E 's#^https?://([^/:]+).*#\1#')"
[[ -n "${ISVC_HOST}" ]] || die "InferenceService ${ISVC}의 status.url이 없습니다 (파이프라인 deploy 단계 확인)"
log "POST ${URL} (Host: ${ISVC_HOST})"
curl -sS -X POST "${URL}" \
  -H "Host: ${ISVC_HOST}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -F "image=@${IMG}" -F "score_threshold=0.5" \
  -w '\nHTTP %{http_code}, %{time_total}s\n' | { command -v jq >/dev/null && jq -R 'fromjson? // .' || cat; }

log "인증 없이 호출하면 거부되어야 한다:"
curl -s -o /dev/null -w "  HTTP %{http_code} (403/302 기대)\n" -X POST "${URL}" -H "Host: ${ISVC_HOST}" -F "image=@${IMG}"
