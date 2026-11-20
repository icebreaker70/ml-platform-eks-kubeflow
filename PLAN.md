# EKS 기반 ML 플랫폼 실습 — 구현 계획서 (v4)

> 최초 작성: 2026-11-13 · 최종 수정: 2026-11-20 · 상태: v4 (4주차 · 5주차 실습 EKS 검증 완료, 릴리즈 `v1.0.0`)
> 실행 방법은 [README.md](README.md), 해설은 4주차 · 5주차 블로그 글(README 맨 위 링크)

| 주차 | 실습 | 핵심 구성 |
|---|---|---|
| 4주차 | 신분증 인식 MLOps 파이프라인 | Kubeflow Pipelines · MLflow · BentoML · KServe, GPU 학습 |
| 5주차 | Kubeflow Tensorboard + 영화 추천 | Tensorboard 컨트롤러 · 웹 앱, MovieLens BPR 학습, TensorBoard 로깅 |
| 공통 | 비용 절감 | Karpenter — GPU 노드는 학습 때만 Spot으로 |

## 0. 변경 이력

| 버전 | 날짜 | 내용 |
|---|---|---|
| v1 | 11-13 | 초안 |
| v2 | 11-13 | 리전·노드 사양·OCR 범위·IaC·MLflow DB·외부 접속 방식 확정, 설치 제품 버전 명시 |
| v3 | 11-13 | HTTP 접속 + 허용 IP 확정, Kubeflow 필수 구성만 설치, 버전 재검증(torch/torchvision 짝, MLflow 패치 버전 등), 구현 중 바뀐 설계 반영 |
| v3.1 | 11-13 | 허용 IP를 180.224.237.157/32로 변경, 모든 AWS 자원에 `Owner=sejkim` 태그 필수 적용 |
| v3.2 | 11-13 | 일반 노드를 `m6i.xlarge` × 2로 변경 (GPU 노드 `g6.xlarge` × 1 유지) |
| v3.3 | 11-14 | EKS 실측 반영: 학습 파드 CPU 요청 2 → 1 (taint 없는 GPU 노드에서 `Insufficient cpu`로 20분 Pending), 기본값 실행(10 epoch·3,000장) 통과 기록 |
| v3.4 | 11-15 | GPU 비용 절감: 관리형 GPU 노드그룹을 Karpenter NodePool로 대체 (Spot 우선, 학습 때만 기동, taint 적용). 파이프라인용 임시 CPU NodePool(burst) 추가 |
| **v4** | 11-15 ~ 11-20 | **5주차 실습 추가**(Tensorboard 컨트롤러 · 웹 앱, MovieLens 영화 추천, 로그용 PVC). 클러스터 이름 `idcard-mlops` → `mlops`. MLflow presigned URL을 리전 엔드포인트로 고정(`MLFLOW_S3_ENDPOINT_URL`). Karpenter · 5주차 실습 EKS 검증. 릴리즈 `v1.0.0` |

### 확정된 요구사항
| 항목 | 결정 |
|---|---|
| 리전 | `ap-northeast-2` (서울) |
| 클러스터 이름 | `mlops` (S3 버킷 · ECR 저장소 · IAM 역할 이름의 접두사로도 사용) |
| 일반 노드 | `m6i.xlarge` × 2, EKS 관리형 노드그룹, 온디맨드 — 리소스가 부족하면 `cpu_node_count = 3` 또는 `m6i.2xlarge`로 상향 (Terraform 변수) |
| GPU 노드 | Karpenter가 GPU 파드가 있을 때만 기동 — Spot 우선(없으면 온디맨드), `g6`/`g5`/`g4dn` 계열의 xlarge·2xlarge. `enable_karpenter = false`면 `g6.xlarge` × 1 상시 |
| 인식 범위 (4주차) | **검출 모델만 학습**, 글자 인식은 EasyOCR 사전학습 모델 사용 |
| 추천 범위 (5주차) | MovieLens `ml-latest-small`, 암묵적 피드백, BPR 행렬 분해, CPU 학습 |
| IaC | Terraform |
| MLflow DB | 클러스터 내부 PostgreSQL (StatefulSet + gp3 PVC) |
| 외부 접속 | AWS ALB, **HTTP(80)**, 접속 허용 IP **180.224.237.157/32** (ALB와 EKS API 엔드포인트 모두) |
| Kubeflow 범위 | **필수 서비스만** 설치 + 5주차에 Tensorboard 2종 추가 (2.2절) |
| 자원 태그 | **`Owner=sejkim` 필수** (+ `Project=mlops`, `ManagedBy`). 적용 경로는 아래 표 |

