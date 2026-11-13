#!/usr/bin/env bash
# 접속 정보 출력
set -euo pipefail
source "$(dirname "$0")/env.sh"
HOST="$(alb_host)"
cat <<EOF
Kubeflow 대시보드 : http://${HOST}/            (Dex: ${KF_USER_EMAIL} / .env의 KF_USER_PASSWORD)
Kubeflow Pipelines: http://${HOST}/_/pipeline/
MLflow            : http://${HOST}/mlflow/      (먼저 대시보드에 로그인)
KServe 추론        : http://${HOST}/serving/${KF_PROFILE_NS}/idcard-recognizer/recognize  (Bearer 토큰)
허용 IP            : ${ALLOWED_CIDRS:-?}
EOF
kubectl -n "${KF_PROFILE_NS}" get inferenceservice 2>/dev/null || true
