#!/usr/bin/env bash
# 6주차 LLM RAG 플랫폼 (PLAN.md 10장)
#
#   bash platform/rag/install.sh data [PDF]   법률 PDF → s3://<bucket>/datasets/law/<번호>-<시행일>/source.pdf
#   bash platform/rag/install.sh cache        LLM · bge-m3 가중치 Hugging Face → S3 models/ (Job, 한 번만)
#   bash platform/rag/install.sh platform     ragdb(pgvector) + rag-api + 접근 정책
#   bash platform/rag/install.sh index        색인 Job (PDF → 임베딩 → ragdb → 평가 → 승격)
#   bash platform/rag/install.sh llm          law-llm(vLLM) 배포 → GPU 노드 기동 → Ready 대기
#   bash platform/rag/install.sh llm-down     law-llm 삭제 → 5분 뒤 GPU 노드 정리 (실습 후 꼭)
#   bash platform/rag/install.sh ask "질문"   ALB 경유 질의 (SA 토큰)
#   bash platform/rag/install.sh status
#
# 순서: make images-rag → rag-data → llm-cache → rag-platform → rag-index-job → llm → rag-ask
set -euo pipefail
source "$(dirname "$0")/../../scripts/env.sh"
DIR="$(cd "$(dirname "$0")" && pwd)"

LAW_PDF_DEFAULT="${ROOT_DIR}/docs/개인정보 보호법(법률)(제21445호)(20260911).pdf"
export LLM_SLUG="$(model_slug "${LLM_MODEL}")" EMBED_SLUG="$(model_slug "${EMBED_MODEL}")"

need_tf() { [[ -n "${BUCKET:-}" && -n "${IMAGE_RAG:-}" ]] || die "terraform output이 필요합니다 (make infra)"; }

render() {  # 템플릿 → kubectl apply. 지정한 변수만 치환한다 ($(RAGDB_PASSWORD) 같은 k8s 문법은 그대로)
  local tmpl="$1"; shift
  envsubst "$(printf '${%s} ' "$@")" < "${DIR}/${tmpl}"
}
COMMON=(KF_PROFILE_NS BUCKET AWS_REGION AWSCLI_IMAGE IMAGE_RAG EMBED_SLUG)

model_cached() {  # S3에 완료 표시가 있는지
  aws s3api head-object --bucket "${BUCKET}" --key "models/$1/_COMPLETE.json" >/dev/null 2>&1
}

pdf_key() {  # PDF 1쪽에서 법률 번호 · 시행일을 읽어 S3 키를 만든다 (로컬 .venv 필요)
  PYTHONPATH="${ROOT_DIR}/ml/src" "${ROOT_DIR}/.venv/bin/python" - "$1" <<'EOF'
import sys
from lawrag.pdfparse import extract_pages, parse_meta
m = parse_meta(extract_pages(sys.argv[1])[0], 0, "")
print(f"datasets/law/{m.law_no}-{m.effective.replace('-', '')}/source.pdf")
EOF
}