#### Owner 태그 적용 경로
| 자원 | 생성 주체 | 적용 방법 |
|---|---|---|
| VPC, 서브넷, NAT, EKS 클러스터, IAM 역할, S3, ECR, EKS 애드온, SQS(Karpenter) | Terraform | provider `default_tags` + 모듈 `tags` |
| 일반 노드 EC2 인스턴스, 루트 EBS, ENI | EKS 관리형 노드그룹 | 모듈 `tags` → launch template `tag_specifications` |
| GPU · 임시 CPU 노드 EC2 인스턴스, EBS | Karpenter | EC2NodeClass `spec.tags` (`ManagedBy=karpenter`) |
| ALB, 대상 그룹, 보안 그룹 | AWS Load Balancer Controller | Helm `defaultTags.Owner` + Ingress `alb.ingress.kubernetes.io/tags` |
| PVC용 EBS (PostgreSQL, KFP MySQL/SeaweedFS, BuildKit 캐시, TensorBoard 로그) | EBS CSI 드라이버 | StorageClass `gp3`의 `tagSpecification_1: Owner=sejkim` |

## 1. 목표와 완료 기준

AWS EKS 위에 Kubeflow · MLflow · BentoML · KServe를 구성하고, 두 가지 실습을 Kubeflow 파이프라인으로 자동화한다.
("KFServing"은 **KServe**로 이름이 바뀌었다.)

**공통**
- `make infra` → VPC · EKS · 일반 노드그룹 · Karpenter(NodePool 포함) · S3 · ECR · IAM · ALB 컨트롤러 · NVIDIA 플러그인 생성
- `make images` → 컴포넌트 이미지 3종 ECR 푸시
- `make platform` → Kubeflow 필수 구성 · ALB · MLflow · BuildKit 설치, ALB 주소로 대시보드/MLflow 접속
- `make destroy` → ALB · EBS · Karpenter 노드 정리 후 전체 삭제

**4주차 — 신분증 인식**
- `make pipeline` → GPU 학습(Karpenter Spot) → MLflow 등록 → Bento 이미지 빌드 → KServe 배포(2회차부터 카나리) → 스모크 테스트 → 승격
- `make test-infer` → ALB 경유 추론, 신분증 종류 + 필드 JSON 반환(주민번호 뒷자리 마스킹)

**5주차 — Tensorboard + 영화 추천**
- `make tensorboard` → Tensorboard 컨트롤러 · 웹 앱, 대시보드 메뉴, 로그용 PVC
- `make movierec` → MovieLens 학습(임베딩 차원별), PVC에 TensorBoard 로그
- `make tb` → Tensorboard 인스턴스 생성, 브라우저에서 Scalars · Histograms · Projector · Text · HParams 확인

## 2. 설치 제품 버전

> 2026-11-13에 각 프로젝트 공식 릴리스 페이지/PyPI/GitHub API로 확인했다(Karpenter는 11-15, TensorBoard는 11-15). 출시 2주 미만 버전은 피하고 직전 버전으로 고정했다.

### 2.1 인프라 / 클러스터
| 구분 | 제품 | 고정 버전 | 비고 |
|---|---|---|---|
| IaC | Terraform CLI | `>= 1.10` (검증 1.14.8, 최신 1.17.2) | |
| IaC | AWS / Helm / Kubernetes Provider | 6.73.0 / 3.3.0 / 3.5.0 | `.terraform.lock.hcl`에 고정 |
| IaC | terraform-aws-modules/vpc | 6.7.3 | |
| IaC | terraform-aws-modules/eks (+ `modules/karpenter`) | 21.29.0 | |
| IaC | terraform-aws-modules/eks-pod-identity | 2.10.0 | |
| 클러스터 | Amazon EKS | **1.36** | 표준 지원 ~2027-08-02, Kubeflow 26.10.0(1.35~1.37)·26.03.1(1.36) 검증 범위 |
| 노드 AMI | `AL2023_x86_64_STANDARD` (관리형) / `al2023@latest` (Karpenter, GPU는 NVIDIA 변형 자동 선택) | EKS 최신 릴리스 | |
| 애드온 | vpc-cni, coredns, kube-proxy, eks-pod-identity-agent, aws-ebs-csi-driver | EKS 1.36 최신 | |
| 오토스케일러 | Karpenter (Helm, `oci://public.ecr.aws/karpenter`) | **1.15.1** | EKS 1.36은 1.13 이상 필요. 1.15.2는 2026-11-12 출시라 제외 |
| 컨트롤러 | AWS Load Balancer Controller (Helm) | **3.6.2** | 3.6.3은 2026-11-13 출시라 제외 |
| GPU | NVIDIA k8s-device-plugin (Helm) | **0.20.1** | |

