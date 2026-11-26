# EKS 기반 ML 플랫폼 실습 — 구현 계획서 (v5 초안)

> 최초 작성: 2026-11-13 · 최종 수정: 2026-11-27 · 상태: v5.1 (4주차 · 5주차 EKS 검증 완료 · 릴리즈 `v1.0.0`, **6주차 로컬 · EKS 검증 완료 — 7.4절 · 10장**)
> 실행 방법은 [README.md](README.md), 해설은 4주차 · 5주차 블로그 글(README 맨 위 링크)

| 주차 | 실습 | 핵심 구성 |
|---|---|---|
| 4주차 | 신분증 인식 MLOps 파이프라인 | Kubeflow Pipelines · MLflow · BentoML · KServe, GPU 학습 |
| 5주차 | Kubeflow Tensorboard + 영화 추천 | Tensorboard 컨트롤러 · 웹 앱, MovieLens BPR 학습, TensorBoard 로깅 |
| **6주차** | **LLM RAG — 개인정보 보호법 Q&A** | KServe + vLLM GPU 서빙, bge-m3 임베딩, pgvector, KFP 색인 파이프라인, MLflow 평가 · 추적 |
| 공통 | 비용 절감 | Karpenter — GPU 노드는 학습 때만 Spot으로 (6주차 서빙은 온디맨드, 실습 중에만) |

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
| v4 | 11-15 ~ 11-20 | **5주차 실습 추가**(Tensorboard 컨트롤러 · 웹 앱, MovieLens 영화 추천, 로그용 PVC). 클러스터 이름 `idcard-mlops` → `mlops`. MLflow presigned URL을 리전 엔드포인트로 고정(`MLFLOW_S3_ENDPOINT_URL`). Karpenter · 5주차 실습 EKS 검증. 릴리즈 `v1.0.0` |
| v5 초안 | 11-25 | **6주차 설계 추가**(10장): 개인정보 보호법 Q&A RAG(원본은 미리 내려받은 법률 PDF). vLLM을 KServe로 GPU 서빙(서빙 전용 온디맨드 NodePool), bge-m3 + pgvector, 색인 KFP 파이프라인(평가 통과 시 색인 승격), MLflow 추적. 브랜치 `feature/week6-llm-rag` |
| **v5.1** | 11-25 ~ 11-27 | **6주차 구현 · 검증**: 로컬(PDF 파싱 · 청킹 · bge-m3 + pgvector · 평가 세트 40문항 · mlx-lm 생성 평가) → EKS 전 단계 실행(7.4절). EKS에서 고친 것 5건(rag-api 스키마, KServe CPU 상한, `kubectl wait` observedGeneration, 로컬 Docker 디스크, `terraform init -upgrade` 제거). 설계 정정: L4에서 vLLM은 가중치 전용 FP8, Istio 네이티브 사이드카라 UID 1337 불필요 |

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

**6주차 — LLM RAG (설계, 상세는 10장)**
- `make llm` → LLM 가중치를 S3에 캐시, KServe + vLLM 배포(서빙 전용 GPU 노드 기동)
- `make rag-data` → 개인정보 보호법 PDF를 S3에 업로드, `make rag-index` → PDF 파싱 → 조문 청킹 → 임베딩 → pgvector 적재 → 평가 → 기준 통과 시 색인 승격
- `make rag-ask Q="..."` → ALB 경유 질의, 근거 조문 인용이 포함된 답변 반환

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

### 7.4 EKS — 6주차 LLM RAG (클러스터 `mlops`, 2026-11-26 ~ 27, 한국 시간)

| 항목 | 결과 |
|---|---|
| `make infra` (21:51 ~ 22:03) | 일반 노드 2대, NodePool `gpu-serving`(온디맨드 · `g6.xlarge` · GPU 상한 1), NVIDIA device plugin affinity `node-role In [gpu, gpu-serving]`, Pod Identity `law-llm` · `rag-api`, ECR `mlops/rag` |
| `make images` (22:32 ~ 22:54) | 22분. 첫 시도는 로컬 Docker 디스크 부족으로 실패 → 정리 후 재시도 |
| 플랫폼 (22:58 ~ 23:06) | 약 8분, 모든 파드 Running |
| `make llm-cache` (23:13 ~ 23:18) | bge-m3 14개 2.30 GB 87초(HF 34.6초 · S3 18.6초), Qwen3-8B-FP8 12개 9.45 GB 4분 40초(HF 184.1초 · S3 73.9초) |
| `make rag-platform` | ragdb 정상. rag-api readiness 500(빈 DB에 테이블 없음) → 수정 · 재빌드 · 재시작 후 Ready(bge-m3 init 19 ~ 27초) |
| `make rag-index NO_GEN=1` (23:30 ~ 23:37) | 7분, 승격. embed 3분 41초(받기 · 로딩 22.4초 + 143청크 120.4초, CPU 2코어 — 로컬 22 ~ 41초보다 3 ~ 5배 느림, 임시 CPU 노드 c6i.xlarge Spot에서 실행). **청크 143 · Recall@5 0.9722 · MRR@5 0.8681 · 시행 예정 2/2 · 답 없음 기준 0.498 — 로컬과 같음** |
| `make llm` (23:41 ~ 23:52, 재적용 기준) | 처음엔 Deployment 거부(KServe 기본 cpuLimit 1) → `limits.cpu: 4` 수정 후 **약 11분**: GPU 노드 46초 · S3 가중치 75초 · vLLM 이미지(Docker Hub 9.74 GB) **4분 55초** · vLLM 로딩 약 3분 15초(가중치 71초, 컴파일 · CUDA 그래프 62초) |
| law-llm | 드라이버 580.178.04 + torch 2.14.0+cu130 호환, GPU 메모리 19.5 / 23 GiB(가중치 8.9 · KV 캐시 9.06 GiB = 65,968토큰, 8K 요청 동시 8.05개). **vLLM 0.33은 L4에서 가중치 전용 FP8(Marlin)**. Istio 네이티브 사이드카, Service `law-llm-predictor` |
| `make rag-ask` (23:56 ~ 23:58) | 200 OK. 첫 질의 12.0초(검색 8.4초 — 임베딩 초기화, 생성 3.6초), 두 번째 검색 0.16 · 생성 2.7초. 토큰 없이 403(`ext_authz_denied`). MLflow `law-rag` 트레이스 기록 |
| `make rag-index` (23:59 ~ 00:06) | 6분 21초. parse · chunk · embed **KFP 캐시 0초**, eval-retrieval 50초, eval-generation 3분 21초(40문항, rag-api 경유) → 새 색인 승격 · 이전 색인 교체(MLflow `promoted=true`) |
| 생성 평가 (Qwen3-8B-FP8) | 거절 1.0 · 잘못된 거절 0 · 인용 1.0 · 정답 조 인용 0.944 · 핵심 표현 0.628 · 시행일 명시 1.0 · 문항당 생성 4.4초(로컬 4bit 65초) |
| 답변 직접 판정 (36문항) | **정확 26 · 부분 5 · 오답 5** (로컬 4bit 23 · 6 · 7). 나아짐: q11 · q05 · q03 · q12 · q28 / 나빠짐: q14 / 그대로 오답: q02 · q08 · q25 · q31 · q35(숫자 · 날짜 · 원칙 · 검색 실패) |

**EKS에서 고친 것**

