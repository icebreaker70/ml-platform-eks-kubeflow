#!/usr/bin/env bash
# 접속 정보 출력
set -euo pipefail
source "$(dirname "$0")/env.sh"
HOST="$(alb_host)"
cat <<EOF
Kubeflow 대시보드 : http://${HOST}/            (Dex: ${KF_USER_EMAIL} / .env의 KF_USER_PASSWORD)
Kubeflow Pipelines: http://${HOST}/_/pipeline/
MLflow            : http://${HOST}/mlflow/      (먼저 대시보드에 로그인)
Tensorboards      : http://${HOST}/_/tensorboards/   (make tensorboard 설치 후)
KServe 추론        : http://${HOST}/serving/${KF_PROFILE_NS}/idcard-recognizer/recognize  (Bearer 토큰)
개인정보 보호법 Q&A: http://${HOST}/rag/       (6주차, make rag-platform 설치 후 · 먼저 대시보드에 로그인)
허용 IP            : ${ALLOWED_CIDRS:-?}
EOF
kubectl -n "${KF_PROFILE_NS}" get inferenceservice 2>/dev/null || true
if kubectl -n "${KF_PROFILE_NS}" get isvc law-llm >/dev/null 2>&1; then
  printf '\033[1;33m[주의]\033[0m law-llm(GPU 서빙, g6.xlarge 온디맨드 시간당 약 $1)이 떠 있습니다. 실습이 끝나면 make llm-down\n'
fi