### 2.2 Kubeflow 26.10.0 — 필수 구성 + Tensorboard
| 설치 | 버전 | 용도 |
|---|---|---|
| cert-manager | 1.21.2 | 웹훅 인증서 |
| Istio (+CNI) | 1.31.3 | 게이트웨이, 사이드카, 인증 정책 |
| OAuth2-Proxy (`m2m-dex-and-eks`) | 7.15.5 | 브라우저 로그인 + **EKS ServiceAccount 토큰(M2M)** 인증 |
| Dex | 2.46.1 | 사용자 로그인 (`user@example.com`) |
| Knative Serving (+cluster-local-gateway) | 1.24.0 | KServe 서버리스 모드, 카나리 |
| Kubeflow Pipelines | 2.18.0 | 파이프라인 (MySQL, SeaweedFS 포함) |
| Central Dashboard (+Profiles, KFAM, PodDefaults) | 2.0.1 | UI, 사용자 네임스페이스 |
| KServe | 0.21.0 | 모델 서빙 (경로 기반 URL `/serving/<ns>/<name>`) |
| 사용자 프로필 | — | `kubeflow-user-example-com` |
| **tensorboard-controller** (5주차, `make tensorboard`) | v1.11.1 | `Tensorboard` 리소스 → Deployment · Service · VirtualService. 뷰어 이미지 기본값 `tensorflow/tensorflow:2.5.1` |
| **tensorboards-web-app** (5주차, `make tensorboard`) | v1.11.1 | `/tensorboards/` 화면. HTTP 접속이라 `APP_SECURE_COOKIES=false` |

**설치하지 않음**: Katib, Trainer, Spark Operator, Hub(Model Registry/Catalog), Workspaces(Notebooks 2.0), Notebooks v1의 Jupyter · PVC Viewer, KServe Models Web App, Knative Eventing, Ray.
Tensorboard 2종은 Notebooks v1 묶음(`applications/notebooks-v1/upstream/`)에서 이 둘만 골라 설치한다. Kubeflow 26.10.0은 Notebooks v1을 구버전 계열로 분류하므로 이후 버전에서는 설치 경로가 바뀔 수 있다.

문제가 생기면 `KF_VERSION=26.03.1`로 되돌린다 (EKS 1.36 그대로 사용 가능).

### 2.3 MLOps 구성 요소
| 제품 | 버전 | 비고 |
|---|---|---|
| MLflow 서버 | **3.17.2** | 공식 이미지에 psycopg2가 없어 `images/mlflow`로 빌드 (3.18.0은 2026-11-03 출시라 제외) |
| PostgreSQL | **17.11** (공식 `postgres` 이미지) | StatefulSet, PVC 20 GiB |
| BuildKit | **v0.34.1-rootless** | 클러스터 내 데몬. Kaniko는 2025년 아카이브. v0.35.0은 2026-11-11 출시라 제외 |
| BentoML | **1.4.40** | |

### 2.4 ML 라이브러리
| 제품 | 버전 | 비고 |
|---|---|---|
| Python | 3.12 | |
| torch / torchvision | **2.14.1 / 0.29.1** | 같은 날 출시된 짝. torch 2.15.0용 torchvision은 아직 정식 짝이 없어 제외 |
| torchmetrics / pycocotools | 1.9.0 / 2.0.11 | mAP 계산 (4주차) |
| easyocr | 1.7.2 | 한국어 OCR (4주차) |
| **tensorboard** | **2.21.0** | `SummaryWriter` 로깅 (5주차). 학습 이미지 `idcard-ml`에 추가 |
| kfp / kfp-kubernetes | 2.18.0 / 2.18.0 | 백엔드와 같은 버전 |
| mlflow (클라이언트) | 3.17.2 | 서버와 같은 버전 |
| kubernetes (Python) | 30.1.0 | kfp 2.18.0 의존성 상한 |
| boto3 / faker / pillow / numpy | 1.44.2 / 40.54.0 / 13.0.0 / 2.5.4 | |

### 2.5 로컬 도구
AWS CLI v2, kubectl 1.36.x, kustomize v5.x, Helm 3.x, Docker + buildx, envsubst(gettext), jq, uv(선택)

## 3. 실습 범위

### 3.1 신분증 인식 (4주차)

| 단계 | 내용 | 방식 |
|---|---|---|
| ① 종류 분류 + 필드 영역 검출 | 주민등록증 / 운전면허증 / 여권 + 이름·번호·주소·발급일·사진 영역 | **직접 학습**: torchvision `fasterrcnn_mobilenet_v3_large_fpn` (Apache-2.0), 입력 짧은 변 640 |
| ② 필드 텍스트 인식 | 검출 영역 크롭 → OCR (번호·날짜는 허용 문자 제한) | EasyOCR `ko`+`en` |
| ③ 후처리 | 숫자 OCR 혼동 보정, 형식 검증, **모든 필드에서 주민번호 패턴 마스킹** | 규칙 기반 |