| # | 증상 | 원인 | 조치 (커밋) |
|---|---|---|---|
| ① | `make images` `No space left on device` | 로컬 colima Docker 디스크(98 GB) 가득 참 | 예전 이미지 · 참조 끊긴 빌드 캐시 정리(코드 변경 없음) |
| ② | AWS provider 6.73.0 → 6.74.0 | Makefile `terraform init -upgrade`가 lock 파일 무시 | lock 복원, `-upgrade` 제거 (`d69c5c4`) |
| ③ | rag-api readiness 500 | healthz가 `active_index` 조회, 스키마는 색인 때만 생성 | 시작 시 `init_schema` (`f4d070c`) |
| ④ | law-llm Deployment 거부 | 상한을 비우면 KServe 기본값(cpuLimit 1)이 들어가 요청 cpu 2보다 작음 | `limits.cpu: 4` + 상한 ≥ 요청 테스트 (`dacb86c`) |
| ⑤ | law-llm Ready인데 `make llm` 대기가 안 끝남 | 재적용 후 generation 2 · `status.observedGeneration` 1 → `kubectl wait --for=condition`이 무시 | 롤아웃 완료 + jsonpath Ready로 대기 (`install.sh`) |

③ · ④는 오프라인 검증(CRD 스키마)으로 잡을 수 없는 종류였다 — ④는 컨트롤러가 기본값을 채운 뒤에, ③은 빈 DB에서 띄워야 드러난다.

**아직 확인하지 않은 것**: law-llm AuthorizationPolicy의 거부 동작(rag-api 외 호출), 브라우저 질의 화면, Spot 서빙 노드, `make llm-down` · `make destroy` 후 잔존 자원.

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

## 10. 6주차 설계 — LLM RAG: 개인정보 보호법 Q&A (구현 · 로컬 · EKS 검증 완료)

> 브랜치 `feature/week6-llm-rag`. 로컬 검증(2026-11-25 ~ 26)과 EKS 검증(2026-11-26 ~ 27, 7.4절)을 마쳤다. main 머지 후 릴리즈 `v1.1.0`.

### 10.1 목표와 범위

"주민등록번호를 수집해도 되나요?" 같은 질문에 **개인정보 보호법 조문을 근거로 인용해** 답하는 RAG 서비스를 EKS에 올린다. 4주차(신분증 인식, 주민번호 마스킹)와 주제가 이어진다.

| 항목 | 결정 |
|---|---|
| 문서 | **미리 내려받은 PDF 1개**: `docs/개인정보 보호법(법률)(제21445호)(20260911).pdf` — 국가법령정보센터 출력본, 법률 제21445호, 2026-09-11 시행, 47쪽 |
| 데이터 경로 | 저장소의 PDF → `make rag-data`가 S3 `datasets/law/<법률번호>-<시행일>/source.pdf`로 올림 → 파이프라인이 S3에서 읽어 파싱. 외부 API는 쓰지 않는다 |
| 저작권 | 법률은 저작권법 제7조에 따라 보호 대상이 아니다 → 저장소 커밋 · S3 저장 가능 |
| LLM | **직접 GPU 서빙** — KServe + vLLM, 8B급 Instruct 모델 (10.2) |
| 임베딩 | `BAAI/bge-m3` (다국어, 1024차원), CPU 실행 |
| 벡터 저장소 | PostgreSQL 17 + pgvector, 클러스터 내부 StatefulSet |
| 답변 규칙 | 검색된 조문만 근거로 답하고 「개인정보 보호법」 제N조(제M항)를 인용한다. 근거가 없으면 "찾지 못함". 시행 예정 조문을 근거로 쓰면 시행일을 밝힌다. 모든 답변 끝에 "법률 자문이 아님" 안내 |
| 범위 밖 | 시행령 · 고시 · 판례 · 해석례(같은 방식으로 PDF를 추가하면 확장 가능), 재순위(reranker), 대화 기록(멀티턴), 미세조정, 다중 GPU, 스캔 PDF(OCR) |

### 10.2 설치 제품 버전 (2026-11-25 조회, 출시 2주 미만 제외 원칙 유지)

| 제품 | 고정 버전 | 상태 | 비고 |
|---|---|---|---|
| vLLM (서빙 이미지) | **`vllm/vllm-openai:v0.33.0`** | **EKS 확인** — 드라이버 580.178.04 + torch cu130 호환 | 0.34.0(11-16)은 2주 미만이라 제외. amd64 9.74 GB, Docker Hub에서 받기 4분 55초(기동의 가장 큰 병목 → ECR 복사 후보) |
| KServe | 0.21.0 (기존) | 확인 | 내장 Hugging Face 런타임(`kserve/huggingfaceserver:v0.21.0`)은 vLLM 0.24 계열이라 쓰지 않고, 4주차처럼 **커스텀 predictor 컨테이너**로 vLLM 이미지를 직접 지정한다. 배포 모드 값은 `Standard` · `Knative`(Kubeflow 26.10.0 매니페스트에서 확인, `RawDeployment`는 0.21 매니페스트에 없음) |
| aws-cli (init container) | **`amazon/aws-cli:2.40.1`** (11-11) | 확인 | S3 → emptyDir 가중치 받기. 2.40.2 이후는 2주 미만 |
| huggingface-hub / boto3 | **2.4.0** (10-29) / **1.44.2** (기존) | 확인 | 가중치 캐시 Job(`lawrag.cache`), 색인 Job의 S3 PDF 읽기 |
| 모델 (확인) | `Qwen/Qwen3-8B-FP8`(Apache-2.0) · `BAAI/bge-m3`(MIT) | Hugging Face API로 확인 | bge-m3는 `onnx/` · `imgs/`를 빼고 약 2.3 GB만 캐시 |
| LLM | **`Qwen/Qwen3-8B-FP8`** (기본) | **EKS 확인** | Apache-2.0. 가중치 8.9 GiB, L4에서 KV 캐시 9.06 GiB. **vLLM 0.33은 L4에서 FP8 연산이 아닌 가중치 전용 FP8(Marlin)을 쓴다** — 메모리 절약만 얻음. thinking 모드는 끈다(`chat_template_kwargs.enable_thinking=false`). 생성 평가 직접 판정 정확 26 · 부분 5 · 오답 5 |
| LLM 비교 후보 | `Qwen3.5-9B` (2026-03, Apache-2.0) | 구현 시 확인 | 평가 세트(10.6)로 기본 모델과 비교해서 좋으면 교체. 라이선스가 모델 크기별로 다른 Qwen3.8 계열은 제외 |
| 임베딩 모델 | `BAAI/bge-m3` | 확인 | MIT, 1024차원, 최대 8192 토큰, 100개 이상 언어 |
| sentence-transformers / transformers | **6.2.0** (10-21) / **5.22.2** (11-06) | 확인 (torch 2.14.1과 로컬 실행) | 임베딩 계산. 6.3.0(11-11)은 2주 미만이라 제외 |
| **pypdf** | **7.0.1** (11-10) | 확인 | PDF 텍스트 추출. 순수 Python이라 이미지가 가볍다. 7.1.0(11-13) · 7.1.1(11-18)은 2주 미만이라 제외 |
| pgvector 이미지 | **`pgvector/pgvector:0.8.6-pg17`** | 확인 (2026-08-13 빌드, 로컬 실행) | 0.8.7은 출시일이 2주 이내일 수 있어 보류. CVE-2026-3172(병렬 HNSW 빌드)는 0.8.2에서 수정됨. 반복 스캔(`hnsw.iterative_scan`) 사용 |
| psycopg[binary] / pgvector(Python) | **3.3.8** (11-04) / **0.5.1** (10-09) | 확인 | DB 접속, 벡터 타입. psycopg 3.3.9(11-12)는 2주 미만이라 제외 |
| openai(Python) | **3.49.0** (11-10) | 확인 (로컬 mlx-lm 서버 호출) | vLLM · mlx-lm OpenAI 호환 API 호출. 재시도 끔(`max_retries=0`), 타임아웃 `LLM_TIMEOUT` |
| FastAPI / uvicorn | **0.162.4** (11-10) / **0.54.0** (09-25) | 확인 (로컬 실행) | RAG API |
| (로컬 전용) mlx-lm / mlx | **0.32.2** / **0.32.4** | 확인 | Apple Silicon에서 OpenAI 호환 LLM 서버(`make llm-local`), 모델 `mlx-community/Qwen3-8B-4bit`(Apache-2.0). EKS에는 쓰지 않음. mlx 0.32.5는 11-25 출시라 제외 |
| MLflow | 3.17.2 (기존) | 확인 | GenAI tracing · 평가 |

