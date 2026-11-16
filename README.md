# EKS 신분증 인식 MLOps 실습

AWS EKS 위에 **Kubeflow · MLflow · BentoML · KServe**를 구성하고, 합성 신분증 이미지로
검출 모델을 학습 → MLflow 등록 → BentoML 패키징 → KServe 서빙까지 Kubeflow 파이프라인 하나로 자동화합니다.

설계와 버전 선택 근거는 [PLAN.md](PLAN.md)를, 아키텍처 구성도·실습 흐름도와 해설은
[기술 블로그 글](https://velog.io/@sejkim/4%EC%A3%BC%EC%B0%A8-EKS-%EC%9C%84%EC%97%90-Kubeflow-MLflow-BentoML-KServe%EB%A1%9C-%EC%8B%A0%EB%B6%84%EC%A6%9D-%EC%9D%B8%EC%8B%9D-MLOps-%ED%8C%8C%EC%9D%B4%ED%94%84%EB%9D%BC%EC%9D%B8-%EB%A7%8C%EB%93%A4%EA%B8%B0)을 참고하세요.

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
| `infra/terraform/` | VPC, EKS 1.36, 관리형 노드그룹(m6i.xlarge×2), Karpenter, S3, ECR, Pod Identity, ALB 컨트롤러, NVIDIA 플러그인 |
| `platform/karpenter/` | Karpenter NodePool — GPU 노드(Spot 우선, 학습 때만), 파이프라인용 임시 CPU 노드 |
| `platform/kubeflow/` | Kubeflow 26.10.0 필수 구성 설치 스크립트 |
| `platform/ingress/` | ALB Ingress (HTTP:80, 허용 IP 제한) |
| `platform/mlflow/` | MLflow 3.17.2 + PostgreSQL 17 |
| `platform/buildkit/`, `platform/kserve/` | BuildKit 데몬, KServe 접근 정책 |
| `ml/src/idcard/` | 합성 데이터 생성, 학습, 평가, 추론, 후처리(마스킹), 파이프라인 단계 구현 |
| `platform/tensorboard/` | (5주차) Tensorboard 컨트롤러·웹 앱 설치, 로그용 PVC, 인스턴스 생성 스크립트 |
| `ml/src/movierec/` | (5주차) MovieLens 영화 추천(BPR 행렬 분해) 학습 + TensorBoard 로깅 |
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
make nodes        # cpu 2대 Ready, NodePool gpu · burst 준비됨 (GPU 노드는 아직 0대)
```

### GPU 노드는 학습 때만, Spot으로 (Karpenter)

GPU 노드를 항상 켜 두지 않습니다. 학습 파드가 GPU를 요청하면 Karpenter가 그때 노드를 띄우고, 끝나고 5분 뒤 지웁니다.

| NodePool | 언제 뜨나 | 구매 방식 | 올라올 수 있는 파드 |
|---|---|---|---|
| `gpu` | GPU를 요청하는 파드가 Pending일 때 | Spot 우선, 없으면 온디맨드 | `nvidia.com/gpu` taint를 허용한 파드(학습) |
| `burst` | 파이프라인 단계가 일반 노드에 못 들어갈 때 | Spot 우선, 없으면 온디맨드 | `workload=batch` taint를 허용한 파드(파이프라인 단계) |

- 플랫폼 파드(Kubeflow·MLflow·추론)는 관리형 일반 노드(온디맨드) 2대에만 올라갑니다.
- 처음 학습할 때는 노드가 뜨고 이미지를 받는 시간(수 분)이 더 걸립니다. 진행 상황: `make nodes`, `kubectl get nodeclaims`
- 설정은 `terraform.tfvars`에서 바꿉니다. Spot만 쓰려면 `gpu_capacity_types = ["spot"]`, 예전처럼 항상 켜진 온디맨드 GPU 노드를 쓰려면 `enable_karpenter = false`. 바꾼 뒤 `make infra`.
- Spot 노드는 2분 전 통보만 하고 회수될 수 있습니다. 학습 단계는 한 번 자동 재시도합니다.

EKS에서 확인한 결과 (클러스터 `mlops`, 2026-11-16, 신분증 파이프라인 기본값 실행):

| 항목 | 결과 |
|---|---|
| 평소 노드 | 관리형 일반 노드 2대(`m6i.xlarge`, 온디맨드). Karpenter 노드 0대 |
| GPU 노드 | 학습 파드가 Pending이 되자 Spot `g4dn.xlarge` 기동 — 요청 후 약 45초에 노드 준비 완료 |
| 임시 CPU 노드 | 평가 단계부터 Spot `c8i-flex.xlarge` 기동(약 33초), 이후 단계를 모두 처리 |
| 파이프라인 | 20/20 성공, 28분 24초 (학습 15분 14초, T4 GPU) |
| 회수 | GPU 노드는 학습 종료 5분 뒤, 임시 노드는 2분 뒤 비우기 시작 → 일반 노드 2대로 복귀 |

- 확인하지 못한 것: Spot 회수가 실제로 일어났을 때의 재시도, 온디맨드로 넘어가는 경우. GPU 노드가 비우기 시작한 뒤 완전히 삭제되기까지 약 7분이 걸린 이유.

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
NG=$(aws eks list-nodegroups --cluster-name mlops --query "nodegroups[?starts_with(@,'cpu-')]|[0]" --output text)
aws eks update-nodegroup-config --cluster-name mlops --nodegroup-name "$NG" \
  --scaling-config minSize=2,maxSize=3,desiredSize=2
aws eks wait nodegroup-active --cluster-name mlops --nodegroup-name "$NG"
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
Karpenter가 띄운 노드도 함께 지웁니다 (NodePool 삭제 → 남은 인스턴스가 있으면 직접 종료).
**학습이 없을 때도 시간당 약 0.7달러가 나오므로 실습이 끝나면 꼭 삭제하세요.** (GPU 노드를 항상 켜 두던 구성은 약 1.6달러)

## 추가 실습 (5주차): 영화 추천 + Kubeflow Tensorboard

MovieLens(ml-latest-small)로 영화 추천 모델을 학습하고, 로그를 Kubeflow Tensorboard 인스턴스로 확인합니다.
위 1~3단계(인프라·이미지·플랫폼)가 끝난 상태에서 진행합니다. `tensorboard` 패키지와 `movierec` 코드가
학습 이미지에 추가됐으므로, 이전에 만든 이미지가 있다면 `make images`를 다시 실행합니다.

```bash
make movierec-local   # (선택) 로컬 학습 → .venv/bin/tensorboard --logdir runs/movierec
make tensorboard      # Tensorboard 컨트롤러 + 웹 앱 + 로그용 PVC(movierec-tb) 설치
make movierec         # 파이프라인 제출 (EPOCHS=20 EMB_DIMS=16,32,64)
make tb               # 학습이 끝난 뒤 Tensorboard 인스턴스 생성 → 접속 주소 출력
make tb-delete        # 인스턴스 삭제 (로그는 PVC에 남음)
```

- 접속: `http://<ALB>/tensorboard/kubeflow-user-example-com/movierec/` 또는 대시보드 메뉴 **Tensorboards** (`http://<ALB>/_/tensorboards/`)
- 로그 PVC는 EBS(ReadWriteOnce)라 한 노드에만 붙습니다. **다시 학습할 때는 `make tb-delete` → `make movierec` → `make tb` 순서**로 진행합니다.
  인스턴스가 떠 있으면 `make movierec`이 제출을 막습니다(`FORCE=1`로 무시).

### 실행 결과 (EKS, 2026-11-16)

| 항목 | 결과 |
|---|---|
| 파이프라인 `movierec-e20-d16-32-64` | 5/5 성공, 3분 18초 (prepare-data 7초, train-sweep 2분 29초 — 이미지 받기 1분 33초 + 실행 36초) |
| 지표 (HR@10 / NDCG@10) | 16차원 0.7033 / 0.4526 · 32차원 0.7148 / 0.4657 · 64차원 **0.7197 / 0.4720** — 로컬 실행과 동일 |
| Tensorboard 인스턴스 | `make tb` 후 약 1분 만에 브라우저에서 접속. Scalars·Distributions·Histograms·Text·HParams·Time Series·Projector 모두 조회됨 |
| 뷰어 이미지 | 컨트롤러 기본값 `tensorflow/tensorflow:2.5.1`로 `tensorboard 2.21.0`이 쓴 로그를 문제없이 읽음 (Projector는 `INACTIVE` 목록 안에 있음) |
| 노드 | 학습·Tensorboard 모두 일반 노드에서 실행. GPU 노드와 임시 노드는 뜨지 않음 |
| MLflow | 실험 `movierec`에 실행 3개 기록 |

- 인스턴스를 만든 직후 Tensorboard 파드가 한 번 교체됩니다(약 20초 뒤). 접속에는 지장이 없었습니다.
- 로그 PVC는 가용 영역에도 묶입니다. 이번 실행에서는 `ap-northeast-2a`에 만들어져, Tensorboard 파드가 갈 수 있는 일반 노드는 a 영역의 1대였습니다.
- 아직 해 보지 않은 것: 대시보드 화면에서 인스턴스 만들기(목록 화면까지만 확인), 학습 중 실시간 보기.

## 문제 해결

| 증상 | 확인 |
|---|---|
| ALB 접속 시 타임아웃 | 내 공인 IP가 `allowed_cidrs`와 같은지 (`curl ifconfig.me`) |
| 로그인 후 계속 로그인 화면 | HTTP 접속이므로 `FORCE_HTTPS=false` 패치 확인: `kubectl -n oauth2-proxy get cm -o yaml \| grep FORCE` |
| `make pipeline`에서 401/403 | `kubectl -n kubeflow-user-example-com create token default-editor` 동작 확인, `kubectl -n istio-system get requestauthentication m2m-token-issuer -o yaml`의 issuer가 `terraform output oidc_issuer_url`과 같은지 |
| train 파드 Pending (Karpenter) | 노드가 뜨는 중이면 정상(수 분). 오래 걸리면 `kubectl get nodeclaims`, `kubectl -n kube-system logs deploy/karpenter \| tail -30` — Spot 용량 부족·한도 초과·`gpu_node_limit` 도달 여부 확인 |
| train 파드 Pending (관리형 GPU 노드) | `kubectl -n kubeflow-user-example-com describe pod <train 파드>`의 이벤트 확인. `Insufficient cpu`면 GPU 노드에 올라간 다른 파드(추론 파드 등) 때문 — 학습 파드 요청은 CPU 1코어 |
| 플랫폼 파드 Pending | 일반 노드 2대가 꽉 찬 것. `cpu_node_count = 3` 또는 `cpu_instance_type = "m6i.2xlarge"` 후 `make infra` (플랫폼 파드는 Karpenter 노드에 올라가지 않음) |
| evaluate에서 `Failed to download artifacts … model.pt` (새 클러스터 첫 실행) | 새로 만든 S3 버킷은 전역 주소(`<버킷>.s3.amazonaws.com`)가 한동안 `us-east-1`로 연결됨. MLflow 파드에 `MLFLOW_S3_ENDPOINT_URL`이 있는지 확인: `kubectl -n mlflow get deploy mlflow -o yaml \| grep -A1 S3_ENDPOINT` (없으면 `make mlflow`) |
| `make infra`에서 `Error locating chart … no cached repo found` | 로컬 Helm 캐시 문제 — `helm repo update` 후 다시 실행 |
| 노드 수가 `terraform.tfvars`와 다름 | `kubectl get nodes -L node-role` — 수동으로 늘린 노드그룹은 위 "이미 만든 클러스터의 노드 수" 절차로 되돌림 (GPU 2대면 비용 약 1.5배) |
| smoke-test/추론이 `413 Payload Too Large`(HTML) | Host 헤더가 `example.com`(isvc `status.url`의 호스트)인지 — 아니면 대시보드로 라우팅됨 |
| 추론 `401 Jwt issuer is not configured` | `kubectl -n istio-system get requestauthentication cluster-local-gateway-jwt -o yaml`에 EKS issuer 규칙이 있는지 (`make kubeflow` 재실행) |
| MLflow 파드 `OOMKilled` / 업로드 `Connection refused` | `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` 적용 여부 (`make mlflow`) |
| data-gen에서 S3 AccessDenied | `aws eks list-pod-identity-associations --cluster-name mlops` 에 `kubeflow-user-example-com/default-editor` 있는지 |
| image-build 실패 | `kubectl -n buildkit logs deploy/buildkitd` |
| InferenceService가 Ready 안 됨 | `kubectl -n kubeflow-user-example-com describe ksvc idcard-recognizer-predictor`, 이미지 pull 오류면 Knative `registries-skipping-tag-resolving` 설정 확인 |
| 추론 403 | `kubectl -n kubeflow-user-example-com get authorizationpolicy allow-idcard-recognizer` |
