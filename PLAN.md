# EKS 기반 신분증 인식 MLOps 실습 — 구현 계획서 (v3)

> 작성일: 2026-11-13 · 상태: v3 (요구사항 확정, 코드 생성 완료) · 실행 방법은 [README.md](README.md)

## 0. 변경 이력

| 버전 | 내용 |
|---|---|
| v1 | 초안 |
| v2 | 리전·노드 사양·OCR 범위·IaC·MLflow DB·외부 접속 방식 확정, 설치 제품 버전 명시 |
| v3 | HTTP 접속 + 허용 IP 확정, Kubeflow 필수 구성만 설치, 버전 재검증(torch/torchvision 짝, MLflow 패치 버전 등), 구현 중 바뀐 설계 반영 |
| v3.1 | 허용 IP를 180.224.237.157/32로 변경, 모든 AWS 자원에 `Owner=sejkim` 태그 필수 적용 |
| v3.2 | 일반 노드를 `m6i.xlarge` × 2로 변경 (GPU 노드 `g6.xlarge` × 1 유지) |
| v3.3 | EKS 실측 반영: 학습 파드 CPU 요청 2 → 1 (taint 없는 GPU 노드에서 `Insufficient cpu`로 20분 Pending), 기본값 실행(10 epoch·3,000장) 통과 기록 |

### 확정된 요구사항
| 항목 | 결정 |
|---|---|
| 리전 | `ap-northeast-2` (서울) |
| 일반 노드 | `m6i.xlarge` × 2 — 리소스가 부족하면 `m6i.2xlarge`로 상향 허용 (Terraform 변수) |
| GPU 노드 | `g6.xlarge` × 1 (NVIDIA L4 24 GB) |
| 인식 범위 | **검출 모델만 학습**, 글자 인식은 EasyOCR 사전학습 모델 사용 |
| IaC | Terraform |
| MLflow DB | 클러스터 내부 PostgreSQL (StatefulSet + gp3 PVC) |
| 외부 접속 | AWS ALB, **HTTP(80)**, 접속 허용 IP **180.224.237.157/32** (ALB와 EKS API 엔드포인트 모두) |
| Kubeflow 범위 | **필수 서비스만** 설치 (2.2절) |
| 자원 태그 | **`Owner=sejkim` 필수** (+ `Project`, `ManagedBy`). 적용 경로는 아래 표 |

#### Owner 태그 적용 경로
| 자원 | 생성 주체 | 적용 방법 |
|---|---|---|
| VPC, 서브넷, NAT, EKS 클러스터, IAM 역할, S3, ECR, EKS 애드온 | Terraform | provider `default_tags` + 모듈 `tags` |
| 노드 EC2 인스턴스, 루트 EBS, ENI | EKS 관리형 노드그룹 | 모듈 `tags` → launch template `tag_specifications` |
| ALB, 대상 그룹, 보안 그룹 | AWS Load Balancer Controller | Helm `defaultTags.Owner` + Ingress `alb.ingress.kubernetes.io/tags` |
| PVC용 EBS (PostgreSQL, KFP MySQL/SeaweedFS, BuildKit 캐시) | EBS CSI 드라이버 | StorageClass `gp3`의 `tagSpecification_1: Owner=sejkim` |

## 1. 목표와 완료 기준

AWS EKS 위에 Kubeflow · MLflow · BentoML · KServe를 구성하고, 신분증 인식 모델을
학습 → 실험 추적 → 모델 레지스트리 등록 → 패키징 → 서빙까지 Kubeflow 파이프라인 하나로 자동화한다.
("KFServing"은 **KServe**로 이름이 바뀌었다.)

- `make infra` → VPC·EKS·노드그룹·S3·ECR·IAM·ALB 컨트롤러·NVIDIA 플러그인 생성
- `make images` → 컴포넌트 이미지 3종 ECR 푸시
- `make platform` → Kubeflow 필수 구성·ALB·MLflow·BuildKit 설치, ALB 주소로 대시보드/MLflow 접속
- `make pipeline` → GPU 학습 → MLflow 등록 → Bento 이미지 빌드 → KServe 배포(2회차부터 카나리) → 스모크 테스트 → 승격
- `make test-infer` → ALB 경유 추론, 신분증 종류 + 필드 JSON 반환(주민번호 뒷자리 마스킹)
- `make destroy` → ALB/EBS 정리 후 전체 삭제

## 2. 설치 제품 버전

> 2026-11-13에 각 프로젝트 공식 릴리스 페이지/PyPI/GitHub API로 확인했다. 출시 2주 미만 버전은 피하고 직전 버전으로 고정했다.