### 10.3 아키텍처

```
 사용자 (허용 IP만) ── ALB ── istio-ingressgateway (Dex 쿠키 / SA 토큰 인증, 기존)
                                ├─ /rag/          rag-api (FastAPI, 질의 화면 + /rag/ask)
                                └─ /mlflow/       MLflow (트레이스 · 평가 결과 조회)

 kubeflow-user-example-com (Istio 사이드카)
 ├─ rag-api Deployment (일반 노드, CPU 1 · 메모리 4Gi)
 │    질문 → 주민번호 패턴 마스킹(4주차 postprocess 재사용) → bge-m3 임베딩 → pgvector 검색(top-k 5)
 │    → 프롬프트 조립 → vLLM 호출 → 인용 조문 검증 → 응답 + MLflow 트레이스
 │    bge-m3: init container(aws s3 sync)가 s3://<bucket>/models/bge-m3/ → emptyDir
 └─ InferenceService law-llm (KServe Standard 모드 = Deployment + Service, vLLM 컨테이너, GPU 1, 레플리카 1)
      init container(aws s3 sync, UID 1337): s3://<bucket>/models/qwen3-8b-fp8/ → emptyDir /mnt/models
      주소 http://law-llm-predictor.<ns>.svc.cluster.local/v1 — 클러스터 내부 전용, rag-api principal만 허용

 rag (사이드카 없음, mlflow 네임스페이스와 같은 방식)
 └─ ragdb StatefulSet (pgvector/pgvector, gp3 PVC 10Gi, 일반 노드)

 S3 datasets/law/21445-20260911/source.pdf ← make rag-data (저장소의 PDF)
 Karpenter NodePool gpu-serving (신규): node-role=gpu-serving, taint nvidia.com/gpu, 온디맨드 기본, g6.xlarge
```

**설계 결정**
- **vLLM은 KServe 커스텀 컨테이너로 띄운다.** 내장 런타임은 vLLM 버전을 고를 수 없다. OpenAI 호환 API(`/v1/chat/completions`)를 그대로 노출한다.
- **배포 모드는 Knative가 아니라 Standard.** (구현 단계에서 바꿈) 클러스터 내부 전용이라 scale-to-zero · 카나리가 필요 없고, Knative는 init container를 기본으로 막으며(`kubernetes.podspec-init-containers`), 첫 기동(이미지 10 GB + 가중치 9 GB)이 `progress-deadline`을 넘기기 쉽다. Standard 모드는 Deployment + Service(`<이름>-predictor`)만 만든다.
- **서빙 전용 NodePool을 따로 둔다.** 기존 `gpu` NodePool은 학습용(Spot 우선, 상한 1)이다. 서빙 파드가 거기에 올라가면 학습과 GPU를 다투고 Spot 회수 때 서비스가 끊긴다. `gpu-serving`은 온디맨드 `g6.xlarge`(L4, FP8 지원)로 고정하고 GPU 상한도 따로 1을 둔다. 실습 중 비용을 줄이려면 `llm_capacity_types = ["spot", "on-demand"]`로 바꿀 수 있다.
- **가중치는 S3에 캐시한다.** 노드가 새로 뜰 때마다 Hugging Face에서 내려받지 않도록 `make llm-cache`가 클러스터 안의 Job으로 한 번 복사한다(HF → `s3://<bucket>/models/`, 끝나면 `_COMPLETE.json`에 리비전 기록 → 같은 리비전은 건너뜀).
- **서빙 파드는 init container(aws-cli)로 S3에서 받는다.** (구현 단계에서 바꿈) KServe storage-initializer는 KServe 웹훅이 나중에 끼워 넣는 컨테이너라 Pod Identity 자격 증명이 주입되는지 장담할 수 없다. 직접 정의한 init container는 파드 생성 시점에 있으므로 확실히 주입된다.
- **init container는 UID 1337로 실행한다.** Kubeflow의 Istio CNI는 앱 init container의 트래픽도 사이드카로 돌리는데, 그 시점엔 사이드카가 아직 없어 S3에 닿지 못한다. istio-proxy UID(1337)는 가로채기에서 빠진다. emptyDir는 누구나 쓸 수 있어 권한 문제가 없다. **EKS 실측**: 이 클러스터의 Istio는 네이티브 사이드카(istio-proxy가 init container로 먼저 뜸)로 동작해 UID 1337 처리는 필요 없었다. 해는 없어 그대로 둔다.
- **NVIDIA device plugin을 서빙 노드에도 띄운다.** 기존 설정은 `node-role=gpu`에만 배치해서, `node-role=gpu-serving` 노드에는 `nvidia.com/gpu` 리소스가 생기지 않아 vLLM 파드가 영원히 Pending이 된다 → affinity를 `node-role In [gpu, gpu-serving]`으로 바꿈(`infra/terraform/k8s.tf`).
- **콜드 스타트에 대비한다.** 노드 기동(약 1분), vLLM 이미지 받기(약 10 GB), 가중치 받기 · 로딩을 합치면 첫 Ready까지 5 ~ 10분 걸릴 것으로 본다. `startupProbe`로 20분까지 기다리고, `make llm`은 30분까지 Ready를 기다린다.
- **임베딩은 rag-api 안에서 CPU로 돌린다.** 질의 1건 임베딩은 CPU로 충분하고, 색인 대상도 약 200 청크라 GPU가 필요 없다. 임베딩용 KServe를 따로 두지 않아 구성 요소가 하나 줄어든다.
- **pgvector는 MLflow DB와 분리한다.** 이미지가 다르고(pgvector 확장), 색인을 다시 만들 때 MLflow에 영향을 주지 않기 위해서다.
- **PDF를 원본으로 쓴다.** 외부 API 인증 · 호출 IP 등록이 필요 없고, 실습 결과가 같은 PDF에서 항상 재현된다. 대신 PDF 텍스트 정리(머리말 · 줄바꿈 · 시행 예정 조문 구분)를 직접 처리해야 하며, 이것이 RAG 데이터 준비 단계의 실습 포인트가 된다.
- **LangChain 같은 프레임워크는 쓰지 않는다.** 검색 → 프롬프트 → 호출 흐름을 코드로 직접 보여주는 것이 실습 목적에 맞고, 버전 고정 대상도 줄어든다.