검출 클래스: `card_rrc`, `card_driver`, `card_passport`, `field_name`, `field_id_number`, `field_address`, `field_issue_date`, `field_photo`

**데이터는 합성만 사용**한다. PIL + Faker(ko_KR)로 가짜 카드를 그리고(주민번호 성별 자리 0 → 실재 불가, "SAMPLE" 워터마크), 무작위 배경 합성·회전(±12°)·밝기/대비·블러·노이즈를 적용한다. COCO 형식으로 `s3://<bucket>/datasets/idcard/<version>/{train,val}.tar.gz`에 저장한다.

### 3.2 영화 추천 (5주차)

| 항목 | 내용 |
|---|---|
| 데이터 | MovieLens `ml-latest-small` (평점 100,836건, 사용자 610명, 영화 9,724편). 라이선스상 재배포 불가 → 실행 시 내려받음 |
| 피드백 | 평점 값은 쓰지 않고 "봤다"는 사실만 사용(암묵적 피드백) |
| 분리 | 사용자마다 가장 마지막에 본 영화 1편을 검증용으로 분리(leave-last-out) |
| 모델 | BPR 행렬 분해 — 사용자 · 영화 임베딩 내적 + 영화 편향 (`ml/src/movierec/model.py`) |
| 실험 | 임베딩 차원 16 / 32 / 64, 20 epoch, lr 0.005, 배치 1,024 |
| 평가 | 정답 1편 + 보지 않은 영화 99편 중 순위 → HR@10, NDCG@10 (무작위 기준 HR@10 = 0.10) |
| TensorBoard 기록 | Scalars(손실 · 검증 지표), Histograms(임베딩 · 편향 분포), Projector(인기 영화 1,500편 + 제목 · 장르), Text(예시 사용자 추천 결과), HParams |

## 4. 아키텍처

```
 사용자 (180.224.237.157/32만)
   │ HTTP :80
   ▼
 ALB (AWS LBC, inbound-cidrs 제한, target-type ip, group.name = 클러스터 이름)
   │
   ▼
 istio-ingressgateway ── 인증: 브라우저=oauth2-proxy+Dex 쿠키 / 스크립트=EKS SA 토큰(Istio RequestAuthentication)
   ├─ /                 Central Dashboard (메뉴에 Tensorboards 추가)
   ├─ /_/pipeline/      KFP UI,  /pipeline  KFP API
   ├─ /mlflow/          MLflow (VirtualService, --static-prefix=/mlflow)
   ├─ /tensorboards/    tensorboards-web-app (인스턴스 목록 · 생성)
   ├─ /tensorboard/<ns>/<이름>/   Tensorboard 인스턴스 (컨트롤러가 만든 VirtualService)
   └─ /serving/kubeflow-user-example-com/idcard-recognizer/*  KServe → BentoML 컨테이너

 EKS 1.36 (클러스터 mlops)
 ├─ 관리형 노드그룹 m6i.xlarge × 2 (node-role=cpu, 온디맨드): Istio, Dex, KFP, MLflow, PostgreSQL,
 │     KServe 추론 파드, Karpenter 컨트롤러, 영화 추천 학습 파드, Tensorboard 파드
 ├─ Karpenter NodePool gpu   (node-role=gpu, taint nvidia.com/gpu, Spot 우선): 신분증 학습 파드 — 있을 때만
 └─ Karpenter NodePool burst (node-role=burst, taint workload=batch, Spot 우선): 신분증 파이프라인 단계 — 자리가 없을 때만

 Pod Identity: mlflow/mlflow → S3 mlflow/*,  kubeflow-user-example-com/default-editor → S3 datasets/*, ECR push,
               kube-system/karpenter → EC2 · SQS(중단 알림)
 ECR: mlops/idcard-ml, mlops/idcard-builder, mlops/mlflow-server, mlops/idcard-recognizer
 PVC (kubeflow-user-example-com): movierec-tb 5Gi — 영화 추천 학습 파드가 쓰고 Tensorboard 파드가 읽음
```

