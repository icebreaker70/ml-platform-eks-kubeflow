#!/usr/bin/env bash
# Kubeflow 26.10.0 "필수 구성 요소만" 설치 (kubeflow/manifests, kustomize)
#
# 설치:  cert-manager, Istio(+CNI), oauth2-proxy(EKS M2M), Dex, Knative Serving,
#        cluster-local-gateway, Kubeflow namespace/roles/istio-resources,
#        Kubeflow Pipelines, Central Dashboard(+Profiles/KFAM/PodDefaults), KServe, 사용자 프로필
# 제외:  Katib, Trainer, Spark Operator, Hub(Model Registry/Catalog), Workspaces(Notebooks 2.0),
#        Notebooks v1, KServe Models Web App, Knative Eventing, Ray
set -euo pipefail
source "$(dirname "$0")/../../scripts/env.sh"

[[ -n "${OIDC_ISSUER:-}" ]] || die "OIDC_ISSUER가 없습니다. 먼저 make infra 를 실행하세요."
[[ -n "${KF_USER_PASSWORD:-}" ]] || die ".env에 KF_USER_PASSWORD를 설정하세요 (Dex 로그인 비밀번호)."
command -v kustomize >/dev/null || die "kustomize가 필요합니다"

WORK="${ROOT_DIR}/.build/kubeflow-manifests-${KF_VERSION}"
if [[ ! -d "${WORK}" ]]; then
  log "kubeflow/manifests ${KF_VERSION} 클론"
  git clone -q --depth 1 --branch "${KF_VERSION}" https://github.com/kubeflow/manifests.git "${WORK}"
fi
cd "${WORK}"

# ---------------------------------------------------------------------------
# EKS용 oauth2-proxy overlay 패치
#  - M2M 토큰 issuer = 이 클러스터의 EKS OIDC issuer
#  - HTTP(ALB:80)로 접속하므로 secure 쿠키를 끈다 (FORCE_HTTPS=false)
# ---------------------------------------------------------------------------
OVERLAY=common/oauth2-proxy/overlays/m2m-dex-and-eks/kustomization.yaml
git checkout -q -- "${OVERLAY}"
sed -i.bak \
  -e "s#https://oidc.eks.AWS_REGION.amazonaws.com/id/CLUSTER_ID#${OIDC_ISSUER}#" \
  -e "s#FORCE_HTTPS=true#FORCE_HTTPS=false#" \
  "${OVERLAY}"
grep -q "${OIDC_ISSUER}" "${OVERLAY}" || die "oauth2-proxy overlay 패치 실패"

# cluster-local-gateway(KServe 추론 경로)도 EKS ServiceAccount 토큰을 검증하도록 EKS OIDC issuer 규칙을 추가한다.
# 기본 규칙은 https://kubernetes.default.svc* issuer만 있어 EKS 토큰은 "Jwt issuer is not configured"(401)로 거부된다.
# jwksUri를 비워 두면 istiod가 issuer의 OIDC discovery(/.well-known/openid-configuration)로 키를 가져온다.
CLG_RA=common/istio/cluster-local-gateway/overlays/m2m-auth/cluster-local-gateway-requestauthentication.yaml
git checkout -q -- "${CLG_RA}"
python3 - "${CLG_RA}" "${OIDC_ISSUER}" <<'PY'
import sys
path, issuer = sys.argv[1], sys.argv[2]
rule = f'''  # EKS ServiceAccount token issuer (install.sh에서 추가)
  - issuer: "{issuer}"
    forwardOriginalToken: true
    outputClaimToHeaders:
    - header: kubeflow-userid
      claim: sub
    fromHeaders:
    - name: Authorization
      prefix: "Bearer "
'''
s = open(path).read()
assert s.count("  jwtRules:\n") == 1, "jwtRules 위치를 찾지 못했습니다"
open(path, "w").write(s.replace("  jwtRules:\n", "  jwtRules:\n" + rule, 1))
PY
grep -q "${OIDC_ISSUER}" "${CLG_RA}" || die "cluster-local-gateway RequestAuthentication 패치 실패"

apply() {  # kustomize build 결과를 재시도하며 적용 (CRD/웹훅 준비 지연 대응)
  local path="$1"; shift
  for i in 1 2 3 4 5; do
    if kustomize build "${path}" | kubectl apply "$@" -f -; then return 0; fi
    log "재시도 ${i}/5: ${path}"; sleep 15
  done
  die "적용 실패: ${path}"
}

# 네임스페이스의 모든 파드가 Ready가 될 때까지 대기. 매번 목록을 새로 조회하므로
# 대기 중 완료(Succeeded)되는 Job 파드 때문에 끝까지 기다리는 문제(kubectl wait --all)가 없다.
wait_ns() {
  local ns="$1" timeout="${2:-300s}" deadline notready
  deadline=$(( $(date +%s) + ${timeout%s} ))
  while :; do
    notready="$(kubectl -n "${ns}" get pods --no-headers \
      -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,READY:.status.conditions[?(@.type=="Ready")].status' 2>/dev/null \
      | awk '$2!="Succeeded" && $3!="True" {print $1}')"
    [[ -z "${notready}" ]] && { log "${ns}: 모든 파드 Ready"; return 0; }
    if (( $(date +%s) >= deadline )); then
      kubectl -n "${ns}" get pods
      die "${ns}: ${timeout} 안에 Ready가 되지 않은 파드: $(echo ${notready})"
    fi
    sleep 10
  done
}