### 2.1 인프라 / 클러스터
| 구분 | 제품 | 고정 버전 | 비고 |
|---|---|---|---|
| IaC | Terraform CLI | `>= 1.10` (검증 1.14.8, 최신 1.17.2) | |
| IaC | AWS / Helm / Kubernetes Provider | 6.73.0 / 3.3.0 / 3.5.0 | `.terraform.lock.hcl`에 고정 |
| IaC | terraform-aws-modules/vpc | 6.7.3 | |
| IaC | terraform-aws-modules/eks | 21.29.0 | |
| IaC | terraform-aws-modules/eks-pod-identity | 2.10.0 | |
| 클러스터 | Amazon EKS | **1.36** | 표준 지원 ~2027-08-02, Kubeflow 26.10.0(1.35~1.37)·26.03.1(1.36) 검증 범위 |
| 노드 AMI | `AL2023_x86_64_STANDARD` / `AL2023_x86_64_NVIDIA` | EKS 최신 릴리스 | |
| 애드온 | vpc-cni, coredns, kube-proxy, eks-pod-identity-agent, aws-ebs-csi-driver | EKS 1.36 최신 | |
| 컨트롤러 | AWS Load Balancer Controller (Helm) | **3.6.2** | 3.6.3은 2026-11-13 출시라 제외 |
| GPU | NVIDIA k8s-device-plugin (Helm) | **0.20.1** | |

### 2.2 Kubeflow 26.10.0 — 필수 구성만 설치
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

**설치하지 않음**: Katib, Trainer, Spark Operator, Hub(Model Registry/Catalog), Workspaces(Notebooks 2.0), Notebooks v1, KServe Models Web App, Knative Eventing, Ray

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
| torchmetrics / pycocotools | 1.9.0 / 2.0.11 | mAP 계산 |
| easyocr | 1.7.2 | 한국어 OCR |
| kfp / kfp-kubernetes | 2.18.0 / 2.18.0 | 백엔드와 같은 버전 |
| mlflow (클라이언트) | 3.17.2 | 서버와 같은 버전 |
| kubernetes (Python) | 30.1.0 | kfp 2.18.0 의존성 상한 |
| boto3 / faker / pillow / numpy | 1.44.2 / 40.54.0 / 13.0.0 / 2.5.4 | |

### 2.5 로컬 도구
AWS CLI v2, kubectl 1.36.x, kustomize v5.x, Helm 3.x, Docker + buildx, envsubst(gettext), jq, uv(선택)

## 3. 신분증 인식 범위

| 단계 | 내용 | 방식 |
|---|---|---|
| ① 종류 분류 + 필드 영역 검출 | 주민등록증 / 운전면허증 / 여권 + 이름·번호·주소·발급일·사진 영역 | **직접 학습**: torchvision `fasterrcnn_mobilenet_v3_large_fpn` (Apache-2.0), 입력 짧은 변 640 |
| ② 필드 텍스트 인식 | 검출 영역 크롭 → OCR (번호·날짜는 허용 문자 제한) | EasyOCR `ko`+`en` |
| ③ 후처리 | 숫자 OCR 혼동 보정, 형식 검증, **모든 필드에서 주민번호 패턴 마스킹** | 규칙 기반 |

검출 클래스: `card_rrc`, `card_driver`, `card_passport`, `field_name`, `field_id_number`, `field_address`, `field_issue_date`, `field_photo`

**데이터는 합성만 사용**한다. PIL + Faker(ko_KR)로 가짜 카드를 그리고(주민번호 성별 자리 0 → 실재 불가, "SAMPLE" 워터마크), 무작위 배경 합성·회전(±12°)·밝기/대비·블러·노이즈를 적용한다. COCO 형식으로 `s3://<bucket>/datasets/idcard/<version>/{train,val}.tar.gz`에 저장한다.

## 4. 아키텍처