### 노드 운영 (Karpenter, 기본)
- 관리형 노드그룹은 일반 노드(`node-role=cpu`, 온디맨드) 2대만 둔다. 플랫폼 파드와 Karpenter 컨트롤러가 여기서 돈다.
- NodePool `gpu`: `nvidia.com/gpu` taint + `node-role=gpu` 라벨. 학습 파드가 Pending이 되면 뜨고, 비면 5분 뒤 사라진다. GPU 상한 1개.
- NodePool `burst`: `workload=batch` taint. 파이프라인 단계가 일반 노드에 들어가지 못할 때만 뜬다(vCPU 상한 8). 관리형 GPU 노드가 맡던 여유 자원을 대신한다.
- 플랫폼 파드는 taint 때문에 Karpenter 노드로 가지 않는다. MySQL·PostgreSQL이 Spot 회수로 중단되는 일을 피하기 위해서다.
- Spot 회수에 대비해 신분증 학습 단계는 1회 재시도한다. Spot 중단 알림은 SQS + EventBridge로 받는다.
- 영화 추천 학습 파드와 Tensorboard 파드는 **일반 노드에만** 둔다(burst 허용을 넣지 않음). Tensorboard 파드는 컨트롤러가 만들어 taint 허용을 넣을 수 없으므로, 학습 파드만 임시 노드로 가면 RWO 볼륨을 함께 쓸 수 없다.
- `enable_karpenter = false`로 두면 아래의 이전 구성(관리형 GPU 노드 1대 상시)으로 돌아간다.

### 이전 구성: 관리형 GPU 노드 상시 운영 (`enable_karpenter = false`)
- GPU 노드에 `NoSchedule` taint를 걸지 않아 플랫폼 파드가 두 노드에 나뉘어 올라간다. 학습 파드만 GPU를 요청하고 `node-role=gpu`에 고정된다.
- 그래서 GPU 노드는 0대로 줄일 수 없다. 비용은 실습 후 `make destroy`로 관리한다.
- GPU 노드(할당 가능 CPU 3.92)의 CPU를 플랫폼·추론 파드와 나눠 쓰므로 학습 파드는 CPU 요청 1코어(limit 없음)로 둔다. 2코어 요청 시 EKS에서 GPU가 비어 있는데도 Pending이 되었다.

### 서빙 구조 (4주차)
- **BentoML**: 검출 모델(state_dict) + EasyOCR 가중치를 BentoML 모델 하나로 묶고, 서비스(`/recognize`, `/recognize_b64`, `/readyz`)와 함께 Bento로 빌드한다. 서빙 이미지는 CPU 전용 torch를 쓴다.
- **이미지 빌드**: 파이프라인 파드가 `bentoml containerize`와 같은 방식으로 빌드 컨텍스트(모델 포함)를 만들고, `buildctl`로 BuildKit 데몬에 보내 ECR에 푸시한다. ECR 인증 토큰은 파드의 Pod Identity로 발급한다.
- **KServe**: 커스텀 predictor 컨테이너(포트 3000), Knative 서버리스 모드, `minReplicas: 1`. 두 번째 배포부터 `canaryTrafficPercent: 10` → 스모크 테스트 통과 시 100%.
- Knative가 ECR 이미지 태그를 다이제스트로 바꾸려다 인증 실패하지 않도록 `registries-skipping-tag-resolving`에 ECR 레지스트리를 추가한다.

### Tensorboard 구조 (5주차)
- `make tensorboard`가 컨트롤러 · 웹 앱 설치, 대시보드 메뉴 추가(`dashboard-config`), 로그용 PVC `movierec-tb`(5Gi, gp3) 생성을 한다.
- 인스턴스는 `Tensorboard` 리소스 하나(`logspath: pvc://movierec-tb/movierec`). 컨트롤러가 같은 이름의 Deployment(PVC 읽기 전용) · Service(80 → 6006) · VirtualService를 만든다.
- 로그 PVC가 EBS(ReadWriteOnce)라 **학습 먼저, 인스턴스는 나중**이다. 인스턴스가 떠 있으면 `make movierec`이 제출을 거부한다(`FORCE=1`로 무시).
- 컨트롤러 설정 `RWO_PVC_SCHEDULING=True`: PVC를 쓰는 실행 중 파드가 있으면 그 노드를 선호해 배치한다(preferred).

## 5. 학습 파이프라인 (KFP v2)

### 5.1 신분증 인식 (`pipelines/idcard_pipeline.py`)

```
[data-gen] → [validate-data] → [train (GPU)] → [evaluate] ─┬─ mAP@0.5 ≥ map50_threshold(0.6)
                                                           │      ▼
                                                           │  [register-model] → [bento-build] → [image-build]
                                                           │      → [deploy-kserve] → [smoke-test] → [promote]
                                                           └─ 미달: 종료
```