### 10.4 PDF 파싱 · 청킹 · 색인

**PDF 확인 결과** (2026-11-25, `make rag-parse-local` 실측)

| 항목 | 내용 |
|---|---|
| 형식 | 텍스트 레이어 있음(OCR 불필요), 47쪽, 약 9.4만 자. 1쪽 머리에 `[시행 2026. 9. 11.] [법률 제21445호, 2026. 3. 10., 일부개정]` |
| 구조 | 장 10개(`제6장 삭제` 포함) · 절, 본문 조 136개(현행 125 · 시행예정 9 · 삭제 2), `부칙 <제21445호,2026. 3. 10.>` 아래 부칙 조 4개 |
| **시행 예정 조문** | 5개 조(제2조, 제7조의9, 제32조의2, 제37조의2, 제75조)가 **현행본과 시행 예정본으로 두 번** 실려 있고, 신설 조 4개(제28조의12 ~ 15, 인공지능기술 개발 특례)는 예정본만 있다. 예정본 끝에 `[시행일: 2027. 3. 9.] 제2조`처럼 표시가 붙는다(시행일 2027-03-09, 2027-07-01). 제32조의2 · 제75조는 일부(`제1항 단서`, `제2항제15호`)만 예정 |
| 삭제 조문 · 항 | 조: `제8조 삭제 <2020. 2. 4.>`, `제28조의6 삭제 <…>` (제목 괄호 없음). 항: `③ 삭제<2020. 2. 4.>` |
| 함정 | 본문 속 조 인용이 줄 첫머리에 오면 조 제목처럼 보인다(`제28조의8(…한정한다)을 적용하지`). 괄호 뒤에 공백/줄 끝이 오는지와 조 번호가 줄지 않는지로 거른다. 부칙 제4조(다른 법률의 개정) 안의 `제20조제4항…`도 같은 경우 |
| 길이 | 조 단위 중앙값 479자, 1,500자 초과 7개(제25조 1.6천 · 제31조 1.8천 · 제64조의2 3.4천 · 제75조 3.9천 자) |
| 잡음 | 쪽마다 `법제처 N 국가법령정보센터` + `개인정보 보호법` 머리말, 1쪽 담당 부서 연락처 8줄, 단어 중간 줄바꿈(`해\n당하는`), 개정 이력 꼬리표 `<개정 2014. 3. 24., …>` · `[본조신설 …]` |

**처리 단계**

| 단계 | 내용 |
|---|---|
| 추출 | pypdf로 쪽별 텍스트 → 머리말 · 꼬리말 · 1쪽 연락처 제거 |
| 메타데이터 | 1쪽 머리에서 법률 번호 · 공포일 · 시행일을 읽는다. 파일 이름과 다르면 중단 |
| 구조 분해 | 장 · 절 제목을 추적하면서 `^제N조(의M)?\(제목\)`으로 조를 나누고, `부칙` 이후는 부칙으로 따로 표시 |
| 시행 상태 | 같은 조 번호가 두 번 나오고 뒤의 것에 `[시행일: …]`이 있으면 앞 = `현행`, 뒤 = `시행예정(effective_from=…)`. 중복 없이 `[시행일]`만 있는 조도 `시행예정` |
| 정리 | 줄바꿈을 붙이되 항(①), 호(1.), 목(가.), 꼬리표 앞에서는 줄을 유지. 개정 이력 꼬리표는 본문에서 빼서 메타데이터 `history`로 보관(임베딩 잡음 제거) |
| 제외 | `삭제` 조문(본문 없음). 부칙은 포함(시행일 질문에 필요) |
| 청킹 | **조 단위**가 기본. 약 1,500자(약 800토큰) 초과면 **항 단위**로 나누고 조 제목을 각 조각에 반복. 항 하나가 그래도 길면(제75조) 호 묶음 단위 |
| 청크 머리말 | `[개인정보 보호법 제3장 제24조의2(주민등록번호 처리의 제한) 제1항]` (시행 예정이면 `· 2027-03-09 시행 예정` 추가) → 조문 번호로 묻는 질문의 검색률을 올린다 |
| 청크 메타데이터 | 장 · 절, 조 번호(`24의2`), 조 제목, 항 번호, 상태(현행/시행예정/부칙), 시행일, 법률 번호, 개정 이력, 원문 쪽 번호, 색인 버전 |
| 저장 | 테이블 `law_chunks(index_version, chunk_id, ..., embedding vector(1024))`, HNSW 인덱스(cosine). 서비스 중인 색인 버전은 `active_index` 테이블 한 줄로 관리 → 승격 = 이 값 교체 |
| 검색 | 벡터 유사도 top-k 5. 질문에 "제N조"가 있으면 해당 조문을 우선 포함(정규식 매칭). 같은 조의 현행 · 시행예정본이 함께 걸리면 둘 다 넘기고 프롬프트에서 구분 |

실측 규모: 색인 대상 조 134개(삭제 2개 제외) + 부칙 4개 → **청크 143개**(현행 134 · 시행예정 9), 본문 길이 최대 1,500자. 나뉜 조는 제64조의2(3개), 제75조(4개).

**시행 예정 청크 중복 제거**: 같은 조 · 같은 범위의 현행 청크와 본문이 같은 시행 예정 청크는 뺀다. 제75조 시행 예정본은 제2항제15호만 바뀌어 4개 중 그 청크 1개만 남는다(146 → 143개). 빼지 않으면 "과태료는 최대 얼마?"의 top-5가 현행 · 예정 같은 내용으로 채워졌다.

**로컬 실측** (2026-11-25, Apple Silicon CPU, `make rag-index-local`): bge-m3 첫 내려받기 포함 로딩 183초(캐시 후 6초), 143청크 임베딩 22 ~ 41초, 질의 1건 검색(임베딩 + pgvector) 0.08 ~ 0.15초. 표본 질문 10개 중 9개에서 정답 조문이 1위, 관련 없는 질문("점심 메뉴")의 최고 유사도는 0.43으로 관련 질문(0.6 이상)보다 낮다 → 생성 단계의 "찾지 못함" 기준값 후보(평가 세트로 결정).

### 10.5 질의 처리 (rag-api)

