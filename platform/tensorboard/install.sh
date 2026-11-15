#!/usr/bin/env bash
# Kubeflow Tensorboard 구성 요소 추가 설치 (make kubeflow 이후에 실행)
#
#  1) tensorboard-controller  : Tensorboard 커스텀 리소스 → Deployment / Service / VirtualService 생성
#  2) tensorboards-web-app    : 대시보드에서 Tensorboard를 만들고 여는 화면 (/tensorboards/)
#  3) 대시보드 메뉴에 "Tensorboards" 추가
#  4) 로그용 PVC (프로필 네임스페이스, gp3 EBS, ReadWriteOnce)
#
# 두 구성 요소는 kubeflow/manifests의 Notebooks v1 묶음에 들어 있어 "필수 구성만" 설치에서는 빠져 있다.
set -euo pipefail
source "$(dirname "$0")/../../scripts/env.sh"
DIR="$(cd "$(dirname "$0")" && pwd)"

command -v kustomize >/dev/null || die "kustomize가 필요합니다"
WORK="${ROOT_DIR}/.build/kubeflow-manifests-${KF_VERSION}"
[[ -d "${WORK}" ]] || die "${WORK} 가 없습니다. 먼저 make kubeflow 를 실행하세요."
kubectl get crd profiles.kubeflow.org >/dev/null 2>&1 || die "Kubeflow가 설치되어 있지 않습니다 (make kubeflow)"

TB_PVC="${TB_PVC:-movierec-tb}"
TB_PVC_SIZE="${TB_PVC_SIZE:-5Gi}"
NB="applications/notebooks-v1/upstream"
cd "${WORK}"

apply() {
  local path="$1"
  for i in 1 2 3 4 5; do
    if kustomize build "${path}" 2>/dev/null | kubectl apply -f -; then return 0; fi
    log "재시도 ${i}/5: ${path}"; sleep 10
  done
  die "적용 실패: ${path}"
}

# 1. 컨트롤러 -------------------------------------------------------------------
log "1/4 tensorboard-controller"
apply "${NB}/tensorboard-controller/overlays/kubeflow"
kubectl -n kubeflow rollout status deploy/tensorboard-controller-deployment --timeout=300s

# 2. 웹 앱 ---------------------------------------------------------------------
# 이 실습은 HTTP(ALB:80)로 접속한다. 기본값(APP_SECURE_COOKIES=true)이면 CSRF 쿠키에 Secure가 붙어
# 브라우저가 쿠키를 보내지 않고, 화면은 뜨지만 생성·삭제 요청이 403으로 거부된다.
log "2/4 tensorboards-web-app (HTTP 접속용 APP_SECURE_COOKIES=false)"
PARAMS="${NB}/tensorboards-web-app/base/params.env"
git checkout -q -- "${PARAMS}"
sed -i.bak -e "s#^TWA_APP_SECURE_COOKIES=true#TWA_APP_SECURE_COOKIES=false#" "${PARAMS}"
grep -q "^TWA_APP_SECURE_COOKIES=false" "${PARAMS}" || die "tensorboards-web-app params.env 패치 실패"
apply "${NB}/tensorboards-web-app/overlays/istio"
kubectl -n kubeflow rollout status deploy/tensorboards-web-app-deployment --timeout=300s

# 3. 대시보드 메뉴 ---------------------------------------------------------------
# 메뉴가 없어도 http://<ALB>/_/tensorboards/ 로 열 수 있으므로 실패해도 설치는 계속한다.
log "3/4 대시보드 메뉴에 Tensorboards 추가"
if LINKS="$(kubectl -n kubeflow get configmap dashboard-config -o jsonpath='{.data.links}' 2>/dev/null)" \
   && PATCH="$(python3 "${DIR}/menu_link.py" <<<"${LINKS}")"; then
  kubectl -n kubeflow patch configmap dashboard-config --type merge -p "${PATCH}"
else
  printf '[warn] 메뉴 추가를 건너뜁니다 (dashboard-config를 읽지 못함)\n' >&2
fi

# 4. 로그용 PVC ------------------------------------------------------------------
log "4/4 로그용 PVC ${KF_PROFILE_NS}/${TB_PVC} (${TB_PVC_SIZE})"
KF_PROFILE_NS="${KF_PROFILE_NS}" TB_PVC="${TB_PVC}" TB_PVC_SIZE="${TB_PVC_SIZE}" \
  envsubst '${KF_PROFILE_NS} ${TB_PVC} ${TB_PVC_SIZE}' < "${DIR}/pvc.yaml.tmpl" | kubectl apply -f -

log "완료. 다음 순서: make movierec (학습) → make tb (Tensorboard 인스턴스 생성)"
