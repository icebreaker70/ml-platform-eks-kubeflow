# 5주차 - EKS + Kubeflow에 TensorBoard 인스턴스 띄우기

EKS 위의 Kubeflow 환경에 **Tensorboard 구성 요소를 추가로 설치**하고, MovieLens 영화 추천 모델을 Kubeflow 파이프라인으로 학습한 뒤,
그 로그를 **Kubeflow Tensorboard 인스턴스**로 열어 학습 과정을 확인하는 실습 저장소입니다.

- 해설 글: [5주차 - EKS + Kubeflow에 TensorBoard 인스턴스 띄우기 — 영화 추천 모델의 학습 과정을 눈으로 확인하기](https://velog.io/@sejkim/5%EC%A3%BC%EC%B0%A8-EKS-Kubeflow%EC%97%90-TensorBoard-%EC%9D%B8%EC%8A%A4%ED%84%B4%EC%8A%A4-%EB%9D%84%EC%9A%B0%EA%B8%B0-%EC%98%81%ED%99%94-%EC%B6%94%EC%B2%9C-%EB%AA%A8%EB%8D%B8%EC%9D%98-%ED%95%99%EC%8A%B5-%EA%B3%BC%EC%A0%95%EC%9D%84-%EB%88%88%EC%9C%BC%EB%A1%9C-%ED%99%95%EC%9D%B8%ED%95%98%EA%B8%B0)
- 기반 실습(4주차): [EKS 위에 Kubeflow · MLflow · BentoML · KServe로 신분증 인식 MLOps 파이프라인 만들기](https://velog.io/@sejkim/4%EC%A3%BC%EC%B0%A8-EKS-%EC%9C%84%EC%97%90-Kubeflow-MLflow-BentoML-KServe%EB%A1%9C-%EC%8B%A0%EB%B6%84%EC%A6%9D-%EC%9D%B8%EC%8B%9D-MLOps-%ED%8C%8C%EC%9D%B4%ED%94%84%EB%9D%BC%EC%9D%B8-%EB%A7%8C%EB%93%A4%EA%B8%B0) — 이 저장소에 함께 들어 있습니다([부록](#부록-4주차-신분증-인식-파이프라인))
- 시리즈: [Machine Learning Platform Engineering](https://velog.io/@sejkim/series/Machine-Learning-Platform-Engineering)
- 설계와 버전 선택 근거: [PLAN.md](PLAN.md)

## TL;DR

| 항목 | 내용 |
|---|---|
| 과제 | Kubeflow 실습 환경에 **Tensorboard 인스턴스**를 띄우고, 영화 추천 모델 학습 로그를 확인한다 |
| 추가 설치 | `tensorboard-controller` + `tensorboards-web-app` (Kubeflow 26.10.0 매니페스트의 Notebooks v1 묶음) |
| 데이터 | MovieLens `ml-latest-small` — 평점 100,836건, 사용자 610명, 영화 9,724편 |
| 모델 | BPR 행렬 분해(PyTorch). 임베딩 차원 16 / 32 / 64를 차례로 학습해 비교 |
| 로그 저장 | 프로필 네임스페이스의 PVC `movierec-tb` (gp3 EBS, ReadWriteOnce) |
| 인스턴스 | `Tensorboard` 커스텀 리소스 1개, `logspath: pvc://movierec-tb/movierec` |
| 로컬 실측 | 3개 차원 × 20 epoch 학습 13초, HR@10 **0.70 → 0.72** |
| EKS 실측 | 파이프라인 3분 18초(5/5 성공), 지표는 로컬과 동일. 인스턴스 생성 후 **약 1분 만에 브라우저에서 확인**, GPU 노드는 0대 유지 |
| 명령 | `make tensorboard` → `make movierec` → `make tb` |

핵심은 세 가지입니다.

1. "필수 구성만" 설치한 Kubeflow에는 **Tensorboard 기능이 없습니다.** 컨트롤러와 웹 앱을 따로 올려야 합니다.
2. HTTP로 접속하는 실습 환경에서는 웹 앱의 **Secure 쿠키 설정을 꺼 두어야** 합니다.
3. 로그를 EBS PVC에 두면 **학습 파드와 Tensorboard 파드가 같은 노드에 있어야** 합니다. 실행 순서가 중요합니다.

GPU 노드는 **Karpenter가 필요할 때만 Spot으로** 띄우므로, GPU를 쓰지 않는 이 실습의 비용은 시간당 약 0.7달러입니다(GPU 노드를 항상 켜 두던 구성은 약 1.6달러).

## 전체 구조

```
브라우저 → ALB(HTTP, 허용 IP만) → istio-ingressgateway → oauth2-proxy/Dex
   ├─ /                                      Kubeflow 대시보드 (메뉴: Tensorboards)
   ├─ /tensorboards/                         tensorboards-web-app — 인스턴스 목록 · 생성 · 삭제
   ├─ /tensorboard/<ns>/<이름>/              Tensorboard 인스턴스 (컨트롤러가 만든 VirtualService)
   ├─ /_/pipeline/                           Kubeflow Pipelines
   └─ /mlflow/                               MLflow

kubeflow-user-example-com (프로필 네임스페이스)
   train-sweep 파드 ──쓰기──▶ PVC movierec-tb (/tensorboard/movierec/<실행>-dim<N>/)
   Tensorboard 파드 ──읽기 전용──▶ 같은 PVC (logspath: pvc://movierec-tb/movierec)

노드: 관리형 일반 노드 m6i.xlarge × 2 (온디맨드, 항상)
      + Karpenter NodePool gpu(Spot 우선, GPU 파드가 있을 때만) · burst(임시 CPU 노드)
```

| 구성 요소 | 하는 일 |
|---|---|
| `tensorboard-controller` | `Tensorboard` 리소스를 보고 같은 이름의 Deployment · Service · VirtualService를 만든다 |
| `tensorboards-web-app` | 대시보드 안에서 인스턴스를 만들고(New TensorBoard) 여는(CONNECT) 화면 |
| `Tensorboard` 리소스 | "이 로그 경로를 보여 주는 TensorBoard 서버 하나". 필드는 `logspath` 하나뿐 |
| PVC `movierec-tb` | 학습 파드가 쓰고 Tensorboard 파드가 읽는 공유 저장소 |

## 디렉터리

| 경로 | 내용 |
|---|---|
| `platform/tensorboard/` | Tensorboard 컨트롤러 · 웹 앱 설치(`install.sh`), 로그용 PVC, 인스턴스 생성 · 삭제(`instance.sh`) |
| `ml/src/movierec/` | MovieLens 내려받기 · 분리(`data.py`), BPR 모델 · 지표(`model.py`), 학습 + TensorBoard 로깅(`train.py`) |
| `pipelines/movierec_pipeline.py`, `run_movierec.py` | 영화 추천 파이프라인(prepare-data → train-sweep)과 제출 스크립트 |
| `infra/terraform/` | VPC, EKS 1.36, 관리형 노드그룹(m6i.xlarge×2), Karpenter, S3, ECR, Pod Identity, ALB 컨트롤러, NVIDIA 플러그인 |
| `platform/karpenter/` | Karpenter NodePool — GPU 노드(Spot 우선, 학습 때만), 파이프라인용 임시 CPU 노드 |
| `platform/kubeflow/` | Kubeflow 26.10.0 필수 구성 설치 스크립트 |
| `platform/ingress/` | ALB Ingress (HTTP:80, 허용 IP 제한) |
| `platform/mlflow/` | MLflow 3.17.2 + PostgreSQL 17 |
| `platform/buildkit/`, `platform/kserve/` | (4주차) BuildKit 데몬, KServe 접근 정책 |
| `ml/src/idcard/`, `ml/serving/` | (4주차) 신분증 검출 모델과 BentoML 서비스 |
| `pipelines/idcard_pipeline.py`, `run.py` | (4주차) 신분증 인식 파이프라인 |
| `images/` | 컴포넌트 이미지 Dockerfile (idcard-ml, idcard-builder, mlflow-server) |
| `scripts/` | 환경 변수, 이미지 빌드, 추론 테스트, 정리 |

## 0. 준비

```bash
make check-tools            # aws, terraform, kubectl, kustomize, helm, docker(buildx), envsubst, python3, jq
cp .env.example .env        # KF_USER_PASSWORD 변경
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
make venv && make test      # 로컬 가상환경(.venv) + 단위 테스트 27개 (movierec 7개 포함)
```

## 1. 사전 조건 — 인프라 · 이미지 · 플랫폼

4주차와 같은 EKS + Kubeflow 환경이 필요합니다. 클러스터가 없으면 처음부터 만듭니다.

```bash
make infra        # EKS, 일반 노드 2대, Karpenter, S3, ECR … (약 20분)
make images       # 컴포넌트 이미지 → ECR (첫 빌드 20~40분)
make kubeflow && make ingress && make mlflow && make extras
make nodes        # 노드와 Karpenter NodePool 확인 (GPU 노드는 0대가 정상)
make info         # 접속 주소
```

**이전에 만든 이미지가 있어도 `make images`를 다시 해야 합니다.** 학습 이미지(`idcard-ml`)에 두 가지가 추가됐습니다.

- `tensorboard==2.21.0` 패키지 (`ml/requirements-ml.txt`) — PyTorch의 `SummaryWriter`가 사용
- `ml/src/movierec/` — 영화 추천 코드

새 이미지를 따로 만들지 않고 기존 학습 이미지에 얹었습니다(torch가 이미 들어 있음).

브라우저에서 `http://<ALB>/` → `user@example.com` / `.env`의 비밀번호로 로그인합니다.

### 노드 구성 — Karpenter

GPU 노드를 항상 켜 두지 않습니다. GPU를 요청하는 파드가 생기면 Karpenter가 그때 노드를 띄우고, 비면 지웁니다.

| 노드 | 누가 관리 | 언제 떠 있나 | 무엇이 올라가나 |
|---|---|---|---|
| 일반 노드 `m6i.xlarge` × 2 | EKS 관리형 노드그룹 (온디맨드) | 항상 | Kubeflow · MLflow · 추론 파드, **영화 추천 학습 파드와 Tensorboard 파드** |
| GPU 노드 | Karpenter NodePool `gpu` (Spot 우선, 없으면 온디맨드) | GPU를 요청하는 파드가 있을 때만 | 신분증 학습 파드 (`nvidia.com/gpu` taint) |
| 임시 CPU 노드 | Karpenter NodePool `burst` (Spot 우선) | 신분증 파이프라인 단계가 일반 노드에 못 들어갈 때만 | `workload=batch` taint를 허용한 파드 |

- 영화 추천 실습은 CPU만 쓰므로 **GPU 노드가 한 번도 뜨지 않습니다.**
- 학습 파드와 Tensorboard 파드는 일부러 일반 노드에만 둡니다([주의할 점](#7-주의할-점) 참고).
- 설정은 `terraform.tfvars`에서 바꿉니다. Spot만 쓰려면 `gpu_capacity_types = ["spot"]`, 항상 켜진 온디맨드 GPU 노드를 쓰려면 `enable_karpenter = false`. 바꾼 뒤 `make infra`.

EKS에서 확인한 결과 (클러스터 `mlops`, 2026-11-16, 4주차 신분증 파이프라인 기본값 실행):

| 확인 항목 | 결과 |
|---|---|
| 평소 노드 | 관리형 일반 노드 2대뿐 (가용 영역 a · c에 1대씩). Karpenter 노드 0대 |
| GPU 노드 | 학습 파드가 Pending이 되자 Spot `g4dn.xlarge` 기동 — 약 45초 뒤 노드 준비 완료 |
| 임시 CPU 노드 | 평가 단계부터 Spot `c8i-flex.xlarge` 기동(약 33초), 이후 단계를 모두 처리 |
| 신분증 파이프라인 | 20/20 성공, 28분 24초 (기본값 10 epoch · 3,000장) |
| 회수 | GPU 노드는 학습 종료 5분 뒤, 임시 노드는 2분 뒤 비우기 시작 → 일반 노드 2대로 복귀 |

- 새로 만든 S3 버킷 때문에 첫 실행의 평가 단계가 실패했습니다. MLflow 서버에 `MLFLOW_S3_ENDPOINT_URL`을 넣어 리전 주소로 고정해 해결했습니다([문제 해결](#문제-해결)).
- 확인하지 못한 것: Spot 회수가 실제로 일어났을 때의 재시도, 온디맨드로 넘어가는 경우, GPU 노드가 비우기 시작한 뒤 완전히 삭제되기까지 약 7분이 걸린 이유.

모든 AWS 자원에는 `Owner=sejkim` 태그가 붙습니다(Karpenter가 띄운 노드와 PVC로 만든 EBS 포함). 확인:

```bash
aws resourcegroupstaggingapi get-resources --region ap-northeast-2 \
  --tag-filters Key=Owner,Values=sejkim --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | wc -l
```

## 2. Tensorboard 구성 요소 설치

```bash
make tensorboard
```

`make kubeflow`는 필수 구성만 설치하므로 Tensorboard가 빠져 있습니다(Notebooks v1 묶음 `applications/notebooks-v1/upstream/`에 들어 있음).
`platform/tensorboard/install.sh`가 네 가지를 합니다.

1. `tensorboard-controller` 설치 (CRD 포함, `overlays/kubeflow`)
2. `tensorboards-web-app` 설치 — HTTP 접속이므로 `TWA_APP_SECURE_COOKIES=false`로 패치 후 `overlays/istio` 적용
3. 대시보드 메뉴(`dashboard-config` ConfigMap)에 **Tensorboards** 추가
4. 프로필 네임스페이스에 로그용 PVC `movierec-tb` (5Gi, gp3) 생성

**Secure 쿠키를 끄는 이유**: Kubeflow 웹 앱은 CSRF 방어용 `XSRF-TOKEN` 쿠키에 기본으로 `Secure`를 붙이는데, HTTP로 접속하면 브라우저가 이 쿠키를 저장하지 않아
목록은 보여도 생성 요청이 거부됩니다(Kubeflow 매니페스트 README도 HTTP에서는 Tensorboard · Jupyter 웹 앱이 동작하지 않는다고 경고). 운영 환경이라면 HTTPS를 붙이고 기본값을 유지해야 합니다.

```bash
kubectl get crd tensorboards.tensorboard.kubeflow.org
kubectl -n kubeflow get deploy tensorboard-controller-deployment tensorboards-web-app-deployment
kubectl -n kubeflow-user-example-com get pvc movierec-tb     # 첫 파드가 붙기 전까지는 Pending이 정상
```

`gp3` StorageClass는 `WaitForFirstConsumer`라, PVC는 처음 붙는 파드의 가용 영역에 EBS로 만들어집니다.

EKS 실측: 컨트롤러 · 웹 앱 1/1 Ready, 웹 앱 `APP_SECURE_COOKIES=false`, 대시보드 메뉴에 Tensorboards 추가, `/tensorboards/` 목록 200 응답,
로그 PVC는 학습 파드가 처음 붙을 때 `ap-northeast-2a`에 5 GiB EBS로 생성(`Owner` · `Project` 태그 확인).

## 3. 로컬에서 먼저 돌려 보기

```bash
make movierec-local                              # MovieLens 내려받기 → 차원 16/32/64 학습 → runs/movierec
.venv/bin/tensorboard --logdir runs/movierec     # http://localhost:6006
```

- 데이터: MovieLens `ml-latest-small`(약 1 MB). 라이선스상 재배포할 수 없어 저장소에 넣지 않고 실행할 때 내려받습니다.
- 평점 값은 쓰지 않고 "봤다"는 사실만 씁니다(암묵적 피드백). 사용자마다 **가장 마지막에 본 영화 1편**을 검증용으로 뗍니다.
- 모델: BPR 행렬 분해 — 사용자 · 영화 임베딩의 내적 + 영화 편향. "본 영화의 점수가 안 본 영화보다 높아지게" 학습합니다.
- 평가: 검증용 영화 1편을 그 사용자가 보지 않은 영화 99편과 섞어 순위를 매깁니다. **HR@10**(상위 10편 안에 든 비율), **NDCG@10**(더 위일수록 높은 점수). 무작위로 찍으면 HR@10은 0.10.

| 임베딩 차원 | 최종 손실 | HR@10 | NDCG@10 | 학습 시간 |
|---|---|---|---|---|
| 16 | 0.168 | 0.703 | 0.453 | 2.2초 |
| 32 | 0.148 | 0.715 | 0.466 | 2.5초 |
| 64 | 0.138 | **0.720** | **0.472** | 4.6초 |

(Apple Silicon 노트북 CPU, 데이터 읽기 포함 전체 13초)

TensorBoard에 남기는 것:

| TensorBoard 탭 | 기록 내용 |
|---|---|
| Scalars | 스텝별 손실(`train/bpr_loss`), epoch별 손실 · `val/hr@10` · `val/ndcg@10` |
| Histograms | 사용자 · 영화 임베딩과 영화 편향의 분포 변화 |
| Projector | 인기 영화 1,500편의 임베딩 + 제목 · 장르 · 평점 수 |
| Text | 예시 사용자 1명의 최근 시청 목록과 추천 10편 |
| HParams | 임베딩 차원 · 학습률 등과 최종 지표 |

실행마다 하위 폴더(`<실행 이름>-dim<N>/`)를 따로 써서 TensorBoard에서 곡선을 겹쳐 볼 수 있게 했습니다.

## 4. Kubeflow 파이프라인으로 학습

```bash
make movierec                              # 기본: EPOCHS=20, EMB_DIMS=16,32,64
EPOCHS=50 EMB_DIMS=8,32,128 make movierec  # 값을 바꿔서
```

```
prepare-data (MovieLens 내려받기, 통계 리포트) ─Dataset─▶ train-sweep (차원별로 차례로 학습)
                                                             ├─▶ PVC movierec-tb : movierec/<실행 이름>-dim<N>/
                                                             └─▶ MLflow 실험 movierec (지표)
```

- 학습 파드는 PVC를 `/tensorboard`에 붙이고 `/tensorboard/movierec/<파이프라인 실행 이름>-dim<N>/`에 로그를 씁니다. 파이프라인을 여러 번 돌리면 TensorBoard에 실행이 계속 쌓입니다.
- GPU를 요청하지 않으므로 Karpenter도 GPU 노드를 띄우지 않습니다. 임시 CPU 노드(`burst`)로 가는 허용도 넣지 않았습니다.
- 차원마다 파드를 나누지 않고 **파드 하나에서 차례로 학습**합니다. 로그 PVC가 EBS(ReadWriteOnce)라, 파드가 나뉘면 노드가 바뀔 때마다 볼륨을 뗐다 붙여야 합니다.

EKS 실측 (실행 `movierec-e20-d16-32-64`):

| 단계 | 소요 | 내용 |
|---|---|---|
| prepare-data | 7초 | MovieLens 내려받기, 100,836건 확인 |
| train-sweep | 2분 29초 | 이미지 받기 1분 33초 + **실제 실행 36초** (3개 차원 × 20 epoch) |
| **전체** | **3분 18초** | 5/5 성공 |

| 임베딩 차원 | HR@10 | NDCG@10 |
|---|---|---|
| 16 | 0.7033 | 0.4526 |
| 32 | 0.7148 | 0.4657 |
| 64 | **0.7197** | **0.4720** |

지표는 로컬 실행과 소수점 넷째 자리까지 같았습니다(같은 시드, CPU 학습). 학습 단계 시간의 대부분은 3.6 GB 학습 이미지를 그 노드에서 처음 받는 시간입니다.
MLflow 실험 `movierec`에도 실행 3개가 기록됩니다.

## 5. Tensorboard 인스턴스 띄우기

**학습이 끝난 뒤에** 만듭니다.

### 방법 A — 명령으로

```bash
make tb           # 인스턴스 생성 → 접속 주소 출력
make tb-status    # 인스턴스 · PVC · 파드 상태
make tb-delete    # 인스턴스 삭제 (로그는 PVC에 남음)
```

`make tb`는 이 리소스 하나를 적용합니다(`platform/tensorboard/tensorboard.yaml.tmpl`).

```yaml
apiVersion: tensorboard.kubeflow.org/v1alpha1
kind: Tensorboard
metadata:
  name: movierec
  namespace: kubeflow-user-example-com
spec:
  logspath: pvc://movierec-tb/movierec      # pvc://<PVC 이름>/<PVC 안의 하위 경로>
```

`logspath`의 하위 경로(`movierec`)는 학습 파드가 쓴 폴더와 같아야 합니다. 어긋나면 TensorBoard는 뜨지만 "No dashboards are active"만 보입니다.

### 방법 B — 대시보드 화면에서

1. `http://<ALB>/` 로그인 → 왼쪽 메뉴 **Tensorboards** (메뉴가 없으면 `http://<ALB>/_/tensorboards/`)
2. 네임스페이스 `kubeflow-user-example-com` 확인 → **New TensorBoard**
3. Name `movierec`, 저장소 종류 **PVC**, PVC Name `movierec-tb`, Mount Path `movierec`
4. **CREATE** → 목록의 Status가 준비 상태가 되면 **CONNECT**

화면에서 만들어도 방법 A와 같은 `Tensorboard` 리소스가 생깁니다.

### 접속

```text
http://<ALB>/tensorboard/kubeflow-user-example-com/movierec/      (대시보드 로그인 쿠키 필요)
```

컨트롤러는 리소스 하나에 대해 같은 이름의 Deployment(`tensorflow/tensorflow:2.5.1`, `--logdir=/tensorboard_logs/`, PVC 읽기 전용), Service(80 → 6006),
VirtualService(`/tensorboard/<ns>/<이름>/` → Service)를 만듭니다.

EKS 실측 (방법 A):

| 시각 (UTC) | 일어난 일 |
|---|---|
| 15:31:50 | 파이프라인 종료 |
| 15:32:57 | `make tb` — 컨트롤러가 Deployment 생성, 2초 뒤 로그 PVC 연결 |
| 15:33:15 | 뷰어 이미지 받기 13초, Tensorboard 컨테이너 시작 |
| 15:33:23 | 파드가 한 번 교체됨 (원인 미확인, `RWO_PVC_SCHEDULING` 동작으로 추정) |
| 15:33:56 | 브라우저에서 첫 접속, HTTP 200 |
| 15:37:37 | `make tb-delete` — 인스턴스 삭제, PVC와 로그는 유지 |

`make tb`부터 브라우저에서 곡선을 보기까지 **약 1분**이었습니다. 만든 직후 잠깐 접속이 끊겨도 기다리면 됩니다.

## 6. TensorBoard에서 확인할 것

실행은 `movierec-e20-d16-32-64-dim16` · `-dim32` · `-dim64` 세 개로 보입니다. 뷰어가 구버전(TensorBoard 2.5 계열)이라
**Projector는 오른쪽 위 `INACTIVE` 목록 안에** 있습니다.

| 탭 | 볼 것 | 이번 실습에서 보인 것 |
|---|---|---|
| Scalars / Time Series | `train/epoch_loss`, `val/hr@10`, `val/ndcg@10`을 세 실행 겹쳐 보기 | 손실은 차원 순서대로 내려가지만, **HR@10 곡선이 epoch마다 0.012~0.035씩 오르내려** 차원 간 차이(0.017)와 비슷함. 64차원은 17 epoch에 0.744까지 갔다가 0.720으로 끝남 |
| Histograms | `embeddings/item`, `embeddings/user`, `embeddings/item_bias` | 임베딩 분포가 epoch이 지날수록 넓어지고, 영화 편향은 오른쪽으로만 긴 꼬리 — 편향과 평점 수(로그)의 상관계수 0.95(로컬 확인) |
| Projector | `movies`에서 제목 검색, `Color by: genre` | "가깝다" = 같은 사람들이 함께 봄. `Matrix` 옆에 `Star Wars` · `Gladiator`, `Toy Story` 옆에 `Schindler's List` — 인기 영화끼리 뭉침 |
| Text | `recommendations` | 같은 사용자(#142)인데 16차원은 `Harry Potter` 같은 대작을, 32차원은 `Ice Age 2` · `Ratatouille`처럼 취향에 가까운 작품을 추천 |
| HParams | PARALLEL COORDINATES VIEW | 바꾼 것이 `emb_dim` 하나뿐이라 다른 축에서는 선이 한 점으로 모임. 차원이 클수록 지표가 높음 |

**최종 숫자 하나보다 곡선 전체를 봐야 합니다.** 검증 사용자가 610명뿐이라 한 명이 맞고 틀리는 것만으로 HR@10이 0.0016씩 움직입니다.
"64차원이 가장 좋다"는 순서 정도만 믿을 수 있고, 차이를 확실히 말하려면 시드를 바꿔 여러 번 돌리거나 더 큰 데이터셋(ml-1m 등)을 써야 합니다.

## 7. 주의할 점

### EBS PVC는 한 노드에만 붙는다 — 실행 순서

로그 PVC는 gp3 EBS(ReadWriteOnce)라 같은 노드의 파드끼리만 함께 쓸 수 있고, 가용 영역에도 묶입니다.

```
Tensorboard 인스턴스가 떠 있나?
  ├─ 아니오 → make movierec (학습) → make tb (인스턴스 생성) → 확인
  └─ 예     → make tb-delete (로그는 PVC에 남음) → make movierec → make tb
```

| 상황 | 일어나는 일 |
|---|---|
| 학습이 끝난 뒤 인스턴스 생성 | 문제없음 — **권장 순서** (EKS 실측: 2초 만에 볼륨 연결) |
| 학습 중에 인스턴스 생성 | 컨트롤러가 학습 파드가 있는 노드를 선호하도록 배치(`RWO_PVC_SCHEDULING=True`). 실시간으로 볼 수 있다 (미실행) |
| 인스턴스가 떠 있는데 학습 실행 | 학습 파드가 다른 노드에 가면 `Multi-Attach error`로 `ContainerCreating`에서 멈춤 |

- 마지막 경우를 막으려고 `make movierec`은 **Tensorboard 인스턴스가 있으면 제출하지 않습니다**(`FORCE=1`로 무시).
- 실시간으로 보려면 `EPOCHS=500 make movierec`처럼 학습을 늘리고, `train-sweep` 파드가 Running이 된 뒤 `make tb`를 실행합니다.
- 이번 실행에서 로그 볼륨은 `ap-northeast-2a`에 만들어져, Tensorboard 파드가 갈 수 있는 일반 노드는 a 영역의 1대뿐이었습니다.
- 여러 파드가 동시에 읽고 써야 하는 운영 환경이라면 EFS 같은 ReadWriteMany 저장소나 오브젝트 스토리지가 맞습니다.

### Karpenter 노드에는 올리지 않는다

Karpenter의 임시 CPU 노드에는 `workload=batch` taint가 있습니다(플랫폼 파드가 Spot 노드로 옮겨 가지 않도록).
Tensorboard 파드는 컨트롤러가 만들어 이 taint를 허용하게 바꿀 수 없으므로(`Tensorboard` 리소스의 필드는 `logspath`뿐), 학습 파드만 임시 노드로 가면
두 파드가 같은 노드에 있을 수 없어 `Multi-Attach error`가 납니다. 그래서 두 파드 모두 일반 노드에만 둡니다.
일반 노드 2대가 꽉 차 Pending이면 `terraform.tfvars`에서 `cpu_node_count = 3`으로 올리고 `make infra`를 실행합니다.

### 뷰어 이미지 버전

컨트롤러의 기본 뷰어 이미지는 `tensorflow/tensorflow:2.5.1`(2021년)이고 로그는 `tensorboard 2.21.0`으로 씁니다.
**이 조합으로 문제없이 읽혔습니다(EKS 실측).** 화면 구성만 다릅니다. 바꾸려면 컨트롤러 설정의 `TENSORBOARD_IMAGE`를 고칩니다(바꿔서 실행해 보지는 않음).

### Notebooks v1 묶음은 구버전 계열

Kubeflow 26.10.0은 Notebooks 2.0(Workspaces)을 새로 내세우고, Tensorboard 컨트롤러 · 웹 앱이 든 Notebooks v1은 구버전 계열로 분류합니다.
이후 버전에서는 설치 위치나 방식이 바뀔 수 있습니다.

## 8. 비용과 정리

| 항목 (서울, 시간당 USD, 대략) | GPU 노드 상시 (`enable_karpenter = false`) | Karpenter (기본) |
|---|---|---|
| EKS 컨트롤 플레인 | 0.10 | 0.10 |
| 일반 노드 `m6i.xlarge` × 2 | 약 0.48 | 약 0.48 |
| GPU 노드 | 약 1.0 (온디맨드 `g6.xlarge`, 항상) | **0** (영화 추천 실습에서는 뜨지 않음) |
| NAT · ALB | 약 0.09 | 약 0.09 |
| **합계** | **약 1.6** | **약 0.7** |

GPU가 필요한 파이프라인(4주차 신분증)을 돌릴 때만 GPU Spot 요금이 붙습니다. 2026-11-15 서울 리전 Spot 시세: `g4dn.xlarge` 약 0.33 · `g6.xlarge` 약 0.47 · `g5.xlarge` 약 0.60달러.

```bash
make tb-delete     # 인스턴스만 삭제
make destroy       # 전체 삭제
```

`make destroy`는 추론 서비스 · ALB와 PVC(EBS)를 쿠버네티스에서 먼저 지우고, Karpenter NodePool을 삭제한 뒤(남은 Karpenter 인스턴스는 태그로 찾아 종료) `terraform destroy`를 실행합니다.
S3의 데이터셋 · MLflow 아티팩트, ECR 이미지, TensorBoard 로그도 함께 삭제됩니다.
**학습이 없어도 시간당 약 0.7달러가 나오므로 실습이 끝나면 꼭 삭제하세요.**

## 문제 해결

### Tensorboard · 영화 추천

| 증상 | 확인할 것 |
|---|---|
| 메뉴에 Tensorboards가 없다 | `http://<ALB>/_/tensorboards/`로 직접 접속. `make kubeflow`를 다시 실행했다면 메뉴가 초기화되므로 `make tensorboard`도 다시 실행 |
| 목록은 보이는데 생성이 거부된다(403) | `kubectl -n kubeflow get deploy tensorboards-web-app-deployment -o yaml \| grep -A1 APP_SECURE_COOKIES` 값이 `false`인지 |
| 인스턴스가 준비 상태가 되지 않는다 | `kubectl -n kubeflow-user-example-com describe pod -l app=movierec` — `Multi-Attach error`면 [실행 순서](#ebs-pvc는-한-노드에만-붙는다--실행-순서) 참고 |
| TensorBoard에 "No dashboards are active" | `logspath`의 하위 경로와 학습 파드가 쓴 폴더가 같은지, 학습이 실제로 끝났는지 |
| 학습 파드가 `ContainerCreating`에서 멈춤 | Tensorboard 인스턴스가 PVC를 잡고 있는지 → `make tb-delete` 후 재실행 |
| `make movierec`이 제출을 거부한다 | 의도된 동작. `make tb-delete` 후 다시 실행하거나 `FORCE=1` |
| 학습 파드에서 `No module named 'movierec'` 또는 `tensorboard` | 이미지를 다시 빌드하지 않은 것 — `make images` |
| 학습 파드나 Tensorboard 파드가 `Pending` (`Insufficient cpu`/`memory`) | 일반 노드 2대가 꽉 찬 것. 이 파드들은 Karpenter 노드로 가지 않는다 — `cpu_node_count = 3` 후 `make infra` |
| 컨트롤러가 Deployment를 만들지 않는다 | `kubectl -n kubeflow logs deploy/tensorboard-controller-deployment -c manager` |

### 인프라 · 공통

| 증상 | 확인 |
|---|---|
| ALB 접속 시 타임아웃 | 내 공인 IP가 `allowed_cidrs`와 같은지 (`curl ifconfig.me`) |
| 로그인 후 계속 로그인 화면 | HTTP 접속이므로 `FORCE_HTTPS=false` 패치 확인: `kubectl -n oauth2-proxy get cm -o yaml \| grep FORCE` |
| `make pipeline` / `make movierec`에서 401/403 | `kubectl -n kubeflow-user-example-com create token default-editor` 동작 확인, `kubectl -n istio-system get requestauthentication m2m-token-issuer -o yaml`의 issuer가 `terraform output oidc_issuer_url`과 같은지 |
| 플랫폼 파드 Pending | 일반 노드 2대가 꽉 찬 것. `cpu_node_count = 3` 또는 `cpu_instance_type = "m6i.2xlarge"` 후 `make infra` (플랫폼 파드는 Karpenter 노드에 올라가지 않음) |
| train 파드 Pending (Karpenter, 4주차 GPU 학습) | 노드가 뜨는 중이면 정상(수 분). 오래 걸리면 `kubectl get nodeclaims`, `kubectl -n kube-system logs deploy/karpenter \| tail -30` — Spot 용량 부족·한도 초과·`gpu_node_limit` 도달 여부 확인 |
| evaluate에서 `Failed to download artifacts … model.pt` (새 클러스터 첫 실행) | 새로 만든 S3 버킷은 전역 주소(`<버킷>.s3.amazonaws.com`)가 한동안 `us-east-1`로 연결됨. MLflow 파드에 `MLFLOW_S3_ENDPOINT_URL`이 있는지 확인: `kubectl -n mlflow get deploy mlflow -o yaml \| grep -A1 S3_ENDPOINT` (없으면 `make mlflow`) |
| MLflow 파드 `OOMKilled` / 업로드 `Connection refused` | `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` 적용 여부 (`make mlflow`) |
| `make infra`에서 `Error locating chart … no cached repo found` | 로컬 Helm 캐시 문제 — `helm repo update` 후 다시 실행 |
| 노드 수가 `terraform.tfvars`와 다름 | `kubectl get nodes -L node-role` — 수동으로 늘린 노드그룹은 아래 절차로 되돌림 |
| data-gen에서 S3 AccessDenied | `aws eks list-pod-identity-associations --cluster-name mlops` 에 `kubeflow-user-example-com/default-editor` 있는지 |

이미 만든 클러스터의 일반 노드 수를 바꿀 때는 먼저 AWS CLI로 노드그룹 크기를 맞춥니다(EKS 모듈은 생성 후 `desired_size` 변경을 무시하고, `min_size`만 올리면 AWS가 거부합니다).

```bash
NG=$(aws eks list-nodegroups --cluster-name mlops --query "nodegroups[?starts_with(@,'cpu-')]|[0]" --output text)
aws eks update-nodegroup-config --cluster-name mlops --nodegroup-name "$NG" \
  --scaling-config minSize=2,maxSize=3,desiredSize=2
aws eks wait nodegroup-active --cluster-name mlops --nodegroup-name "$NG"
make infra   # 이후 Terraform 상태와 맞춘다 (변경 없음이 정상)
```

## 부록: 4주차 신분증 인식 파이프라인

같은 환경에서 합성 신분증 이미지로 검출 모델을 학습 → MLflow 등록 → BentoML 패키징 → KServe 서빙까지 자동화하는 파이프라인입니다.
자세한 내용은 [4주차 글](https://velog.io/@sejkim/4%EC%A3%BC%EC%B0%A8-EKS-%EC%9C%84%EC%97%90-Kubeflow-MLflow-BentoML-KServe%EB%A1%9C-%EC%8B%A0%EB%B6%84%EC%A6%9D-%EC%9D%B8%EC%8B%9D-MLOps-%ED%8C%8C%EC%9D%B4%ED%94%84%EB%9D%BC%EC%9D%B8-%EB%A7%8C%EB%93%A4%EA%B8%B0)을 참고하세요.

```
data-gen → validate → train(GPU, Karpenter Spot) → evaluate ─(mAP@0.5≥기준)→ register(MLflow)
→ bento-build → image-build(BuildKit→ECR) → deploy(KServe, 카나리) → smoke-test → promote
```

```bash
make pipeline                                # 기본: 3000장, 10 epoch (EKS 실측 28분 24초)
EPOCHS=3 NUM_TRAIN=600 make pipeline         # 빠른 확인용
make test-infer                              # 합성 신분증 1장 생성 후 ALB로 추론 호출
make test-infer IMG=./my.jpg                 # 직접 고른 이미지
kubectl -n kubeflow-user-example-com get isvc idcard-recognizer   # 두 번째 실행부터 카나리 90 / 10
```

| 증상 | 확인 |
|---|---|
| smoke-test/추론이 `413 Payload Too Large`(HTML) | Host 헤더가 `example.com`(isvc `status.url`의 호스트)인지 — 아니면 대시보드로 라우팅됨 |
| 추론 `401 Jwt issuer is not configured` | `kubectl -n istio-system get requestauthentication cluster-local-gateway-jwt -o yaml`에 EKS issuer 규칙이 있는지 (`make kubeflow` 재실행) |
| image-build 실패 | `kubectl -n buildkit logs deploy/buildkitd` |
| InferenceService가 Ready 안 됨 | `kubectl -n kubeflow-user-example-com describe ksvc idcard-recognizer-predictor`, 이미지 pull 오류면 Knative `registries-skipping-tag-resolving` 설정 확인 |
| 추론 403 | `kubectl -n kubeflow-user-example-com get authorizationpolicy allow-idcard-recognizer` |

## 검증 범위

- **로컬에서 확인**: 데이터 내려받기와 전처리, 학습과 지표, TensorBoard 2.21.0에서 로그 조회, 단위 테스트 27개, 파이프라인 컴파일.
- **EKS에서 확인** (클러스터 `mlops`, 2026-11-16): Karpenter 구성(GPU Spot 노드와 임시 노드의 기동 · 회수), `make tensorboard`, `make movierec`(3분 18초, 지표는 로컬과 동일),
  `make tb`로 만든 인스턴스에 브라우저로 접속해 7개 탭 조회, `make tb-delete`, `make destroy`.
- **아직 실행하지 않은 것**: 대시보드 화면에서 인스턴스 만들기(방법 B — 목록 화면까지만 확인), 학습 중 실시간 보기, 인스턴스가 떠 있을 때의 재학습, 뷰어 이미지 교체.
  Tensorboard 파드가 한 번 교체된 원인과 GPU 노드 삭제에 7분이 걸린 원인도 확인하지 못했습니다.
- 비용은 단가를 더한 추정이고 실제 청구액이 아닙니다. 추천 지표는 무작위 99편과 비교하는 방식이라 인기 편향이 섞여 있습니다.