```
POST /rag/ask {"question": "...", "index_version": (선택, 평가용)}
 ① 마스킹: 주민번호 패턴을 질문 · 로그에서 가림 (idcard.postprocess 재사용)
 ② 검색: bge-m3 임베딩 → pgvector top-k 5 (+ 조문 번호 직접 매칭)
 ③ 프롬프트: 시스템 규칙(근거 조문만 사용, 인용 형식, 시행 예정이면 시행일 명시, 모르면 모른다고) + 검색된 조문 + 질문
 ④ 생성: vLLM /v1/chat/completions (temperature 0.2, max_tokens 768, thinking 끔)
 ⑤ 검증: 답변에서 인용(「…」 제N조)을 추출 → 검색된 조문에 없는 인용이면 citations_verified=false
 ⑤' 시행 예정 안내: 인용한 조의 시행 예정 청크가 검색됐는데 답변에 시행 연도가 없으면 코드가 안내 문장을 덧붙인다
 ⑥ 응답: {answer, refused, citations[(조, retrieved|referenced|unsupported)], citations_verified, retrieved[(조, 항, 상태, 시행일, 쪽, 본문)],
         top_score, skipped_llm, future_notice_added, timings, index_version, model, disclaimer}
```
- 질의 1건을 MLflow 트레이스 하나로 남긴다(실험 `law-rag`, `MLFLOW_TRACKING_URI`가 있을 때만). 질문은 ① 이후의 마스킹된 값만 기록하고, LLM에도 마스킹된 질문만 보낸다.
- **LLM 생략**: 벡터 유사도 최고값이 `RAG_MIN_SCORE`(기본 0.48, 10.6 검색 평가의 답 없음 기준값 0.498보다 약간 낮게) 미만이면 LLM을 부르지 않고 바로 "찾지 못함". 조문 번호를 직접 짚은 질문은 생략하지 않는다.
- **거절 판단**: 시스템 프롬프트가 정한 문장(`NOT_FOUND`)이 답변에 있으면 거절로 본다.
- **인용 검증 3단계**: retrieved(검색된 조) · referenced(검색된 조 본문이 언급한 조, 예: 제24조의2가 언급하는 제24조) · unsupported. 다른 법령 인용(「행정기본법」 제20조 등)은 검증 대상에서 뺀다.
- **시행 예정 안내를 코드로 보장**: 로컬 Qwen3-8B-4bit가 프롬프트 규칙 3을 어기고 제28조의12 답변에서 시행일을 빼먹었다. 시행 예정본만 검색됐으면 "시행될 예정인 조문", 현행 · 예정이 함께 검색됐으면(제2조처럼) "개정 내용이 시행 예정 — 근거 조문의 표시를 확인"이라고 붙인다.
- 로컬에서는 mlx-lm 서버(`make llm-local`)를 쓴다. vLLM과 같은 OpenAI 호환 API라 `LLM_BASE_URL` · `LLM_MODEL`만 바꾸면 EKS와 같은 코드로 돈다. Qwen3 thinking은 `chat_template_kwargs.enable_thinking=false`로 끄고, 그래도 `<think>`가 나오면 지운다.
- `GET /rag/`는 질문 입력과 근거 조문(원문 쪽 번호 포함) 펼쳐보기만 있는 단순 HTML 화면.
- 스트리밍은 1차 범위에서 제외(Knative · Istio 경유 SSE는 구현 후 확인).

### 10.6 색인 파이프라인 (`pipelines/rag_index_pipeline.py`, 구현 · EKS 검증)

```
[parse-pdf] → [chunk] → [embed] → [load-index] → [eval-retrieval] ─┬─ Recall@5 ≥ min_recall ─┬─ 생성 평가 켬: [eval-generation] ─┬─ 인용 정확도 ≥ 기준 → [promote]
 S3 PDF        조/항     bge-m3     새 index_version                 │                         │                                   └─ 미달: 종료
 (캐시)       (캐시)    (캐시)     (서비스 중 색인은 그대로)          │                         └─ 생성 평가 끔(NO_GEN=1): ──────────→ [promote]
                                                                    └─ 미달: 종료 (서비스 중 색인 유지)
```

| 컴포넌트 | 자원 | 내용 (로직은 `lawrag/steps.py`) |
|---|---|---|
| parse-pdf | 0.5 CPU · 1Gi | `pdf_uri`(S3) → `articles.jsonl` + `law.json` 아티팩트, 파싱 리포트. 양식 검사 실패 시 중단. 반환: PDF SHA-256 |
| chunk | 0.5 CPU · 1Gi | `max_chars`(1500) 기준 조 → 항 → 호 분할, 현행과 같은 시행 예정 청크 제거. 리포트(청크 수 · 길이 · 나뉜 조) |
| embed | 2 CPU · 4 ~ 6Gi | S3 `models/bge-m3/`를 받아 CPU 임베딩 → `embeddings.npy` |
| load-index | 0.5 CPU | `index_version = <법률번호>-<시행일>-<파이프라인 실행 ID 앞 8자>`로 적재. MLflow 실행 시작(params: PDF SHA-256, 법률 번호, 청크 수, 임베딩 모델) |
| eval-retrieval | 1 CPU · 3 ~ 5Gi | 평가 세트 40문항 검색 평가 → KFP Metrics · 리포트 · `index_versions.metrics` · MLflow. 반환: Recall@5 ≥ `min_recall`(0.8) |
| eval-generation | 0.5 CPU | 게이트웨이(`istio-ingressgateway`) 경유로 rag-api `/rag/ask`를 **`index_version` 지정**으로 호출 → 승격 전 색인으로 생성 평가. 파드 SA 토큰 사용(4주차 smoke-test와 같은 방식). 반환: 인용 정확도 ≥ `min_citation_accuracy`(0.9) |
| promote | 0.5 CPU | `active_index` 교체 → rag-api가 다음 질의부터 새 색인 사용. MLflow 태그 `promoted=true`, `replaced=<이전 버전>` |

- **캐시**: parse-pdf · chunk · embed는 KFP 캐시를 쓴다. 같은 PDF로 다시 돌리면 임베딩까지 건너뛰고 load-index부터 새로 한다. load-index 이후는 DB 상태를 바꾸므로 캐시하지 않는다.
- **이미지**: 모든 단계가 rag 이미지 하나(`kfp==2.18.0` 포함 — `install_kfp_package=False`라 실행기가 이미지에 있어야 함). 모든 단계에 사이드카 없음 · `workload=batch` 허용(burst 노드), DB를 쓰는 다섯 단계에 Secret `ragdb` → `RAGDB_PASSWORD`.
- **MLflow**: 실험 `law-rag-index`, 색인 버전마다 실행 하나(run name = 태그 `index_version`). 단계마다 같은 실행에 이어서 기록.
- **생성 평가 전제**: law-llm이 Ready여야 한다. `make rag-index`는 Ready가 아니면 제출을 거부하고 `NO_GEN=1`(검색 평가만)을 권한다. 생성 평가의 승격 기준은 인용 정확도뿐이다 — 핵심 표현 포함률 · 리포트의 답변 원문은 사람이 보고 판단한다(아래 생성 평가 실측의 "발견 1").
- **`make rag-index-job`과의 관계**: 같은 lawrag 코드를 Job 하나로 돌리는 간이판(검색 평가 기준만). 파이프라인을 쓸 수 없을 때를 위해 남긴다.
- **클러스터 없이 한 검증**: 컴파일 · DAG 구조 테스트(조건 · 캐시 · Secret), KFP 로컬 실행기(`SubprocessRunner`)로 parse-pdf → chunk 아티팩트 전달(143청크), 로컬 pgvector로 `steps` 전 과정(parse → chunk → embed → load → eval-retrieval → promote → 원래 색인으로 복구), MLflow 기록(sqlite), rag 이미지 `pip check`.

PDF의 SHA-256을 MLflow 실행 파라미터로 남겨, 어떤 원본으로 만든 색인인지 추적한다. 법이 개정되면 새 PDF를 `docs/`에 넣고 `make rag-data PDF=...` → `make rag-index`.

