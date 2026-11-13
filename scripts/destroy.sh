#!/usr/bin/env bash
# 전체 정리. 순서가 중요하다:
#  1) ALB(Ingress)와 PVC(EBS)를 쿠버네티스에서 먼저 지운다 → 컨트롤러가 AWS 리소스를 정리
#  2) terraform destroy (EKS, VPC, S3, ECR)
# 1)을 건너뛰면 ALB/ENI가 남아 VPC 삭제가 실패하거나 EBS 볼륨이 고아로 남는다.
set -uo pipefail
source "$(dirname "$0")/env.sh"

read -r -p "클러스터 ${CLUSTER_NAME} 와 S3/ECR 데이터를 모두 삭제합니다. 계속할까요? (yes/no) " ans
[[ "${ans}" == "yes" ]] || { echo "취소"; exit 1; }

if kubectl cluster-info >/dev/null 2>&1; then
  log "InferenceService / Ingress 삭제"
  kubectl -n "${KF_PROFILE_NS}" delete inferenceservice --all --timeout=120s 2>/dev/null
  kubectl -n istio-system delete ingress kubeflow-alb --timeout=180s 2>/dev/null

  log "PVC 사용 워크로드 삭제 (EBS 볼륨 반환)"
  kubectl delete profile --all --timeout=180s 2>/dev/null
  kubectl delete namespace mlflow buildkit --timeout=300s 2>/dev/null
  kubectl -n kubeflow delete deploy,statefulset --all --timeout=180s 2>/dev/null
  kubectl -n kubeflow delete pvc --all --timeout=180s 2>/dev/null

  log "ALB 삭제 대기"
  for i in $(seq 1 30); do
    n=$(aws elbv2 describe-load-balancers --region "${AWS_REGION}" \
          --query "LoadBalancers[?contains(LoadBalancerName, 'k8s-idcardml')].LoadBalancerArn" --output text 2>/dev/null | wc -w)
    [[ "${n}" -eq 0 ]] && break
    sleep 10
  done
  sleep 30
else
  log "클러스터에 접속할 수 없어 쿠버네티스 정리 단계를 건너뜁니다"
fi

log "terraform destroy"
terraform -chdir="${TF_DIR}" destroy -auto-approve

log "남은 EBS 볼륨 확인 (PVC로 만들어진 볼륨)"
aws ec2 describe-volumes --region "${AWS_REGION}" \
  --filters "Name=tag-key,Values=kubernetes.io/created-for/pvc/name" "Name=status,Values=available" \
  --query 'Volumes[].{id:VolumeId,size:Size,pvc:Tags[?Key==`kubernetes.io/created-for/pvc/name`]|[0].Value}' \
  --output table
echo "위 목록에 이 실습의 볼륨이 있으면 aws ec2 delete-volume --volume-id <id> 로 삭제하세요."