ragdb_secret() {  # ragdb 비밀번호: rag 네임스페이스(DB)와 프로필 네임스페이스(rag-api · Job)에 같은 값
  kubectl create namespace rag --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  local pw
  pw="$(kubectl -n rag get secret ragdb -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)"
  if [[ -z "${pw}" ]]; then
    pw="$(openssl rand -hex 16)"
    kubectl -n rag create secret generic ragdb --from-literal=password="${pw}"
  fi
  kubectl -n "${KF_PROFILE_NS}" create secret generic ragdb --from-literal=password="${pw}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

cmd_data() {
  need_tf
  local pdf="${1:-${LAW_PDF_DEFAULT}}" key
  [[ -f "${pdf}" ]] || die "PDF가 없습니다: ${pdf}"
  key="$(pdf_key "${pdf}")"
  aws s3 cp "${pdf}" "s3://${BUCKET}/${key}" --only-show-errors
  log "업로드: s3://${BUCKET}/${key}"
  mkdir -p "${ROOT_DIR}/.build"
  echo "${key}" > "${ROOT_DIR}/.build/rag-pdf-key"   # make rag-index-job이 읽는다 (.build/는 git 제외)
}

cmd_cache() {
  need_tf
  run_cache() {  # job, repo, slug, args(JSON)
    local job="$1" repo="$2" slug="$3" args="$4"
    if model_cached "${slug}"; then log "이미 캐시됨: s3://${BUCKET}/models/${slug}/ (${repo})"; return; fi
    log "캐시 Job ${job}: ${repo} → s3://${BUCKET}/models/${slug}/"
    kubectl -n "${KF_PROFILE_NS}" delete job "${job}" --ignore-not-found >/dev/null
    CACHE_JOB="${job}" CACHE_ARGS="${args}" render model-cache-job.yaml.tmpl "${COMMON[@]}" CACHE_JOB CACHE_ARGS \
      | kubectl apply -f -
  }
  run_cache model-cache-llm "${LLM_MODEL}" "${LLM_SLUG}" \
    "[\"--repo\", \"${LLM_MODEL}\", \"--dest\", \"s3://${BUCKET}/models/${LLM_SLUG}\"]"
  run_cache model-cache-embed "${EMBED_MODEL}" "${EMBED_SLUG}" \
    "[\"--repo\", \"${EMBED_MODEL}\", \"--dest\", \"s3://${BUCKET}/models/${EMBED_SLUG}\", \"--ignore\", \"onnx/*\", \"imgs/*\", \"*.jpg\"]"
  for job in model-cache-llm model-cache-embed; do
    kubectl -n "${KF_PROFILE_NS}" get job "${job}" >/dev/null 2>&1 || continue
    log "대기: ${job} (LLM은 10분 안팎)"
    kubectl -n "${KF_PROFILE_NS}" wait --for=condition=complete "job/${job}" --timeout=1800s \
      || { kubectl -n "${KF_PROFILE_NS}" logs "job/${job}" --tail=30; die "${job} 실패"; }
    kubectl -n "${KF_PROFILE_NS}" logs "job/${job}" --tail=1
  done
}

cmd_platform() {
  need_tf
  model_cached "${EMBED_SLUG}" || die "bge-m3 캐시가 없습니다 — make llm-cache 먼저"
  log "ragdb (${RAGDB_IMAGE}) + Secret"
  ragdb_secret
  render ragdb.yaml.tmpl RAGDB_IMAGE | kubectl apply -f -
  kubectl -n rag rollout status statefulset/ragdb --timeout=300s
  log "rag-api (${IMAGE_RAG})"
  render rag-api.yaml.tmpl "${COMMON[@]}" LLM_MODEL | kubectl apply -f -
  kubectl -n "${KF_PROFILE_NS}" rollout status deploy/rag-api --timeout=600s
  log "준비 완료 — 색인이 없으면 질의는 503입니다 (make rag-index-job)"
}

cmd_index() {
  need_tf
  local key="${RAG_PDF_KEY:-$(cat "${ROOT_DIR}/.build/rag-pdf-key" 2>/dev/null || true)}"
  [[ -n "${key}" ]] || die "S3 PDF 경로를 모릅니다 — make rag-data 먼저 (또는 RAG_PDF_KEY=datasets/law/…/source.pdf)"
  export PDF_URI="s3://${BUCKET}/${key}" INDEX_SUFFIX="job$(date +%m%d%H%M)" GATE_RECALL="${GATE_RECALL:-0.8}"
  log "색인 Job: ${PDF_URI} (승격 기준 Recall@5 ≥ ${GATE_RECALL})"
  kubectl -n "${KF_PROFILE_NS}" delete job rag-index --ignore-not-found >/dev/null
  render index-job.yaml.tmpl "${COMMON[@]}" PDF_URI INDEX_SUFFIX GATE_RECALL | kubectl apply -f -
  kubectl -n "${KF_PROFILE_NS}" wait --for=condition=ready pod -l job-name=rag-index --timeout=600s >/dev/null 2>&1 || true
  kubectl -n "${KF_PROFILE_NS}" logs -f job/rag-index -c index || true
  kubectl -n "${KF_PROFILE_NS}" wait --for=condition=complete job/rag-index --timeout=60s >/dev/null \
    || die "색인 Job 실패 또는 기준 미달 (위 로그 확인)"
}

cmd_llm() {
  need_tf
  model_cached "${LLM_SLUG}" || die "${LLM_MODEL} 캐시가 없습니다 — make llm-cache 먼저"
  log "law-llm: ${LLM_MODEL} on ${VLLM_IMAGE} (GPU 노드 gpu-serving 기동 → 이미지 · 가중치 받기 → 로딩, 10분 안팎)"
  render llm-isvc.yaml.tmpl KF_PROFILE_NS BUCKET AWS_REGION AWSCLI_IMAGE VLLM_IMAGE LLM_MODEL LLM_SLUG \
    | kubectl apply -f -
  local t0=$SECONDS
  kubectl -n "${KF_PROFILE_NS}" wait --for=condition=Ready isvc/law-llm --timeout=1800s \
    || { kubectl -n "${KF_PROFILE_NS}" get pods -l serving.kserve.io/inferenceservice=law-llm -o wide;
         die "law-llm이 Ready가 되지 않았습니다 — kubectl describe pod / logs -c kserve-container 확인"; }
  log "law-llm Ready ($(( SECONDS - t0 ))초). 실습이 끝나면 make llm-down (GPU 온디맨드 과금)"
}

cmd_llm_down() {
  kubectl -n "${KF_PROFILE_NS}" delete isvc law-llm --ignore-not-found
  log "law-llm 삭제 — 비어 있는 gpu-serving 노드는 5분 뒤 Karpenter가 지웁니다 (확인: make nodes)"
}

cmd_ask() {
  local q="${1:-}"; [[ -n "${q}" ]] || die '질문이 없습니다: make rag-ask Q="..."'
  local host token
  host="$(alb_host)"; [[ -n "${host}" ]] || die "ALB 주소가 없습니다 (make ingress)"
  token="$(kubectl -n "${KF_PROFILE_NS}" create token default-editor --duration 10m)"
  log "POST http://${host}/rag/ask"
  local resp code
  resp="$(mktemp)"
  code="$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "${q}" \
    | curl -sS -o "${resp}" -w '%{http_code} %{time_total}s' -X POST "http://${host}/rag/ask" \
        -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" --data-binary @-)"
  python3 - "${resp}" <<'EOF' || cat "${resp}"
import json, sys
a = json.load(open(sys.argv[1]))
print(a["answer"], "\n")
print("인용:", [c["key"] + ("(근거 없음)" if c["status"] == "unsupported" else "") for c in a["citations"]],
      "| 검색:", [r["key"] + ("*" if r["status"] == "시행예정" else "") for r in a["retrieved"]])
print("색인:", a["index_version"], "| 모델:", a["model"], "| 시간:", a["timings"])
EOF
  echo "HTTP ${code}"; rm -f "${resp}"
  log "인증 없이 호출하면 거부되어야 한다:"
  curl -s -o /dev/null -w "  HTTP %{http_code} (403/302 기대)\n" -X POST "http://${host}/rag/ask" \
    -H "Content-Type: application/json" -d '{"question":"테스트 질문"}'
}

cmd_status() {
  kubectl -n rag get statefulset,pvc 2>/dev/null || echo "ragdb 없음"
  kubectl -n "${KF_PROFILE_NS}" get deploy/rag-api isvc/law-llm job -l 'app in (rag-index,model-cache)' 2>/dev/null || true
  kubectl get nodes -l node-role=gpu-serving -L karpenter.sh/capacity-type,node.kubernetes.io/instance-type 2>/dev/null
  if kubectl -n rag get pod ragdb-0 >/dev/null 2>&1; then
    kubectl -n rag exec ragdb-0 -- psql -U rag -d rag -At -c \
      "select index_version, chunks, (index_version = (select index_version from active_index)) as active
         from index_versions order by created_at" 2>/dev/null || echo "색인 없음"
  fi
}

case "${1:-}" in
  data)      shift; cmd_data "$@" ;;
  cache)     cmd_cache ;;
  platform)  cmd_platform ;;
  index)     cmd_index ;;
  llm)       cmd_llm ;;
  llm-down)  cmd_llm_down ;;
  ask)       shift; cmd_ask "$*" ;;
  status)    cmd_status ;;
  *) sed -n '2,14p' "$0"; exit 1 ;;
esac