| 컴포넌트 | 이미지 | 내용 |
|---|---|---|
| data-gen | idcard-ml | 합성 데이터 생성 → S3 (기본 3,000 / 500장) |
| validate-data | idcard-ml | 클래스 분포·박스 유효성 → Markdown 리포트, 실패 시 중단 |
| train | idcard-ml | GPU 1개(Karpenter `gpu` NodePool), CPU 요청 1코어, 1회 재시도, MLflow에 파라미터·손실·state_dict·모델 기록 |
| evaluate | idcard-ml | mAP, mAP@0.5, 클래스별 AP, 종류 정확도 → KFP Metrics + MLflow |
| register-model | idcard-builder | MLflow Registry `idcard-detector`, alias `staging` |
| bento-build | idcard-builder | Bento 빌드 → KFP 아티팩트 |
| image-build | idcard-builder | BuildKit → ECR `idcard-recognizer:v<버전>` (레지스트리 캐시 사용) |
| deploy-kserve | idcard-builder | 최초 생성 또는 카나리 패치, Ready + 트래픽 비율 대기 |
| smoke-test | idcard-builder | 게이트웨이 경유 6회 추론, 종류 정확도·마스킹 확인 |
| promote | idcard-builder | 카나리 100%, alias `production` |

모든 단계에 `workload=batch` 허용을 넣어, 일반 노드에 자리가 없으면 Karpenter `burst` 노드로 갈 수 있게 한다.

### 5.2 영화 추천 (`pipelines/movierec_pipeline.py`)

```
[prepare-data] ──Dataset──▶ [train-sweep] ──▶ PVC movierec-tb : movierec/<실행 이름>-dim<N>/
 MovieLens 내려받기,            차원 16 → 32 → 64를        └──▶ MLflow 실험 movierec
 통계 리포트                    한 파드에서 차례로 학습
```

| 컴포넌트 | 이미지 | 내용 |
|---|---|---|
| movierec-prepare | idcard-ml | MovieLens 내려받기 → KFP Dataset 아티팩트, 통계 Markdown 리포트 (캐시 사용) |
| train-sweep | idcard-ml | CPU 1코어. PVC를 `/tensorboard`에 마운트, 차원별로 학습하고 TensorBoard 로그 · MLflow 지표 기록 |

차원마다 파드를 나누지 않는다(`dsl.ParallelFor`에서 변경). 로그 PVC가 RWO라 파드가 다른 노드로 가면 볼륨을 뗐다 붙여야 하기 때문이다.

모든 파이프라인 파드에는 Istio 사이드카를 넣지 않고(`sidecar.istio.io/inject: "false"`), 이미지는 매번 새로 받는다(`imagePullPolicy: Always`).

## 6. 구현 결과 (디렉터리)

```
myeks/
├── PLAN.md  README.md  Makefile  .env.example  .gitignore
├── infra/terraform/      versions, providers, variables, main(vpc+eks), karpenter, storage(s3+ecr),
│                         iam(pod identity), k8s(sc+helm), outputs
├── platform/
│   ├── kubeflow/install.sh       필수 구성 설치 (issuer/HTTP 패치, Dex 비밀번호, Knative ECR 설정 포함)
│   ├── karpenter/                EC2NodeClass + NodePool(gpu, burst) 템플릿, 적용 · 상태 스크립트
│   ├── tensorboard/              컨트롤러 · 웹 앱 설치, 메뉴 패치, 로그 PVC, 인스턴스 생성 · 삭제
│   ├── ingress/                  ALB Ingress 템플릿 + 적용 스크립트
│   ├── mlflow/                   PostgreSQL + MLflow + VirtualService
│   ├── buildkit/, kserve/        BuildKit 데몬, AuthorizationPolicy
│   └── install-extras.sh
├── ml/
│   ├── src/idcard/       labels, fonts, datagen, dataset, model, train, evaluate, recognize, postprocess,
│   │                     storage, steps(학습 단계), ops(배포 단계)
│   ├── src/movierec/     data(MovieLens 내려받기 · 분리 · 후보 생성), model(BPR · 순위 지표), train(TensorBoard 로깅)
│   ├── serving/          service.py, bentofile.yaml
│   ├── tests/            datagen, postprocess, builder 이미지 import, movierec 단위 테스트 (27개)
│   └── requirements-{ml,builder,dev}.txt
├── pipelines/            components.py, idcard_pipeline.py, run.py, movierec_pipeline.py, run_movierec.py
├── images/               ml/, builder/, mlflow/ Dockerfile
└── scripts/              env, check-tools, build-images, test-infer, info, destroy
```

## 7. 검증 결과

### 7.1 로컬 (2026-11-13 ~ 11-15)

