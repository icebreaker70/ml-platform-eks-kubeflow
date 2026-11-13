# EKS 신분증 인식 MLOps 실습

AWS EKS 위에 **Kubeflow · MLflow · BentoML · KServe**를 구성하고, 합성 신분증 이미지로
검출 모델을 학습 → MLflow 등록 → BentoML 패키징 → KServe 서빙까지 Kubeflow 파이프라인 하나로 자동화합니다.

설계와 버전 선택 근거는 [PLAN.md](PLAN.md)를, 아키텍처 구성도·실습 흐름도와 해설은
[기술 블로그 글](docs/blog-eks-idcard-mlops.md)을 참고하세요.

```
ALB(HTTP, 허용 IP만) → Istio → oauth2-proxy/Dex
   ├─ /                Kubeflow 대시보드
   ├─ /_/pipeline/     Kubeflow Pipelines
   ├─ /mlflow/         MLflow
   └─ /serving/<ns>/idcard-recognizer/recognize   KServe(BentoML 컨테이너)

파이프라인: data-gen → validate → train(GPU) → evaluate ─(mAP@0.5≥기준)→ register(MLflow)
           → bento-build → image-build(BuildKit→ECR) → deploy(KServe, 카나리) → smoke-test → promote
```

## 디렉터리

| 경로 | 내용 |
|---|---|
| `infra/terraform/` | VPC, EKS 1.36, 노드그룹(m6i.xlarge×2, g6.xlarge×1), S3, ECR, Pod Identity, ALB 컨트롤러, NVIDIA 플러그인 |
| `platform/kubeflow/` | Kubeflow 26.10.0 필수 구성 설치 스크립트 |
| `platform/ingress/` | ALB Ingress (HTTP:80, 허용 IP 제한) |
| `platform/mlflow/` | MLflow 3.17.2 + PostgreSQL 17 |
| `platform/buildkit/`, `platform/kserve/` | BuildKit 데몬, KServe 접근 정책 |
| `ml/src/idcard/` | 합성 데이터 생성, 학습, 평가, 추론, 후처리(마스킹), 파이프라인 단계 구현 |
| `ml/serving/` | BentoML 서비스(`service.py`, `bentofile.yaml`) |
| `pipelines/` | KFP v2 컴포넌트·파이프라인·실행 스크립트 |
| `images/` | 컴포넌트 이미지 Dockerfile (idcard-ml, idcard-builder, mlflow-server) |
| `scripts/` | 환경 변수, 이미지 빌드, 추론 테스트, 정리 |

## 0. 준비

```bash
make check-tools            # aws, terraform, kubectl, kustomize, helm, docker(buildx), envsubst, python3, jq
cp .env.example .env        # KF_USER_PASSWORD 변경
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
make venv && make test      # 로컬 가상환경(.venv) + 단위 테스트
make local-demo             # (선택) 로컬 CPU에서 소량 학습 → mlflow.db
```

## 1. 인프라 (약 20분)

```bash
make infra
kubectl get nodes -L node-role                       # cpu 2대, gpu 1대 Ready
kubectl get nodes -o json | jq '.items[].status.allocatable["nvidia.com/gpu"]'   # GPU 노드 "1"
```

모든 AWS 자원에는 `Owner=sejkim` 태그가 붙습니다 (`terraform.tfvars`의 `owner`). 확인:

```bash
aws resourcegroupstaggingapi get-resources --region ap-northeast-2 \
  --tag-filters Key=Owner,Values=sejkim --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | wc -l
aws ec2 describe-instances --filters Name=tag:Owner,Values=sejkim --query 'Reservations[].Instances[].InstanceType'
```

GPU AMI에 device plugin이 이미 들어 있어 `nvidia-device-plugin` 파드가 중복되면
`terraform.tfvars`에 `install_nvidia_device_plugin = false`를 넣고 다시 `make infra` 합니다.

## 2. 컴포넌트 이미지 (첫 빌드 20~40분)

```bash
make images     # idcard-ml(CUDA), idcard-builder(CPU), mlflow-server → ECR
```

Apple Silicon에서는 linux/amd64 에뮬레이션으로 빌드되어 느립니다. `bash scripts/build-images.sh builder`처럼 하나씩 빌드할 수도 있습니다.

## 3. 플랫폼 (약 20분)

```bash
make kubeflow   # cert-manager, Istio, oauth2-proxy, Dex, Knative, KFP, Dashboard, KServe, 프로필
make ingress    # ALB 생성 (허용 IP: 180.224.237.157/32)
make mlflow     # PostgreSQL + MLflow
make extras     # BuildKit 데몬, KServe 접근 정책
make info       # 접속 주소
```

브라우저에서 `http://<ALB>/` → `user@example.com` / `.env`의 비밀번호로 로그인합니다.
MLflow는 같은 세션에서 `http://<ALB>/mlflow/`로 접속합니다.

파드가 `Pending`이면 노드 리소스가 부족한 것입니다:

```bash
kubectl get pods -A --field-selector=status.phase=Pending
kubectl describe node | grep -A8 "Allocated resources"
```

`terraform.tfvars`에서 `cpu_instance_type = "m6i.2xlarge"`로 올리거나 `cpu_node_count`를 늘린 뒤 `make infra` 합니다.

이미 만든 클러스터의 노드 수를 바꿀 때는 먼저 AWS CLI로 노드그룹 크기를 맞춥니다.
EKS 모듈은 생성 후 `desired_size` 변경을 무시하고, `min_size`만 올리면 AWS가 거부하기 때문입니다.