```
 사용자 (180.224.237.157/32만)
   │ HTTP :80
   ▼
 ALB (AWS LBC, inbound-cidrs 제한, target-type ip)
   │
   ▼
 istio-ingressgateway ── 인증: 브라우저=oauth2-proxy+Dex 쿠키 / 스크립트=EKS SA 토큰(Istio RequestAuthentication)
   ├─ /                 Central Dashboard
   ├─ /_/pipeline/      KFP UI,  /pipeline  KFP API
   ├─ /mlflow/          MLflow (VirtualService, --static-prefix=/mlflow)
   └─ /serving/kubeflow-user-example-com/idcard-recognizer/*  KServe → BentoML 컨테이너

 EKS 1.36
 ├─ Node A1·A2 m6i.xlarge × 2 (node-role=cpu): Istio, Dex, KFP, MLflow, PostgreSQL, KServe 추론 파드 …
 └─ Node B g6.xlarge  (node-role=gpu, taint 없음): 학습 파드(nvidia.com/gpu:1) + 일부 플랫폼 파드

 Pod Identity: mlflow/mlflow → S3 mlflow/*,  kubeflow-user-example-com/default-editor → S3 datasets/*, ECR push
 ECR: idcard-ml, idcard-builder, mlflow-server, idcard-recognizer
```

### 노드 2대 운영
- GPU 노드에 `NoSchedule` taint를 걸지 않아 플랫폼 파드가 두 노드에 나뉘어 올라간다. 학습 파드만 GPU를 요청하고 `node-role=gpu`에 고정된다.
- 그래서 GPU 노드는 0대로 줄일 수 없다. 비용은 실습 후 `make destroy`로 관리한다.
- GPU 노드(할당 가능 CPU 3.92)의 CPU를 플랫폼·추론 파드와 나눠 쓰므로 학습 파드는 CPU 요청 1코어(limit 없음)로 둔다. 2코어 요청 시 EKS에서 GPU가 비어 있는데도 Pending이 되었다.
- 파드가 Pending이면 `cpu_instance_type = "m6i.2xlarge"` 또는 일반 노드 2대로 상향한다.

### 서빙 구조
- **BentoML**: 검출 모델(state_dict) + EasyOCR 가중치를 BentoML 모델 하나로 묶고, 서비스(`/recognize`, `/recognize_b64`, `/readyz`)와 함께 Bento로 빌드한다. 서빙 이미지는 CPU 전용 torch를 쓴다.
- **이미지 빌드**: 파이프라인 파드가 `bentoml containerize`와 같은 방식으로 빌드 컨텍스트(모델 포함)를 만들고, `buildctl`로 BuildKit 데몬에 보내 ECR에 푸시한다. ECR 인증 토큰은 파드의 Pod Identity로 발급한다.
- **KServe**: 커스텀 predictor 컨테이너(포트 3000), Knative 서버리스 모드, `minReplicas: 1`. 두 번째 배포부터 `canaryTrafficPercent: 10` → 스모크 테스트 통과 시 100%.
- Knative가 ECR 이미지 태그를 다이제스트로 바꾸려다 인증 실패하지 않도록 `registries-skipping-tag-resolving`에 ECR 레지스트리를 추가한다.

## 5. 학습 파이프라인 (KFP v2)

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
| train | idcard-ml | GPU 1개, AMP, SGD+warmup/cosine, MLflow에 파라미터·손실·state_dict·모델 기록 |
| evaluate | idcard-ml | mAP, mAP@0.5, 클래스별 AP, 종류 정확도 → KFP Metrics + MLflow |
| register-model | idcard-builder | MLflow Registry `idcard-detector`, alias `staging` |
| bento-build | idcard-builder | Bento 빌드 → KFP 아티팩트 |
| image-build | idcard-builder | BuildKit → ECR `idcard-recognizer:v<버전>` (레지스트리 캐시 사용) |
| deploy-kserve | idcard-builder | 최초 생성 또는 카나리 패치, Ready 대기 |
| smoke-test | idcard-builder | 게이트웨이 경유 6회 추론, 종류 정확도·마스킹 확인 |
| promote | idcard-builder | 카나리 100%, alias `production` |

파이프라인 파드에는 Istio 사이드카를 넣지 않는다(`sidecar.istio.io/inject: "false"`).

## 6. 구현 결과 (디렉터리)

```
myeks/
├── PLAN.md  README.md  Makefile  .env.example  .gitignore
├── infra/terraform/      versions, providers, variables, main(vpc+eks), storage(s3+ecr), iam(pod identity), k8s(sc+helm), outputs
├── platform/
│   ├── kubeflow/install.sh       필수 구성 설치 (issuer/HTTP 패치, Dex 비밀번호, Knative ECR 설정 포함)
│   ├── ingress/                  ALB Ingress 템플릿 + 적용 스크립트
│   ├── mlflow/                   PostgreSQL + MLflow + VirtualService
│   ├── buildkit/, kserve/        BuildKit 데몬, AuthorizationPolicy
│   └── install-extras.sh
├── ml/
│   ├── src/idcard/       labels, fonts, datagen, dataset, model, train, evaluate, recognize, postprocess,
│   │                     storage, steps(학습 단계), ops(배포 단계)
│   ├── serving/          service.py, bentofile.yaml
│   ├── tests/            datagen, postprocess 단위 테스트
│   └── requirements-{ml,builder,dev}.txt
├── pipelines/            components.py, idcard_pipeline.py, run.py
├── images/               ml/, builder/, mlflow/ Dockerfile
└── scripts/              env, check-tools, build-images, test-infer, info, destroy
```