| 항목 | 결과 |
|---|---|
| 단위 테스트 | 27개 통과 (datagen, postprocess, builder 이미지 import 검사, movierec 7개) |
| 합성 데이터 생성 + 박스 시각화 | 3종 카드, 박스 위치 정확 |
| 신분증 학습 1 epoch(40장, CPU) → 평가 → MLflow 기록 | 성공 (MLflow 3.x 기본 `pt2` 저장 형식 문제 → `pickle`로 수정) |
| MLflow 등록 → Bento 빌드 → export/import → 빌드 컨텍스트에 모델 포함 | 성공 (Bento 디렉터리에 모델이 없는 1.4 동작 → `construct_containerfile` 사용으로 수정) |
| `bentoml serve` → `/recognize`, `/recognize_b64` | 성공, EasyOCR 한글 인식 확인 (마스킹 누락 경로 발견 → 모든 필드 마스킹으로 수정) |
| 영화 추천 학습 (3개 차원 × 20 epoch) | 13초, HR@10 0.703 / 0.715 / 0.720, TensorBoard 2.21.0에서 5개 탭 조회 |
| KFP 파이프라인 2종 컴파일 | 성공 (GPU, nodeSelector, toleration, /dev/shm, PVC 마운트 반영) |
| Kubeflow · Tensorboard kustomize 렌더링 | 성공, EKS issuer · `FORCE_HTTPS=false` · `APP_SECURE_COOKIES=false` 패치 반영 |
| Karpenter NodePool · EC2NodeClass | 차트 1.15.1 CRD 스키마 검증 통과 |
| Terraform init / validate / plan | 통과 (Karpenter 사용 108개, 미사용 93개 리소스) |

### 7.2 EKS — 4주차, 관리형 GPU 노드 구성 (클러스터 `idcard-mlops`, 2026-11-13 ~ 11-14)

| 항목 | 결과 |
|---|---|
| 신분증 파이프라인 (3 epoch · 600장) | 20/20 성공, 약 10분. mAP@0.5 0.9987, 종류 정확도 1.0, 카나리 10% → 100% |
| 신분증 파이프라인 (기본값 10 epoch · 3,000장) | 21/21 성공, 약 51분(학습 파드 `Insufficient cpu`로 20분 대기 포함 → v3.3에서 수정). mAP@0.5 1.0 |
| ALB 경유 추론 | HTTP 200, 약 0.5~1.0초/장, 주민번호 뒷자리 마스킹, 토큰 없이 403 |

EKS에서 고친 문제 17건(Istio 웹훅 포트, MLflow OOM · CORS · 리전, `/dev/shm`, Host 헤더 · JWT issuer 등)은 4주차 블로그 5장에 정리했다.

### 7.3 EKS — Karpenter + 5주차 (클러스터 `mlops`, 2026-11-16)

| 항목 | 결과 |
|---|---|
| 평소 노드 | 관리형 일반 노드 2대(a · c 영역 1대씩), Karpenter 노드 0대 |
| GPU 노드 | 학습 파드 Pending → Spot `g4dn.xlarge` 기동, 약 45초 뒤 준비 |
| 임시 CPU 노드 | 평가 단계부터 Spot `c8i-flex.xlarge`(약 33초) |
| 신분증 파이프라인 (기본값) | 20/20 성공, 28분 24초 |
| 노드 회수 | GPU 노드는 학습 종료 5분 뒤, 임시 노드는 2분 뒤 비우기 시작 → 일반 노드 2대로 복귀 |
| `make tensorboard` | 컨트롤러 · 웹 앱 Ready, 메뉴 추가, `/tensorboards/` 200, PVC는 `ap-northeast-2a`에 5 GiB EBS(태그 확인) |
| 영화 추천 파이프라인 | 5/5 성공, 3분 18초(이미지 받기 1분 33초 포함). HR@10 0.7033 / 0.7148 / 0.7197 — 로컬과 동일 |
| Tensorboard 인스턴스 | `make tb` 후 약 1분 만에 브라우저 접속, 7개 탭 조회. 뷰어 2.5.1이 2.21.0 로그를 정상 판독 |
| `make destroy` | 108개 리소스 삭제, 인스턴스 · EBS · ALB · NAT · S3 · ECR 잔존 없음 |

첫 실행에서 새 S3 버킷의 전역 주소가 `us-east-1`로 연결되어 evaluate의 모델 다운로드가 실패했다 → MLflow에 `MLFLOW_S3_ENDPOINT_URL`(리전 엔드포인트)을 넣어 해결.

**아직 검증하지 않은 것**: Spot 회수가 실제로 일어났을 때의 재시도와 온디맨드 대체, 대시보드 화면에서 Tensorboard 인스턴스 만들기(목록 화면까지만 확인), 학습 중 실시간 보기, 인스턴스가 떠 있을 때의 재학습, 뷰어 이미지 교체.
원인을 확인하지 못한 것: Tensorboard 파드가 생성 직후 한 번 교체되는 이유, GPU 노드가 비운 뒤 삭제되기까지 약 7분 걸린 이유.

## 8. 비용 (서울, 대략)