**평가 세트** (`ml/src/lawrag/eval/qa.jsonl`, **40문항** = 답 있음 36 + 답 없음 4, 2026-11-25 작성)
- 항목: `question`, `expected`(정답 조 키: `24의2`, 부칙은 `부칙1`), `expect_status`(시행예정 문항만), `points`(답변에 들어가야 할 핵심 표현)
- 종류: 상황 20 · 개념 11 · 복합 1 · 조문번호 1 · 시행예정 2 · 부칙 1 · 답없음 4. 질문은 법률 용어 대신 일상어로 쓴다("제휴사에 넘기려면")
- **평가 세트 자체 검사**(`test_lawrag_evaluate.py`): 정답 조가 PDF에 실제로 있는지, `points`가 정답 조 본문에 있는지(띄어쓰기 무시) 확인 → 생성 평가에서 `points` 포함률을 쓸 수 있다
- 평가 세트를 검색 결과에 맞춰 고치지 않는다. 놓친 문항은 개선 대상으로 남긴다

| 지표 | 계산 | 승격 기준 (초안) |
|---|---|---|
| Recall@5 | 정답 조문이 top-5 안에 있는 비율 | ≥ 0.80 |
| MRR@5 | 정답 조문 순위 역수 평균 | 기록만 |
| 인용 정확도 | 답변 인용 중 정답 조문 또는 검색된 조문에 있는 비율 | ≥ 0.90 (`eval_generation=true`일 때). 필요조건일 뿐 — 아래 실측 참고 |
| 핵심 표현 포함률 | 답변에 평가 세트 `points`가 들어 있는 비율(공백 · 가운뎃점 무시) | 하한은 EKS 모델 평가 후 결정 |
| 시행 상태 정확도 | 시행 예정 조문을 근거로 쓴 답변이 시행일을 밝힌 비율 | 기록만 |
| 거절 정확도 | 답이 없는 질문에 "찾지 못함"으로 답한 비율 | 기록만 |
| 근거 일치(LLM 판정) | MLflow GenAI 평가 + 판정 모델은 같은 vLLM 엔드포인트 | 기록만 — 자기 답을 자기가 채점하는 편향이 있어 기준으로 쓰지 않는다 |

**검색 평가 실측** (2026-11-25, 로컬 색인 `21445-20260911-local11252349`, bge-m3, k=5, `make rag-eval-local`)

| 지표 | 값 | 비고 |
|---|---|---|
| Recall@5 · Hit@5 | **0.972** | 36문항 중 35개. 승격 기준(≥ 0.80) 통과 |
| MRR@5 | 0.868 | 정답이 2 ~ 4위인 문항 6개 (q07 · q23 · q24 · q25 · q29 · q31) |
| 시행 상태 | 2/2 | q33 · q34 모두 시행 예정 청크 검색 |
| 놓친 문항 | q08 "고객 정보를 제휴사에 **넘기려면**" | 제17조(개인정보의 **제공**)가 7위. "제3자에게 제공하려면"으로 물으면 1위 → **일상어 ↔ 법률 용어 차이**. 개선 후보: LLM으로 질문을 법률 용어로 바꿔 검색(질의 재작성), 키워드 검색 병행 |
| 답 없음 기준값 | 0.498 (정확도 0.975) | 답 있음 최저 0.520(q03), 답 없음 최고 **0.542(u04 "주민등록증 재발급")** — u04가 제24조의2와 가까워 유사도만으로는 완전히 가를 수 없다 |

**결정**: 유사도 기준값은 "확실히 관련 없음"(약 0.50 미만)만 걸러 LLM을 부르지 않는 용도로 쓰고, 그 위의 질문은 검색된 조문을 보고 LLM이 "찾지 못함"을 판단하게 한다(프롬프트 규칙 + 생성 평가의 거절 정확도로 확인). 답 있음 · 없음 사이 간격이 0.04 ~ 0.05로 좁아 평가 세트가 커지면 다시 정한다.

**생성 평가 실측** (2026-11-26, 같은 색인, 로컬 `mlx-community/Qwen3-8B-4bit`, `make rag-gen-eval-local`, 리포트 `data/law/eval_generation.md`)

| 지표 | 값 | 비고 |
|---|---|---|
| 거절 정확도 (답 없음 4) | **1.0** | u01 ~ u03은 유사도 기준값 미만이라 LLM 생략, u04("주민등록증 재발급")는 LLM이 거절 |
| 잘못된 거절 (답 있음 36) | 0.0 | |
| 인용 정확도 | 1.0 | 인용한 조가 모두 검색된 조(또는 그 조가 언급한 조) |
| 정답 조 인용률 | 0.917 | 33/36 |
| 시행일 명시 (시행예정 2문항) | 1.0 | 2건 모두 코드가 붙인 안내 덕분. 시행 예정 안내는 전체 5건 붙음 |
| 핵심 표현 포함률 | **0.54** | 평가 후 q30 정답을 바로잡고 저장된 답변으로 다시 계산(처음 값 0.53) |
| 생성 시간 | 평균 65초 | 로컬 mlx의 긴 프롬프트 처리가 느림(약 1,900토큰 프롬프트에 40초 이상). 짧은 요청은 초당 약 60토큰 |

**답변을 직접 읽은 판정** (답 있음 36): **정확 23 · 부분 6 · 오답 7**
- 오답: q02 주민번호를 "받아도 되지만"(원칙 금지를 뒤집음), q05 바디캠 조건을 뒤섞음, q08 제휴사 제공에 국외 이전 조문(제28조의8) 적용(검색 실패 문항), q11 민감정보를 "대통령령으로 정하는 경우만"으로 답함, q25 삭제 요구에 처리정지 조문(제37조)으로 답함, q31 최대 과태료 3천만원(실제 5천만원), q35 시행일 9월 10일(실제 9월 11일 — "공포 후 6개월"을 모델이 계산)
- 부분: q03 · q12 · q17 · q18 · q28(300만원 누락) · q30(매출액 10%를 빼고 50억원만)

**발견**
1. **인용 정확도 1.0은 답이 맞다는 뜻이 아니다.** 인용 검증은 "검색된 조를 인용했는지"만 본다. 오답 7개 중 4개(q02 · q05 · q11 · q31)가 정답 조를 정확히 인용하고도 내용을 틀렸다. 승격 기준의 인용 정확도는 필요조건으로만 쓰고, **핵심 표현 포함률을 함께 본다**(직접 판정과 경향이 맞음: 오답 · 부분 문항 대부분이 0 ~ 50%).
2. **평가 세트도 틀릴 수 있다.** q30 "과징금 최대"의 정답을 제1항(매출액 3%, 20억원)으로 적었는데, 제2항(가중, 매출액 10%, 50억원)이 최대였다. 자동 검사는 핵심 표현이 정답 조에 "있는지"만 확인한다 → 문항 추가 시 "최대 · 최소 · 언제" 같은 질문은 조 전체를 읽고 정답을 정한다.
3. **숫자 · 날짜 오류가 많다**(q30 · q31 · q35). 개선 후보: 프롬프트에 "금액 · 기간 · 날짜는 조문 문구를 그대로 쓰고 계산하지 않는다", 법률 메타데이터(법률 번호 · 시행일)를 근거와 함께 넣기.
4. **원칙과 예외를 뒤집는 오류**(q02 · q11). 개선 후보: "원칙(금지/허용)을 먼저 쓰고 예외를 나열" 규칙.
5. 로컬 4bit 모델 결과다. EKS의 `Qwen3-8B-FP8`(vLLM)과 비교 후보 `Qwen3.5-9B`로 같은 평가를 돌려 비교한다 — 생성 품질 기준(핵심 표현 포함률 하한)은 그 결과를 보고 정한다.

