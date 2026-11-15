#!/usr/bin/env bash
# Tensorboard 인스턴스 생성 / 삭제 / 상태 확인
#   bash platform/tensorboard/instance.sh create|delete|status
set -euo pipefail
source "$(dirname "$0")/../../scripts/env.sh"
DIR="$(cd "$(dirname "$0")" && pwd)"

TB_NAME="${TB_NAME:-movierec}"
TB_PVC="${TB_PVC:-movierec-tb}"
TB_SUBDIR="${TB_SUBDIR:-movierec}"
NS="${KF_PROFILE_NS}"

case "${1:-create}" in
  create)
    kubectl get crd tensorboards.tensorboard.kubeflow.org >/dev/null 2>&1 \
      || die "Tensorboard CRD가 없습니다. 먼저 make tensorboard 를 실행하세요."
    kubectl -n "${NS}" get pvc "${TB_PVC}" >/dev/null 2>&1 || die "PVC ${NS}/${TB_PVC} 가 없습니다 (make tensorboard)"
    log "Tensorboard ${NS}/${TB_NAME}  (logspath: pvc://${TB_PVC}/${TB_SUBDIR})"
    KF_PROFILE_NS="${NS}" TB_NAME="${TB_NAME}" TB_PVC="${TB_PVC}" TB_SUBDIR="${TB_SUBDIR}" \
      envsubst '${KF_PROFILE_NS} ${TB_NAME} ${TB_PVC} ${TB_SUBDIR}' < "${DIR}/tensorboard.yaml.tmpl" | kubectl apply -f -
    # 컨트롤러가 Deployment를 만들 때까지 잠시 기다린 뒤 준비 상태를 확인한다.
    for i in $(seq 1 30); do
      kubectl -n "${NS}" get deploy "${TB_NAME}" >/dev/null 2>&1 && break
      [[ $i -eq 30 ]] && die "컨트롤러가 Deployment를 만들지 않았습니다: kubectl -n kubeflow logs deploy/tensorboard-controller-deployment -c manager"
      sleep 2
    done
    kubectl -n "${NS}" rollout status deploy/"${TB_NAME}" --timeout=300s
    HOST="$(alb_host)"
    echo "Tensorboard : http://${HOST}/tensorboard/${NS}/${TB_NAME}/   (먼저 대시보드에 로그인)"
    echo "목록 화면    : http://${HOST}/_/tensorboards/"
    ;;
  delete)
    # 인스턴스만 지운다. 로그는 PVC에 그대로 남는다.
    kubectl -n "${NS}" delete tensorboard "${TB_NAME}" --ignore-not-found
    kubectl -n "${NS}" wait --for=delete pod -l app="${TB_NAME}" --timeout=120s 2>/dev/null || true
    ;;
  status)
    kubectl -n "${NS}" get tensorboard,pvc 2>/dev/null || true
    kubectl -n "${NS}" get pods -l app="${TB_NAME}" -o wide 2>/dev/null || true
    ;;
  *) die "사용법: $0 create|delete|status" ;;
esac
