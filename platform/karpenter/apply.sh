#!/usr/bin/env bash
# Karpenter NodePool / EC2NodeClass 적용 (make infra의 마지막 단계)
#
#   bash platform/karpenter/apply.sh           # 적용
#   bash platform/karpenter/apply.sh status    # 현재 NodePool · 노드 · 구매 방식 확인
#
# 값은 Terraform output에서 읽는다. terraform.tfvars의 gpu_capacity_types 등을 바꾼 뒤 make infra를 다시 실행하면 반영된다.
set -euo pipefail
source "$(dirname "$0")/../../scripts/env.sh"
DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "${1:-apply}" == "status" ]]; then
  kubectl get nodepools.karpenter.sh,ec2nodeclasses.karpenter.k8s.aws 2>/dev/null || echo "Karpenter NodePool이 없습니다"
  kubectl get nodeclaims.karpenter.sh 2>/dev/null || true
  kubectl get nodes -L node-role,karpenter.sh/capacity-type,node.kubernetes.io/instance-type,topology.kubernetes.io/zone
  exit 0
fi

if [[ "$(tf_out karpenter_enabled)" != "true" ]]; then
  log "Karpenter 미사용 (enable_karpenter = false) — 관리형 GPU 노드그룹을 그대로 씁니다"
  exit 0
fi

SETTINGS="$(terraform -chdir="${TF_DIR}" output -json karpenter_nodepool_settings)"
get() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1" <<<"${SETTINGS}"; }

export KARPENTER_NODE_ROLE="$(tf_out karpenter_node_role_name)"
export GPU_CAPACITY_TYPES="$(get gpu_capacity_types)"
export GPU_FAMILIES="$(get gpu_families)"
export GPU_LIMIT="$(get gpu_limit)"
export BURST_CAPACITY_TYPES="$(get burst_capacity_types)"
export BURST_CPU_LIMIT="$(get burst_cpu_limit)"
export LLM_ENABLED="$(get llm_enabled)"
export LLM_CAPACITY_TYPES="$(get llm_capacity_types)"
export LLM_INSTANCE_TYPES="$(get llm_instance_types)"
export LLM_GPU_LIMIT="$(get llm_gpu_limit)"
[[ -n "${KARPENTER_NODE_ROLE}" ]] || die "karpenter_node_role_name output이 비어 있습니다 (make infra 확인)"

# Spot을 처음 쓰는 계정에는 EC2 Spot 서비스 연결 역할이 없어 Spot 요청이 실패한다.
if grep -q spot <<<"${GPU_CAPACITY_TYPES}${BURST_CAPACITY_TYPES}${LLM_CAPACITY_TYPES}" \
   && ! aws iam get-role --role-name AWSServiceRoleForEC2Spot >/dev/null 2>&1; then
  log "EC2 Spot 서비스 연결 역할 생성 (계정에 한 번만 필요)"
  aws iam create-service-linked-role --aws-service-name spot.amazonaws.com >/dev/null \
    || printf '[warn] 역할 생성 실패 — Spot 노드가 뜨지 않으면 IAM 권한을 확인하세요\n' >&2
fi

log "Karpenter 컨트롤러 준비 대기"
kubectl -n kube-system rollout status deploy/karpenter --timeout=300s
kubectl wait --for=condition=Established --timeout=120s \
  crd/nodepools.karpenter.sh crd/ec2nodeclasses.karpenter.k8s.aws

VARS='${KARPENTER_NODE_ROLE} ${CLUSTER_NAME} ${OWNER} ${GPU_CAPACITY_TYPES} ${GPU_FAMILIES} ${GPU_LIMIT} ${BURST_CAPACITY_TYPES} ${BURST_CPU_LIMIT}'
log "EC2NodeClass default + NodePool gpu  (구매 방식 ${GPU_CAPACITY_TYPES}, 계열 ${GPU_FAMILIES}, GPU 상한 ${GPU_LIMIT})"
envsubst "${VARS}" < "${DIR}/nodepools.yaml.tmpl" | kubectl apply -f -

if [[ "${BURST_CPU_LIMIT}" != "0" ]]; then
  log "NodePool burst  (구매 방식 ${BURST_CAPACITY_TYPES}, vCPU 상한 ${BURST_CPU_LIMIT})"
  envsubst "${VARS}" < "${DIR}/burst-nodepool.yaml.tmpl" | kubectl apply -f -
else
  kubectl delete nodepools.karpenter.sh burst --ignore-not-found
fi

# python json.load가 true/false를 True/False로 출력한다
if [[ "${LLM_ENABLED}" == "True" ]]; then
  log "NodePool gpu-serving  (구매 방식 ${LLM_CAPACITY_TYPES}, 타입 ${LLM_INSTANCE_TYPES}, GPU 상한 ${LLM_GPU_LIMIT})"
  envsubst '${LLM_CAPACITY_TYPES} ${LLM_INSTANCE_TYPES} ${LLM_GPU_LIMIT}' < "${DIR}/llm-nodepool.yaml.tmpl" | kubectl apply -f -
else
  kubectl delete nodepools.karpenter.sh gpu-serving --ignore-not-found
fi

kubectl get nodepools.karpenter.sh,ec2nodeclasses.karpenter.k8s.aws
log "GPU 노드는 GPU를 요청하는 파드가 생길 때 뜹니다. 확인: bash platform/karpenter/apply.sh status"