# 1. cert-manager -------------------------------------------------------------
log "1/10 cert-manager"
apply common/cert-manager/base
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=300s
apply common/cert-manager/overlays/kubeflow
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=300s

# 2. Istio --------------------------------------------------------------------
log "2/10 Istio"
# istio-install overlay에 kubeflow 네임스페이스용 Sidecar가 들어 있으므로 네임스페이스를 먼저 만든다.
apply common/kubeflow-namespace/base
apply common/istio/istio-crds/base
apply common/istio/istio-namespace/base
apply common/istio/istio-install/overlays/oauth2-proxy
wait_ns istio-system
# 컨트롤 플레인→istiod:15017(sidecar injector) 통신 확인. 막혀 있으면 이후 모든 파드 생성이 실패한다.
# (kubeflow 네임스페이스는 PodSecurity restricted라 프로브 파드 자체는 거부될 수 있다. 웹훅 호출 오류만 판정한다.)
for i in $(seq 1 12); do
  PROBE="$(kubectl -n kubeflow run istio-webhook-probe --image=busybox --restart=Never --dry-run=server -o name 2>&1 || true)"
  grep -q "sidecar-injector.istio.io" <<<"${PROBE}" || break
  [[ $i -eq 12 ]] && die "istiod 웹훅(15017) 호출 실패: ${PROBE}
노드 보안그룹에 15017 인바운드가 있는지 확인하고 make infra 를 다시 실행하세요."
  sleep 10
done

# 3. oauth2-proxy (EKS M2M) ---------------------------------------------------
log "3/10 oauth2-proxy (m2m-dex-and-eks)"
apply common/oauth2-proxy/overlays/m2m-dex-and-eks
kubectl -n oauth2-proxy wait --for=condition=Available deploy/oauth2-proxy --timeout=300s

# 4. Dex + 사용자 비밀번호 --------------------------------------------------------
log "4/10 Dex"
apply common/dex/overlays/oauth2-proxy
HASH="$(python3 -c 'import bcrypt,os;print(bcrypt.hashpw(os.environ["KF_USER_PASSWORD"].encode(),bcrypt.gensalt(12)).decode())' 2>/dev/null \
  || htpasswd -bnBC 12 "" "${KF_USER_PASSWORD}" | tr -d ':\n')"
[[ "${HASH}" == \$2* ]] || die "bcrypt 해시 생성 실패 (pip install bcrypt 또는 htpasswd 필요)"
kubectl -n auth create secret generic dex-passwords \
  --from-literal=DEX_USER_PASSWORD="${HASH}" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n auth rollout restart deploy/dex
kubectl -n auth rollout status deploy/dex --timeout=300s

# 5. Knative Serving + 게이트웨이 -------------------------------------------------
log "5/10 Knative Serving"
apply common/knative/knative-serving/overlays/gateways
apply common/istio/cluster-local-gateway/overlays/m2m-auth
wait_ns knative-serving
# ECR 이미지는 태그→다이제스트 변환을 건너뛴다 (Knative 컨트롤러는 ECR 인증 정보가 없음)
kubectl -n knative-serving patch configmap config-deployment --type merge -p \
  "{\"data\":{\"registries-skipping-tag-resolving\":\"kind.local,ko.local,dev.local,${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com\"}}"

# 6. Kubeflow 공통 리소스 ------------------------------------------------------
log "6/10 Kubeflow roles / istio resources"
apply common/kubeflow-roles/base
apply common/istio/kubeflow-istio-resources/base

# 7. Kubeflow Pipelines -------------------------------------------------------
log "7/10 Kubeflow Pipelines"
kubectl apply -f applications/pipeline/upstream/third-party/metacontroller/base/crd.yaml
kubectl wait --for condition=established --timeout=60s crd/compositecontrollers.metacontroller.k8s.io
kubectl apply -f applications/pipeline/upstream/third-party/application/cluster-scoped/application-crd.yaml
kubectl wait --for condition=established --timeout=60s crd/applications.app.k8s.io
apply applications/pipeline/overlays
wait_ns kubeflow 900s

# 8. Central Dashboard (+ Profiles, KFAM, PodDefaults) ------------------------
log "8/10 Central Dashboard"
apply applications/dashboard/overlays/istio
kubectl -n kubeflow rollout status deploy/profiles-deployment --timeout=300s
kubectl wait --for=condition=Established --timeout=60s crd/profiles.kubeflow.org

# 9. KServe (Models Web App 제외) ----------------------------------------------
log "9/10 KServe"
kustomize build applications/kserve/kserve | kubectl apply --server-side --force-conflicts -f - || true
sleep 20
kubectl -n kserve wait --for=condition=Available deploy -l control-plane --timeout=300s || true
kubectl wait --for condition=established --timeout=60s crd/inferenceservices.serving.kserve.io
apply applications/kserve/kserve --server-side --force-conflicts

# 10. 사용자 프로필 ------------------------------------------------------------
log "10/10 사용자 프로필 (${KF_PROFILE_NS})"
apply common/user-namespace/base
for i in $(seq 1 30); do kubectl get ns "${KF_PROFILE_NS}" >/dev/null 2>&1 && break; sleep 5; done
kubectl label namespace "${KF_PROFILE_NS}" pod-security.kubernetes.io/enforce=baseline --overwrite

log "Kubeflow 필수 구성 설치 완료"
kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded || true