## 7. 로컬 검증 결과 (2026-11-13)

AWS 리소스를 만들지 않고 확인할 수 있는 부분은 로컬에서 실행해 확인했다.

| 항목 | 결과 |
|---|---|
| 단위 테스트 (datagen, postprocess) | 10개 통과 |
| 합성 데이터 생성 + 박스 시각화 | 3종 카드, 박스 위치 정확 |
| 학습 1 epoch(40장, CPU) → 평가 → MLflow 기록 | 성공 (MLflow 3.x 기본 `pt2` 저장 형식 문제 → `pickle`로 수정) |
| MLflow 등록 → Bento 빌드 → export/import → 빌드 컨텍스트에 모델 포함 | 성공 (Bento 디렉터리에 모델이 없는 1.4 동작 → `construct_containerfile` 사용으로 수정) |
| `bentoml serve` → `/recognize`, `/recognize_b64` | 성공, EasyOCR 한글 인식 확인 (마스킹 누락 경로 발견 → 모든 필드 마스킹으로 수정) |
| KFP 파이프라인 컴파일 | 성공 (GPU, nodeSelector, /dev/shm 반영) |
| Kubeflow 필수 구성 16개 kustomize 경로 렌더링 | 성공, EKS issuer·`FORCE_HTTPS=false` 패치 반영 |
| Terraform init / validate / fmt | 통과 |
| 이미지 requirements (linux/amd64) 의존성 해석 | 통과 |

**아직 검증하지 않은 것**: 실제 EKS 위 실행 전체 (이미지 빌드·푸시, Kubeflow 설치, ALB, GPU 학습, BuildKit 빌드, KServe 배포·카나리).

## 8. 비용 예상 (서울, 온디맨드, 대략)

| 항목 | 시간당 (USD) |
|---|---|
| EKS 컨트롤 플레인 | 0.10 |
| m6i.xlarge × 2 | 약 0.48 |
| g6.xlarge × 1 | 약 1.0 |
| NAT Gateway | 약 0.06 + 데이터 |
| ALB | 약 0.03 + LCU |
| EBS gp3 (노드 250 GB + PVC 약 90 GB) | 월 약 30 |
| **합계** | **시간당 약 1.6 → 하루 약 40** |

## 9. 위험 요소와 대응

| 위험 | 대응 |
|---|---|
| 노드 리소스 부족 | 필수 구성만 설치. 그래도 부족하면 일반 노드 상향 (변수) |
| GPU 노드 CPU 부족으로 학습 파드 Pending | 학습 파드 CPU 요청 1코어. 그래도 Pending이면 `kubectl describe pod`의 `Insufficient cpu` 확인 후 GPU 노드의 추론 파드 위치 확인 |
| 노드그룹을 수동으로 늘린 채 방치 (GPU 2대면 시간당 약 +0.8달러) | `kubectl get nodes -L node-role`로 확인, README의 CLI 절차로 Terraform 값에 맞춤 |
| g6.xlarge를 제공하지 않는 AZ | Terraform이 두 인스턴스 타입을 모두 제공하는 AZ를 골라 서브넷 생성 |
| Kubeflow 26.10.0 출시 직후 문제 | `KF_VERSION=26.03.1`로 재설치 |
| HTTP 로그인 쿠키 | oauth2-proxy `FORCE_HTTPS=false` 패치 |
| 허용 IP 변경 시 접속 불가 | `allowed_cidrs` 수정 후 `make infra && make ingress` (EKS API 엔드포인트도 같은 제한) |
| destroy 시 ALB/ENI/EBS 잔존 | `scripts/destroy.sh`가 Ingress·PVC를 먼저 삭제하고 남은 EBS를 출력 |
| BuildKit TCP 무인증 노출(클러스터 내부) | 실습 범위로 한정. 운영 시 mTLS 또는 NetworkPolicy 적용 필요 |
| 개인정보 | 합성 데이터만 사용, 응답 전 필드 마스킹 |
| KServe predictor 접근 정책을 모두 허용 | 게이트웨이 인증은 유지됨. 운영 시 principal 기반으로 좁힐 것 |
