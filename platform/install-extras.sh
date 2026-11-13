#!/usr/bin/env bash
# BuildKit 데몬 + KServe 접근 정책
set -euo pipefail
source "$(dirname "$0")/../scripts/env.sh"
DIR="$(cd "$(dirname "$0")" && pwd)"

log "BuildKit 데몬 (${BUILDKIT_IMAGE})"
BUILDKIT_IMAGE="${BUILDKIT_IMAGE}" envsubst '${BUILDKIT_IMAGE}' < "${DIR}/buildkit/manifests.yaml.tmpl" | kubectl apply -f -
kubectl -n buildkit rollout status deploy/buildkitd --timeout=300s

log "KServe AuthorizationPolicy (${KF_PROFILE_NS})"
KF_PROFILE_NS="${KF_PROFILE_NS}" envsubst '${KF_PROFILE_NS}' < "${DIR}/kserve/manifests.yaml.tmpl" | kubectl apply -f -