### 10.7 인프라 · 플랫폼 변경 (구현 · EKS 검증)

| 대상 | 변경 |
|---|---|
| `infra/terraform/variables.tf` | `enable_llm_serving`(기본 true), `llm_capacity_types`(기본 `["on-demand"]`), `llm_instance_types`(기본 `["g6.xlarge"]`), `llm_gpu_limit`(기본 1). 6주차 실습 때는 `cpu_node_count = 3` 권장 |
| `infra/terraform/storage.tf` | ECR `mlops/rag` |
| `infra/terraform/iam.tf` | `rag_serving_pod_identity`: 프로필 네임스페이스의 `law-llm` · `rag-api` SA → S3 `models/*` 읽기. `pipeline_pod_identity`(default-editor)에 `models/*` 쓰기 추가(캐시 Job). `datasets/law/*`는 기존 `datasets/*` 권한으로 충분 |
| `infra/terraform/k8s.tf` | NVIDIA device plugin 배치: `nodeSelector node-role=gpu` → affinity `node-role In [gpu, gpu-serving]` |
| `infra/terraform/outputs.tf` | `karpenter_nodepool_settings`에 `llm_enabled` · `llm_capacity_types` · `llm_instance_types` · `llm_gpu_limit` |
| `platform/karpenter/llm-nodepool.yaml.tmpl` · `apply.sh` | NodePool `gpu-serving`(`make infra`가 함께 적용). 비면 5분 뒤 정리, `expireAfter: 720h`. `enable_llm_serving = false`면 지움 |
| `platform/rag/ragdb.yaml.tmpl` | 네임스페이스 `rag`: StatefulSet(pgvector, PVC 10Gi, 일반 노드) · Service. Secret `ragdb`는 install.sh가 `rag`와 프로필 네임스페이스에 같은 값으로 만든다 |
| `platform/rag/model-cache-job.yaml.tmpl` | 캐시 Job(rag 이미지, `python -m lawrag.cache`, default-editor, 사이드카 없음, emptyDir 30Gi) |
| `platform/rag/llm-isvc.yaml.tmpl` | SA `law-llm` · InferenceService `law-llm`(Standard, cluster-local, init container S3 받기, vLLM `--max-model-len=8192 --gpu-memory-utilization=0.90`, requests cpu 2 · 메모리 10Gi · GPU 1, `/dev/shm` 2Gi) · AuthorizationPolicy(rag-api principal만) |
| `platform/rag/rag-api.yaml.tmpl` | SA `rag-api` · Deployment(일반 노드, init container로 bge-m3 받기, requests cpu 1 · 메모리 3Gi) · Service · VirtualService `/rag/` · AuthorizationPolicy(ingressgateway principal만) |
| `platform/rag/index-job.yaml.tmpl` | 색인 Job(KFP 파이프라인 전 임시): S3 PDF → `lawrag.index load --gate-recall 0.8` → 기준 통과 시 승격 |
| `platform/rag/install.sh` | `data` · `cache` · `platform` · `index` · `llm` · `llm-down` · `ask` · `status` |
| `images/rag/Dockerfile` | Python 3.12 + CPU torch 2.14.1 + `requirements-rag.txt`. 로컬 arm64 빌드 2.7 GB, 컨테이너 안에서 파싱 · 검색 · rag-api 라우트 확인 |
| `scripts/` | `env.sh`(rag 이미지 · vLLM · aws-cli · pgvector 이미지, 모델 이름, `model_slug`), `build-images.sh rag`, `destroy.sh`(`rag` 네임스페이스 삭제 — NodePool은 기존 코드가 모두 지움), `info.sh`(rag 주소, law-llm이 떠 있으면 경고) |
| `docs/개인정보 보호법(법률)(제21445호)(20260911).pdf` | 저장소에 커밋(282 KB, 저작권 보호 대상 아님) |

**클러스터 없이 한 검증**: `terraform validate` · `fmt`, 템플릿 렌더링 → YAML 파싱 → InferenceService(Kubeflow 26.10.0의 KServe CRD) · NodePool(Karpenter v1.15.1 CRD) 스키마 검증(잘못된 값을 넣으면 거부되는 것도 확인), 템플릿 변수 ⊆ install.sh가 넘기는 변수(`test_platform_rag.py`), rag 이미지 로컬 빌드 · 실행.

**EKS 검증** (7.4절): 위 변경이 모두 의도대로 반영됐다. 실행 중 고친 것 — `llm-isvc.yaml.tmpl`에 `limits.cpu: 4`(KServe 기본 cpuLimit 1), rag-api 시작 시 스키마 생성, `install.sh llm`의 대기를 롤아웃 + jsonpath로(observedGeneration 문제), Makefile `terraform init`에서 `-upgrade` 제거. 일반 노드는 2대로도 전 과정이 돌았다(embed 단계만 burst 노드).

### 10.8 Makefile 타깃

**EKS** (순서대로)

| 타깃 | 내용 |
|---|---|
| `make images-rag` | rag 이미지 빌드(linux/amd64) · ECR 푸시 |
| `make rag-data` | `PDF=`(기본: 위 PDF) → 1쪽에서 법률 번호 · 시행일을 읽어 `s3://<bucket>/datasets/law/<번호>-<시행일>/source.pdf` 업로드 |
| `make llm-cache` | LLM · bge-m3 가중치 HF → S3 (캐시 Job 2개, 이미 있으면 건너뜀) |
| `make rag-platform` | ragdb + Secret + rag-api + 접근 정책 |
| `make rag-index` | 색인 KFP 파이프라인 제출(10.6). 생성 평가에 `make llm` 필요, 검색 평가만: `NO_GEN=1`, 기준: `MIN_RECALL=0.8` |
| `make rag-index-job` | 파이프라인 없이 Job 하나로 색인 (Recall@5 ≥ `GATE_RECALL`(0.8)이면 승격) |
| `make rag-compile` | 파이프라인 컴파일만 |
| `make llm` | InferenceService `law-llm` 배포 → GPU 노드 기동 → Ready 대기(최대 30분). 생성 평가가 있는 `make rag-index` 전에 |
| `make rag-ask Q="..."` | ALB 경유 질의(SA 토큰) + 토큰 없이 거부되는지 확인 |
| `make rag-status` | ragdb · rag-api · law-llm · 캐시/색인 Job · GPU 서빙 노드 · 색인 버전 |
| `make llm-down` | law-llm 삭제 → 5분 뒤 GPU 노드 정리 (**실습 후 꼭 실행**) |

**로컬**: `rag-parse-local` · `rag-db-local` · `rag-index-local` · `rag-search-local` · `rag-eval-local` · `llm-local` · `rag-api-local` · `rag-gen-eval-local`

`.env.example`에 `RAG_IMAGE_TAG` · `LLM_MODEL` · `EMBED_MODEL` · `VLLM_IMAGE` · `AWSCLI_IMAGE` · `RAGDB_IMAGE` (주석, 기본값은 `scripts/env.sh`).

### 10.9 디렉터리 (추가분)

