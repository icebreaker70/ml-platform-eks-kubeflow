#!/usr/bin/env bash
# 공통 환경 변수. 사용법:  source scripts/env.sh
# Terraform output과 .env에서 값을 읽어 export 한다.

_ENV_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export ROOT_DIR="$(cd "${_ENV_SH_DIR}/.." && pwd)"
export TF_DIR="${ROOT_DIR}/infra/terraform"

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

tf_out() { terraform -chdir="${TF_DIR}" output -raw "$1" 2>/dev/null; }

if [[ -d "${TF_DIR}/.terraform" ]] && tf_out cluster_name >/dev/null; then
  export AWS_REGION="$(tf_out region)"
  export AWS_DEFAULT_REGION="${AWS_REGION}"
  export CLUSTER_NAME="$(tf_out cluster_name)"
  export ACCOUNT_ID="$(tf_out account_id)"
  export BUCKET="$(tf_out bucket)"
  export ECR_REGISTRY="$(tf_out ecr_registry)"
  export OIDC_ISSUER="$(tf_out oidc_issuer_url)"
  export ALLOWED_CIDRS="$(tf_out allowed_cidrs)"
  export OWNER="$(tf_out owner)"
  export KF_PROFILE_NS="$(tf_out kubeflow_profile_namespace)"
else
  export AWS_REGION="${AWS_REGION:-ap-northeast-2}"
  export CLUSTER_NAME="${CLUSTER_NAME:-mlops}"
  export KF_PROFILE_NS="${KF_PROFILE_NS:-kubeflow-user-example-com}"
fi

export OWNER="${OWNER:-sejkim}"
export KF_VERSION="${KF_VERSION:-26.10.0}"
export KF_USER_EMAIL="${KF_USER_EMAIL:-user@example.com}"
export MLFLOW_VERSION="${MLFLOW_VERSION:-3.17.2}"
export POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:17.11}"
export BUILDKIT_IMAGE="${BUILDKIT_IMAGE:-moby/buildkit:v0.34.1-rootless}"
export IMAGE_TAG="${IMAGE_TAG:-0.1.0}"

if [[ -n "${ECR_REGISTRY:-}" ]]; then
  export ECR_PREFIX="${ECR_REGISTRY}/${CLUSTER_NAME}"
  export IMAGE_ML="${ECR_PREFIX}/idcard-ml:${IMAGE_TAG}"
  export IMAGE_BUILDER="${ECR_PREFIX}/idcard-builder:${IMAGE_TAG}"
  export IMAGE_MLFLOW="${ECR_PREFIX}/mlflow-server:${MLFLOW_VERSION}"
  export IMAGE_RECOGNIZER_REPO="${ECR_PREFIX}/idcard-recognizer"
  export IMAGE_RAG="${ECR_PREFIX}/rag:${RAG_IMAGE_TAG:-${IMAGE_TAG}}"
fi

# 6주차 LLM RAG (PLAN.md 10.2)
export VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.33.0}"
export AWSCLI_IMAGE="${AWSCLI_IMAGE:-amazon/aws-cli:2.40.1}"
export RAGDB_IMAGE="${RAGDB_IMAGE:-pgvector/pgvector:0.8.6-pg17}"
export LLM_MODEL="${LLM_MODEL:-Qwen/Qwen3-8B-FP8}"
export EMBED_MODEL="${EMBED_MODEL:-BAAI/bge-m3}"
# S3 캐시 경로 이름: Qwen/Qwen3-8B-FP8 → qwen3-8b-fp8
model_slug() { tr '[:upper:]' '[:lower:]' <<<"${1##*/}"; }

alb_host() {
  kubectl -n istio-system get ingress kubeflow-alb \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null
}

log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