```bash
NG=$(aws eks list-nodegroups --cluster-name idcard-mlops --query "nodegroups[?starts_with(@,'cpu-')]|[0]" --output text)
aws eks update-nodegroup-config --cluster-name idcard-mlops --nodegroup-name "$NG" \
  --scaling-config minSize=2,maxSize=3,desiredSize=2
aws eks wait nodegroup-active --cluster-name idcard-mlops --nodegroup-name "$NG"
make infra   # 이후 Terraform 상태와 맞춘다 (변경 없음이 정상)
```

## 4. 파이프라인 실행

```bash
make compile                                 # pipelines/idcard_pipeline.yaml
make pipeline                                # 기본: 3000장, 10 epoch
EPOCHS=3 NUM_TRAIN=600 make pipeline         # 빠른 확인용
```

Kubeflow Pipelines UI에서 진행 상황을 봅니다. 확인할 것:

- **validate-data**: Markdown 리포트(클래스별 박스 수)
- **train**: GPU 노드에 배치(`kubectl get pod -o wide`), MLflow 실험 `idcard-detector`에 손실 곡선
- **evaluate**: mAP@0.5가 `map50_threshold`(기본 0.6) 이상이어야 다음 단계 진행
- **register-model**: MLflow Model Registry `idcard-detector` 새 버전, alias `staging`
- **image-build**: ECR `idcard-recognizer:v<버전>`
- **deploy-kserve**: 첫 실행은 바로 배포, 두 번째 실행부터 카나리 10%
- **promote**: 카나리 100% 승격, alias `production`

카나리 상태 확인 (두 번째 실행 중):

```bash
kubectl -n kubeflow-user-example-com get isvc idcard-recognizer
# READY  PREV  LATEST  → 90 / 10
```

## 5. 추론 테스트

```bash
make test-infer                  # 합성 신분증 1장 생성 후 ALB로 호출
make test-infer IMG=./my.jpg     # 직접 고른 이미지
```

응답 예시 (주민번호 뒷자리는 마스킹됨):

```json
{
  "card_type": "card_rrc",
  "card_type_ko": "주민등록증",
  "card_score": 0.99,
  "fields": {
    "name": {"text": "임희지", "ocr_confidence": 0.97, "box": [...]},
    "id_number": {"text": "731016-0******", "valid_format": true},
    "address": {"text": "제주특별자치도 부천시 오정구 백제고분91로 565-30"},
    "issue_date": {"text": "2024. 2. 20.", "digits": "2024220"}
  },
  "model": {"name": "idcard-detector", "version": "1"}
}
```

## 6. 정리

```bash
make destroy
```

ALB와 PVC(EBS)를 쿠버네티스에서 먼저 지운 뒤 `terraform destroy`를 실행합니다.
**GPU 노드 때문에 시간당 약 1.6달러가 나오므로 실습이 끝나면 꼭 삭제하세요.**

## 문제 해결

| 증상 | 확인 |
|---|---|
| ALB 접속 시 타임아웃 | 내 공인 IP가 `allowed_cidrs`와 같은지 (`curl ifconfig.me`) |
| 로그인 후 계속 로그인 화면 | HTTP 접속이므로 `FORCE_HTTPS=false` 패치 확인: `kubectl -n oauth2-proxy get cm -o yaml \| grep FORCE` |
| `make pipeline`에서 401/403 | `kubectl -n kubeflow-user-example-com create token default-editor` 동작 확인, `kubectl -n istio-system get requestauthentication m2m-token-issuer -o yaml`의 issuer가 `terraform output oidc_issuer_url`과 같은지 |
| train 파드 Pending | `kubectl -n kubeflow-user-example-com describe pod <train 파드>`의 이벤트 확인. `Insufficient cpu`면 GPU 노드에 올라간 다른 파드(추론 파드 등) 때문 — 학습 파드 요청은 CPU 1코어 |
| 노드 수가 `terraform.tfvars`와 다름 | `kubectl get nodes -L node-role` — 수동으로 늘린 노드그룹은 위 "이미 만든 클러스터의 노드 수" 절차로 되돌림 (GPU 2대면 비용 약 1.5배) |
| smoke-test/추론이 `413 Payload Too Large`(HTML) | Host 헤더가 `example.com`(isvc `status.url`의 호스트)인지 — 아니면 대시보드로 라우팅됨 |
| 추론 `401 Jwt issuer is not configured` | `kubectl -n istio-system get requestauthentication cluster-local-gateway-jwt -o yaml`에 EKS issuer 규칙이 있는지 (`make kubeflow` 재실행) |
| MLflow 파드 `OOMKilled` / 업로드 `Connection refused` | `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` 적용 여부 (`make mlflow`) |
| data-gen에서 S3 AccessDenied | `aws eks list-pod-identity-associations --cluster-name idcard-mlops` 에 `kubeflow-user-example-com/default-editor` 있는지 |
| image-build 실패 | `kubectl -n buildkit logs deploy/buildkitd` |
| InferenceService가 Ready 안 됨 | `kubectl -n kubeflow-user-example-com describe ksvc idcard-recognizer-predictor`, 이미지 pull 오류면 Knative `registries-skipping-tag-resolving` 설정 확인 |
| 추론 403 | `kubectl -n kubeflow-user-example-com get authorizationpolicy allow-idcard-recognizer` |