```
docs/               개인정보 보호법(법률)(제21445호)(20260911).pdf
ml/src/lawrag/      pdfparse(추출 · 정리 · 조/부칙 분해 · 시행 상태), chunk(조 · 항 분할), embed, store(pgvector),
                    retrieve, prompt, answer(생성 · 인용 검증), evaluate, cache(HF → S3), steps(파이프라인 단계),
                    prepare · index(CLI), eval/qa.jsonl
ml/rag_api/         app.py(FastAPI), static/index.html
ml/tests/           test_lawrag_pdfparse(가짜 쪽 텍스트 경계 사례 + 실제 PDF로 조 수 · 시행 예정 9건 · 머리말 제거 확인),
                    test_lawrag_chunk · retrieve · evaluate · answer · steps(파이프라인 컴파일 · DAG 포함), test_platform_rag(템플릿)
pipelines/          rag_index_pipeline.py, run_rag.py
platform/rag/       10.7
images/rag/         Dockerfile
```

### 10.10 구현 순서와 검증 계획

1. **로컬** — PDF 파싱 · 청킹 단위 테스트(`make rag-parse-local`), 로컬 Docker pgvector에 색인(`make rag-index-local`), 평가 세트 작성 · 검색 평가(`make rag-eval-local`), Apple Silicon mlx-lm으로 Qwen3-8B-4bit를 띄워(`make llm-local`) rag-api 전체 흐름 확인(`make rag-api-local`) · 생성 평가(`make rag-gen-eval-local`)
2. **EKS 플랫폼** — `make infra`(gpu-serving NodePool) → `images-rag` → `rag-data` → `llm-cache` → `rag-platform` → `llm` — **완료(7.4절)**: 첫 Ready 약 11분, GPU 메모리 19.5 / 23 GiB, 짧은 요청 41토큰 1.4초. EKS 생성 평가(FP8)는 로컬 4bit보다 정확 23 → 26, 오답 7 → 5
3. **파이프라인** — `make rag-index NO_GEN=1`(첫 색인, 7분) → `make llm` 후 `make rag-index`(생성 평가 포함, 6분 21초, KFP 캐시로 parse · chunk · embed 0초) — **완료**. 미달 경로(`MIN_RECALL=0.99`)는 EKS에서 아직 확인하지 않음(로컬 · DAG 테스트로만)
4. **질의** — `make rag-ask` 200 OK(약 3초, 첫 질의 12초), MLflow 트레이스, 토큰 없이 403 — **완료**. 브라우저 `/rag/` 화면은 아직 확인하지 않음
5. **정리** — `make llm-down` 후 GPU 노드 0대, `make destroy` 잔존 자원 없음

기록할 값: 첫 Ready까지 시간, 질의 지연(검색 / 생성 분리), Recall@5 · 인용 정확도, 시간당 비용.

### 10.11 비용 (6주차 추가분, 서울, 대략)

| 항목 | 시간당 (USD) |
|---|---|
| GPU 서빙 `g6.xlarge` 온디맨드 (`make llm` ~ `make llm-down` 동안) | 약 1.0 (Spot으로 바꾸면 약 0.47) |
| 일반 노드 1대 추가 (`cpu_node_count = 3`) | 약 0.24 |
| ragdb PVC 10 GiB · S3 모델 캐시 약 12 GB | 월 1 미만 |
| **합계 (기존 + 6주차, LLM 기동 중)** | **시간당 약 2.0** — 4시간 실습이면 약 8 |

### 10.12 위험 요소와 대응

| 위험 | 대응 |
|---|---|
| GPU 서빙 노드를 켜 둔 채 방치 | `make llm-down` 안내를 README · `make info`에 표시. `make destroy`가 NodePool을 먼저 지움 |
| vLLM 이미지의 CUDA와 AL2023 NVIDIA AMI 드라이버 불일치 | **EKS 확인 — 문제없음**(드라이버 580.178.04, torch cu130). 버전을 올릴 때 다시 확인 |
| 첫 기동이 오래 걸림 | Standard 모드라 Knative 기한 없음. `startupProbe` 20분, 가중치 S3 캐시. **EKS 실측 약 11분, 그중 vLLM 이미지 받기 4분 55초** → vLLM 이미지를 ECR에 복사하면 줄어듦(다음 과제) |
| init container가 S3에 닿지 못함 (Istio CNI 가로채기) | **EKS 확인 — 문제없음**. Istio가 네이티브 사이드카로 동작해 UID 1337이 아니어도 됐다. 다른 Istio 설정의 클러스터를 위해 UID 1337은 유지 |
| g6.xlarge 메모리(16GiB) 부족으로 vLLM 파드가 뜨지 않거나 OOM | **EKS 확인 — 문제없음**(requests 10Gi · limits 13Gi, OOM 없음). 상한은 CPU · 메모리 모두 명시해야 한다(KServe 기본값 — 7.4절 ④) |
| Pod Identity 자격 증명이 init container에 없음 | **EKS 확인 — 문제없음**(law-llm · rag-api · 색인 Job 모두 S3 접근 성공) |
| FP8 모델의 한국어 품질 저하 | 평가 세트로 bf16(약 16 GB, `max-model-len` 8192로 제한)과 비교 |
| Qwen3 thinking 출력(`<think>`)이 답변에 섞임 | `enable_thinking=false`, 응답 후처리에서 태그 제거 |
| **현행 · 시행 예정 조문을 섞어 답함** | 청크에 상태 · 시행일 메타데이터와 머리말 표시, 프롬프트 규칙, 평가 세트에 시행 예정 질문 포함 |
| PDF 줄바꿈 처리로 띄어쓰기가 사라짐(`개인정보의\n처리` → `개인정보의처리`) | 검색 영향은 작다고 보고 평가 지표로 확인. 화면 표시용 원문은 줄바꿈을 살린 텍스트를 따로 보관 |
| 다른 형식의 PDF(다른 출력 양식, 스캔본)를 넣음 | parse-pdf 검사(조 번호 연속성, 중복 설명 여부, 1쪽 메타데이터)에서 중단. 스캔본은 범위 밖 |
| 법 개정으로 답이 바뀜 | 색인 버전에 법률 번호 · 시행일 포함, 응답에 `index_version` 표시. 새 PDF로 `make rag-data` → `make rag-index` |
| 모델이 근거 없는 조문을 지어냄 | 인용 검증(⑤) 결과를 응답에 표시, 평가 지표로 승격 차단 |
| 시행령 · 고시에 있는 내용("대통령령으로 정한다")을 물음 | 1차 범위 밖이라 "법률에는 위임만 있고 세부 내용은 시행령에 있음"으로 답하게 규칙화. 시행령 PDF 추가로 확장 |
| 첫 질의만 느림 | **EKS 실측**: 첫 질의 검색 8.4초(rag-api 임베딩 초기화), 이후 0.16초 → rag-api 시작 시 임베딩 미리 계산(다음 과제) |
| 법률 자문으로 오인 | 모든 응답 · 화면에 안내 문구. 블로그에도 명시 |
| 질문에 실제 개인정보 입력 | 주민번호 패턴 마스킹 후에만 로그 · 트레이스 기록. 입력 원문은 저장하지 않음 |
| 일반 노드 자원 부족 (rag-api 메모리 4Gi + ragdb) | `cpu_node_count = 3`. rag-api는 burst 노드로 보내지 않음(상시 서비스) |
| law-llm 접근 범위 | cluster-local + principal 기반 AuthorizationPolicy (4주차의 "모두 허용"보다 좁힘) |