| 항목 | 시간당 (USD) |
|---|---|
| EKS 컨트롤 플레인 | 0.10 |
| m6i.xlarge × 2 (온디맨드) | 약 0.48 |
| NAT Gateway | 약 0.06 + 데이터 |
| ALB | 약 0.03 + LCU |
| GPU 노드 — Karpenter (기본) | 학습할 때만 Spot 요금, 평소 0 |
| GPU 노드 — `enable_karpenter = false` | `g6.xlarge` 온디맨드 약 1.0 (항상) |
| EBS gp3 (일반 노드 루트 200 GB + PVC 115 GB, Karpenter 노드 루트 100 GB는 떠 있는 동안만) | 월 약 25 |
| **합계 (Karpenter, 학습 없을 때)** | **시간당 약 0.7 → 하루 약 16** + 학습 시간만큼의 GPU Spot 요금 |
| **합계 (GPU 상시)** | **시간당 약 1.6 → 하루 약 40** |

GPU Spot 시세 (서울, 2026-11-15 조회, 시간당): `g4dn.xlarge` 약 0.33 · `g6.xlarge` 약 0.47 · `g5.xlarge` 약 0.60.
온디맨드 `g6.xlarge` 약 1.0과 비교하면 절반 이하이고, 학습이 없으면 0이다. 5주차 영화 추천 실습은 GPU를 쓰지 않으므로 GPU 요금이 붙지 않는다.
위 금액은 단가를 더한 추정이며 실제 청구액이 아니다.

## 9. 위험 요소와 대응

| 위험 | 대응 |
|---|---|
| 일반 노드 리소스 부족 (플랫폼 · 영화 추천 학습 · Tensorboard 파드 Pending) | 필수 구성만 설치. 그래도 부족하면 `cpu_node_count = 3` 또는 `m6i.2xlarge` (이 파드들은 Karpenter 노드로 가지 않음) |
| Spot 용량 부족·회수 | 온디맨드 대체 허용(`gpu_capacity_types`), 3개 GPU 계열 허용, 신분증 학습 단계 1회 재시도 |
| Karpenter 노드가 정리되지 않고 남음 | `scripts/destroy.sh`가 NodePool을 먼저 지우고, 남은 인스턴스를 태그(`Project`, `ManagedBy=karpenter`)로 찾아 종료 |
| 새 S3 버킷에서 presigned URL 다운로드 실패 | MLflow `MLFLOW_S3_ENDPOINT_URL`로 리전 엔드포인트 고정 |
| TensorBoard 로그 PVC(RWO) 충돌 (`Multi-Attach error`) | 학습 먼저 · 인스턴스 나중. 인스턴스가 있으면 `make movierec` 제출 거부, 학습 파드는 일반 노드에만 배치 |
| TensorBoard 로그 PVC의 가용 영역 고정 | 볼륨이 만들어진 영역의 일반 노드에만 Tensorboard 파드가 갈 수 있음. 자리가 없으면 일반 노드 증설 |
| Tensorboard 웹 앱 HTTP 쿠키 | `APP_SECURE_COOKIES=false` 패치 (운영 시 HTTPS + 기본값 유지) |
| Notebooks v1(Tensorboard) 구버전 계열 | 이후 Kubeflow 버전에서 설치 경로 변경 가능 — 업그레이드 시 재확인 |
| (관리형 GPU 노드 구성) GPU 노드 CPU 부족으로 학습 파드 Pending | 학습 파드 CPU 요청 1코어. 그래도 Pending이면 `kubectl describe pod`의 `Insufficient cpu` 확인 후 GPU 노드의 추론 파드 위치 확인 |
| 노드그룹을 수동으로 늘린 채 방치 | `kubectl get nodes -L node-role`로 확인, README의 CLI 절차로 Terraform 값에 맞춤 |
| GPU 인스턴스를 제공하지 않는 AZ | Terraform이 두 인스턴스 타입을 모두 제공하는 AZ를 골라 서브넷 생성 |
| Kubeflow 26.10.0 출시 직후 문제 | `KF_VERSION=26.03.1`로 재설치 |
| HTTP 로그인 쿠키 | oauth2-proxy `FORCE_HTTPS=false` 패치 |
| 허용 IP 변경 시 접속 불가 | `allowed_cidrs` 수정 후 `make infra && make ingress` (EKS API 엔드포인트도 같은 제한) |
| destroy 시 ALB/ENI/EBS 잔존 | `scripts/destroy.sh`가 Ingress·PVC를 먼저 삭제하고 남은 EBS를 출력 |
| BuildKit TCP 무인증 노출(클러스터 내부) | 실습 범위로 한정. 운영 시 mTLS 또는 NetworkPolicy 적용 필요 |
| 개인정보 | 합성 데이터만 사용, 응답 전 필드 마스킹. MovieLens는 공개 데이터셋(재배포하지 않음) |
| KServe predictor 접근 정책을 모두 허용 | 게이트웨이 인증은 유지됨. 운영 시 principal 기반으로 좁힐 것 |
