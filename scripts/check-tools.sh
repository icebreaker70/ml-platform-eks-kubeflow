#!/usr/bin/env bash
# 로컬 도구 확인
set -uo pipefail

fail=0
check() {  # name, version-command, hint
  if command -v "$1" >/dev/null 2>&1; then
    printf '  %-10s %s\n' "$1" "$(eval "$2" 2>/dev/null | head -1)"
  else
    printf '  %-10s \033[31m없음\033[0m  (%s)\n' "$1" "$3"; fail=1
  fi
}

echo "필수 도구:"
check aws        "aws --version"                              "AWS CLI v2"
check terraform  "terraform version | head -1"                "Terraform >= 1.10 (검증: 1.17.x)"
check kubectl    "command kubectl version --client | head -1" "kubectl 1.36.x"
check kustomize  "kustomize version"                          "kustomize v5.x"
check helm       "helm version --short"                       "Helm 3.x"
check docker     "docker --version"                           "Docker + buildx (linux/amd64 이미지 빌드)"
check envsubst   "envsubst --version"                         "gettext (brew install gettext)"
check python3    "python3 --version"                          "Python 3.12 권장"
check jq         "jq --version"                               "jq"
check openssl    "openssl version"                            "openssl"

echo
if ! python3 -c 'import bcrypt' 2>/dev/null && ! command -v htpasswd >/dev/null; then
  echo "  bcrypt 해시 도구 없음: pip install bcrypt 또는 htpasswd(apache) 필요"; fail=1
fi
docker buildx version >/dev/null 2>&1 || { echo "  docker buildx 없음"; fail=1; }
aws sts get-caller-identity --query Arn --output text 2>/dev/null | sed 's/^/  AWS 자격 증명: /' \
  || { echo "  AWS 자격 증명 없음 (aws configure / AWS_PROFILE)"; fail=1; }

exit $fail
