# 6주차 - EKS에 LLM RAG 올리기 — 개인정보 보호법 Q&A

개인정보 보호법 PDF를 조문 단위로 색인하고, 질문에 **근거 조문을 인용해** 답하는 RAG 서비스를 만드는 실습 저장소입니다.
LLM은 EKS의 GPU 노드에서 **KServe + vLLM으로 직접 서빙**하고, 색인은 **Kubeflow 파이프라인**이 평가를 통과했을 때만 교체합니다.

- 해설 글: 작성 예정
- 이전 실습: [5주차 - TensorBoard 인스턴스 + 영화 추천](https://velog.io/@sejkim/5%EC%A3%BC%EC%B0%A8-EKS-Kubeflow%EC%97%90-TensorBoard-%EC%9D%B8%EC%8A%A4%ED%84%B4%EC%8A%A4-%EB%9D%84%EC%9A%B0%EA%B8%B0-%EC%98%81%ED%99%94-%EC%B6%94%EC%B2%9C-%EB%AA%A8%EB%8D%B8%EC%9D%98-%ED%95%99%EC%8A%B5-%EA%B3%BC%EC%A0%95%EC%9D%84-%EB%88%88%EC%9C%BC%EB%A1%9C-%ED%99%95%EC%9D%B8%ED%95%98%EA%B8%B0) ([부록 A](#부록-a-5주차-tensorboard--영화-추천)) ·
  [4주차 - 신분증 인식 MLOps 파이프라인](https://velog.io/@sejkim/4%EC%A3%BC%EC%B0%A8-EKS-%EC%9C%84%EC%97%90-Kubeflow-MLflow-BentoML-KServe%EB%A1%9C-%EC%8B%A0%EB%B6%84%EC%A6%9D-%EC%9D%B8%EC%8B%9D-MLOps-%ED%8C%8C%EC%9D%B4%ED%94%84%EB%9D%BC%EC%9D%B8-%EB%A7%8C%EB%93%A4%EA%B8%B0) ([부록 B](#부록-b-4주차-신분증-인식-파이프라인)) — 같은 저장소에 들어 있습니다
- 시리즈: [Machine Learning Platform Engineering](https://velog.io/@sejkim/series/Machine-Learning-Platform-Engineering)
- 설계 · 버전 선택 근거 · 실측 기록: [PLAN.md](PLAN.md) 10장

> **진행 상태 (2026-11-27)** — 로컬(2026-11-25 ~ 26)과 **EKS(2026-11-26 ~ 27) 모두 처음부터 끝까지 실행해서 확인했습니다.**
> EKS에서 처음 실행하며 고친 것 5건과 실측값은 [2.5 EKS 실측](#25-eks-실측-2026-11-26--27)과 PLAN.md 7.4 · 10장에 있습니다 — [검증 범위](#검증-범위).

## TL;DR

| 항목 | 내용 |
|---|---|
| 과제 | 개인정보 보호법 Q&A — 답마다 「개인정보 보호법」 제N조를 인용하고, 근거가 없으면 "찾지 못함" |
| 문서 | 저장소의 PDF 1개: 법률 제21445호(2026-09-11 시행), 47쪽 → 조문 136개 + 부칙 4개 → **청크 143개** |
| 검색 | `BAAI/bge-m3` 임베딩(CPU) + PostgreSQL 17 · pgvector, 질문에 "제N조"가 있으면 그 조를 먼저 |
| LLM | `Qwen/Qwen3-8B-FP8`을 vLLM v0.33.0으로 서빙 (EKS: KServe, g6.xlarge L4) · 로컬: `Qwen3-8B-4bit` on mlx-lm |
| 색인 갱신 | KFP 파이프라인: 파싱 → 청킹 → 임베딩 → 적재 → **평가 통과 시에만 승격** (서비스 중 색인은 그대로) |
| 검색 평가 (로컬) | 평가 세트 40문항, **Recall@5 0.972** · MRR@5 0.868 |
| 생성 평가 (로컬 4bit) | 거절 정확도 1.0 · 인용 정확도 1.0 · 핵심 표현 포함률 0.54 — **직접 읽어 보니 36개 중 정확 23 · 부분 6 · 오답 7** |
| 생성 평가 (EKS FP8) | 거절 1.0 · 인용 1.0 · 핵심 표현 0.628 · 문항당 4.4초 — **정확 26 · 부분 5 · 오답 5** |
| EKS 실측 | law-llm 기동 약 11분(그중 vLLM 이미지 받기 5분), 질의 약 3초, 색인 파이프라인 6 ~ 7분, 검색 지표는 로컬과 같음 |
| 명령 (로컬) | `make rag-index-local` → `make rag-eval-local` → `make llm-local` + `make rag-api-local` |
| 명령 (EKS) | `make images-rag` → `rag-data` → `llm-cache` → `rag-platform` → `llm` → `rag-index` → `rag-ask` → **`llm-down`** |

배운 것을 먼저 요약하면 다음과 같습니다.

1. **RAG의 절반은 데이터 준비입니다.** 법령 PDF에는 같은 조문의 **현행본과 시행 예정본이 함께** 실려 있고, 본문 속 조문 인용이 줄 첫머리에 오면 조 제목처럼 보입니다. 이걸 가리지 않으면 아직 시행되지 않은 조문으로 답합니다.
2. **인용이 맞다고 답이 맞는 것은 아닙니다.** 인용 정확도는 1.0이었지만 오답 7개 중 4개는 정답 조문을 정확히 인용하고도 금액 · 날짜 · 원칙을 틀렸습니다.
3. **평가 세트도 틀릴 수 있습니다.** "과징금 최대"의 정답을 제1항으로 적었다가, 생성 결과를 읽으면서 제2항(가중)이 최대라는 것을 알았습니다.
4. **LLM 서빙 노드는 학습 노드와 나눕니다.** 학습용 GPU는 Spot · 쓰고 끄기, 서빙용 GPU는 온디맨드 · 상시. NVIDIA device plugin이 새 노드 라벨에도 떠야 GPU 리소스가 생깁니다.

## 전체 구조

```
브라우저 / make rag-ask ─▶ ALB(HTTP, 허용 IP) ─▶ istio-ingressgateway ─▶ oauth2-proxy/Dex (로그인 또는 SA 토큰)
   ├─ /rag/                rag-api — 질의 화면 + POST /rag/ask
   ├─ /_/pipeline/         Kubeflow Pipelines — 색인 파이프라인
   └─ /mlflow/             MLflow — 질의 트레이스(law-rag), 색인 평가(law-rag-index)

kubeflow-user-example-com
   rag-api (일반 노드)  질문 → 주민번호 마스킹 → bge-m3 → pgvector top-5 → 프롬프트 → law-llm → 인용 검증 → 응답
   law-llm (KServe Standard, GPU 노드)  vLLM OpenAI 호환 API, 클러스터 내부 전용 · rag-api만 호출 가능
rag
   ragdb (pgvector, PVC 10Gi)  색인 버전 여러 개 + active_index(서비스 중인 버전 하나)
S3
   datasets/law/21445-20260911/source.pdf · models/qwen3-8b-fp8/ · models/bge-m3/ (가중치 캐시)

노드: 일반 m6i.xlarge × 2(~3) · Karpenter gpu(학습, Spot) · burst(임시 CPU) · gpu-serving(LLM, 온디맨드 g6.xlarge) ← 6주차
```

## 디렉터리

| 경로 | 내용 |
|---|---|
| `docs/개인정보 보호법(법률)(제21445호)(20260911).pdf` | 원본 문서 (법령은 저작권 보호 대상이 아님 — 저작권법 제7조) |
| `ml/src/lawrag/` | PDF 파싱(`pdfparse`), 청킹(`chunk`), 임베딩(`embed`), pgvector(`store`), 검색(`retrieve`), 프롬프트 · 생성 · 인용 검증(`prompt`, `answer`), 평가(`evaluate`, `eval/qa.jsonl`), 가중치 캐시(`cache`), 파이프라인 단계(`steps`), CLI(`prepare`, `index`) |
| `ml/rag_api/` | FastAPI rag-api + 질의 화면(`static/index.html`) |
| `pipelines/rag_index_pipeline.py`, `run_rag.py` | 색인 파이프라인과 제출 스크립트 |
| `platform/rag/` | ragdb · rag-api · law-llm(InferenceService) · 캐시 Job · 색인 Job 템플릿, `install.sh` |
| `platform/karpenter/llm-nodepool.yaml.tmpl` | NodePool `gpu-serving` (6주차) |
| `images/rag/` | rag 이미지 (CPU torch, rag-api · 파이프라인 · Job 공용) |
| `infra/terraform/` | VPC, EKS 1.36, 노드그룹, Karpenter, S3, ECR, Pod Identity, ALB 컨트롤러, NVIDIA 플러그인 |
| `platform/kubeflow/`, `ingress/`, `mlflow/` | Kubeflow 26.10.0 필수 구성, ALB, MLflow |
| `ml/src/movierec/`, `platform/tensorboard/` | (5주차) 영화 추천 · Tensorboard |
| `ml/src/idcard/`, `ml/serving/`, `platform/buildkit/`, `platform/kserve/` | (4주차) 신분증 인식 |

## 0. 준비

```bash
make check-tools            # aws, terraform, kubectl, kustomize, helm, docker(buildx), envsubst, python3, jq
cp .env.example .env        # KF_USER_PASSWORD 변경 (6주차 값은 주석 — 기본값은 scripts/env.sh)
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
make venv && make test      # 단위 테스트 84개 (+ 로컬 DB 통합 테스트 2개는 RAGDB_TEST=1일 때)
```

로컬 실습에는 Docker(pgvector 컨테이너)가 필요하고, 로컬 LLM은 **Apple Silicon**(mlx-lm) 기준입니다.

## 1. 로컬에서 먼저 돌려 보기

### 1.1 PDF → 조문 → 청크

```bash
make rag-parse-local        # → data/law/articles.jsonl, chunks.jsonl, report.md
```

국가법령정보센터에서 받은 PDF는 텍스트 레이어가 있어 OCR이 필요 없지만, 그대로 쓰면 안 되는 것이 많았습니다.

| PDF에서 본 것 | 처리 |
|---|---|
| 쪽마다 `법제처 N 국가법령정보센터` 머리말, 1쪽 담당 부서 연락처 | 제거 (105줄) |
| **같은 조가 두 번** — 뒤의 것 끝에 `[시행일: 2027. 3. 9.] 제2조` | 앞 = 현행, 뒤 = 시행 예정. 5개 조가 둘 다 있고, 신설 조 4개(제28조의12 ~ 15, AI 개발 특례)는 예정본만 있음 → 시행 예정 9개 |
| 본문 속 인용이 줄 첫머리에 옴: `제28조의8(…한정한다)을 적용하지` | 괄호 뒤에 공백/줄 끝이 오고, 조 번호가 줄지 않을 때만 조 제목으로 인정 |
| 삭제된 조 `제8조 삭제 <2020. 2. 4.>` (제목 괄호 없음), 삭제된 항 `③ 삭제<…>` | 삭제 조는 청크를 만들지 않고, 삭제 항은 본문에서 뺀다 |
| 개정 이력 `<개정 2014. 3. 24.>`, `[본조신설 …]` | 본문에서 빼서 메타데이터로 (임베딩 잡음 제거) |
| 단어 중간 줄바꿈 `해\n당하는` | 공백 없이 잇는다. 단어 사이 줄바꿈의 공백은 되살릴 수 없음(`통하여안전하게`) — 알려진 한계 |

청킹은 **조 단위**가 기본이고, 1,500자를 넘으면 항 → 호 단위로 나눕니다(호로 나눌 때는 항의 첫 문장을 반복). 청크마다 `[개인정보 보호법 제3장 … 제24조의2(주민등록번호 처리의 제한)] · 2027-03-09 시행 예정` 같은 머리말을 붙여 임베딩합니다.
결과: 본문 조 136(현행 125 · 시행 예정 9 · 삭제 2) + 부칙 4 → **청크 143개**.

파서는 조 번호가 빠지거나, 같은 조가 시행일 표시 없이 두 번 나오거나, 머리말이 남으면 **멈춥니다.** 양식이 다른 PDF로 잘못된 색인을 만들지 않기 위해서입니다.

### 1.2 색인과 검색

```bash
make rag-db-local                                   # pgvector 컨테이너 (localhost:55432)
make rag-index-local                                # PDF → bge-m3 → 적재 → 승격 (첫 실행 때 bge-m3 약 2.3GB 받음)
make rag-search-local Q="주민등록번호를 수집해도 되나요?"
```

- 143청크 CPU 임베딩 22 ~ 41초, 질의 1건 검색 0.08 ~ 0.15초 (Apple Silicon)
- 색인은 버전으로 쌓이고(`21445-20260911-<접미사>`), `active_index` 한 줄이 서비스 중인 버전을 가리킵니다. 승격 = 이 값을 바꾸는 것.
- **현행본과 같은 시행 예정 청크는 뺐습니다.** 제75조 시행 예정본은 제2항제15호만 바뀌었는데 4청크가 모두 들어가 있어서, "과태료는 최대 얼마?"의 top-5가 같은 내용으로 채워졌습니다. 바뀐 청크만 남겨 146 → 143개가 됐습니다.

### 1.3 평가 세트와 검색 평가

```bash
make rag-eval-local         # → data/law/eval_report.md
```

평가 세트(`ml/src/lawrag/eval/qa.jsonl`)는 **40문항**(답 있음 36 · 답 없음 4)이고, 질문은 법률 용어 대신 일상어로 썼습니다("14살 아이가 앱에 가입하려는데…"). 문항마다 정답 조와 답변에 들어가야 할 핵심 표현을 적었고, **핵심 표현이 정답 조 본문에 실제로 있는지 테스트로 검사**합니다.

| 지표 | 값 | 비고 |
|---|---|---|
| Recall@5 | **0.972** | 36문항 중 35개 |
| MRR@5 | 0.868 | |
| 시행 예정 문항 | 2/2 | 예정본 청크를 찾음 |
| 놓친 문항 | q08 "고객 정보를 제휴사에 **넘기려면**" | 정답 제17조(개인정보의 **제공**)가 7위. "제3자에게 제공하려면"으로 물으면 1위 — 일상어와 법률 용어의 차이 |
| 답 없음 구분 | 유사도 0.498 기준 정확도 0.975 | "주민등록증 재발급"(답 없음)이 0.542로 답 있는 질문 최저점(0.520)보다 높음 → 유사도만으로는 못 가름 |

그래서 유사도 기준(0.48)은 "확실히 관련 없음"만 걸러 LLM을 부르지 않는 데 쓰고, 나머지는 LLM이 근거를 보고 거절하게 했습니다. 평가 세트는 검색 결과에 맞춰 고치지 않았습니다.

### 1.4 LLM 생성과 생성 평가

```bash
make llm-local              # 터미널 1: mlx-lm 서버 127.0.0.1:18080 (Qwen3-8B-4bit, 약 4.6GB)
make rag-api-local          # 터미널 2: http://127.0.0.1:18000/rag/
make rag-gen-eval-local     # 40문항 생성 평가 → data/law/eval_generation.md (로컬에서 약 40분)
```

mlx-lm 서버는 vLLM과 같은 OpenAI 호환 API라, rag-api 코드는 `LLM_BASE_URL` · `LLM_MODEL`만 바꿔 EKS에서 그대로 씁니다.

질의 처리: 주민번호 마스킹(4주차 코드 재사용, LLM · 로그에도 가린 값만) → 검색 → 프롬프트(근거 조문만 · 인용 형식 · 시행 예정이면 시행일 · 모르면 정해진 문장) → 생성 → **인용 검증**(인용한 조가 검색된 조인지) → 시행 예정 안내.
시행 예정 조문을 쓰고도 시행일을 빼먹는 경우가 있어, **시행 예정 안내 문장은 코드가 붙입니다.**

| 지표 (로컬 Qwen3-8B-4bit) | 값 |
|---|---|
| 거절 정확도 (답 없음 4) | 1.0 (3개는 LLM 없이, "주민등록증 재발급"은 LLM이 거절) |
| 잘못된 거절 | 0 |
| 인용 정확도 · 정답 조 인용률 | 1.0 · 0.917 |
| 시행 예정 문항 시행일 명시 | 1.0 (코드가 붙인 안내 덕분) |
| 핵심 표현 포함률 | 0.54 |
| **답변을 직접 읽은 판정** | **정확 23 · 부분 6 · 오답 7** |

오답 예: 최대 과태료 "3천만원"(실제 5천만원), 시행일 "9월 10일"(실제 9월 11일 — "공포 후 6개월"을 모델이 계산), 주민번호를 "받아도 되지만"(원칙 금지를 뒤집음), 삭제 요구 질문에 처리정지 조문으로 답함.
**인용 검증은 "검색된 조를 인용했는지"만 봅니다.** 오답 대부분이 정답 조를 정확히 인용했으므로, 승격 기준으로는 필요조건일 뿐이고 핵심 표현 포함률과 리포트의 답변 원문을 함께 봐야 합니다. 개선 과제(숫자 · 날짜는 조문 그대로, 원칙 먼저 쓰기, EKS FP8 모델과 비교)는 PLAN.md 10.6에 있습니다.

## 2. EKS에 올리기 (EKS 실측)

> 아래 절차를 2026-11-26 ~ 27에 새 클러스터에서 처음부터 끝까지 실행했습니다. 실측값은 [2.5](#25-eks-실측-2026-11-26--27)에 있습니다.

### 2.1 사전 조건

4 · 5주차와 같은 EKS + Kubeflow 환경이 필요합니다.

```bash
make infra        # 6주차 변경 포함: NodePool gpu-serving, ECR rag, Pod Identity(law-llm · rag-api), NVIDIA 플러그인 배치
make images       # (4 · 5주차 이미지)
make kubeflow && make ingress && make mlflow && make extras
```

일반 노드 2대로도 6주차 전 과정이 돌았습니다(EKS 실측 — 색인 파이프라인의 embed 단계만 임시 CPU 노드 `burst`로 감). 4 · 5주차 실습을 함께 띄워 두면 `cpu_node_count = 3`을 권합니다.

### 2.2 순서

```bash
make images-rag                   # rag 이미지 → ECR (linux/amd64)
make rag-data                     # PDF → s3://<bucket>/datasets/law/21445-20260911/source.pdf
make llm-cache                    # Qwen3-8B-FP8 · bge-m3 가중치 Hugging Face → S3 models/ (클러스터 Job, 한 번만)
make rag-platform                 # ragdb(pgvector) + rag-api + 접근 정책
make rag-index NO_GEN=1           # 첫 색인: 검색 평가만으로 승격 (LLM 없이)
make llm                          # law-llm 배포 → GPU 노드 기동 → Ready 대기 (EKS 실측 약 11분)
make rag-ask Q="14살 아이가 앱에 가입하려면 보호자 동의가 필요한가요?"
make rag-index                    # 생성 평가까지 포함한 색인 (KFP 캐시로 파싱 · 임베딩은 건너뜀)
make rag-status
make llm-down                     # ← 끝나면 꼭. GPU 온디맨드 시간당 약 1달러
```

브라우저: `http://<ALB>/` 로그인 후 `http://<ALB>/rag/` (`make info`가 주소와 law-llm 경고를 출력).

### 2.3 LLM 서빙 — 왜 이렇게 했나

| 결정 | 이유 |
|---|---|
| KServe **커스텀 컨테이너**로 vLLM 이미지 지정 | 내장 Hugging Face 런타임은 vLLM 버전(0.24 계열)을 고를 수 없음 |
| 배포 모드 **Standard** (Knative 아님) | 클러스터 내부 전용이라 scale-to-zero · 카나리가 필요 없음. Knative는 init container를 기본으로 막고, 첫 기동(이미지 10GB + 가중치 9GB)이 기한을 넘기기 쉬움. KServe 0.21의 값은 `Standard` · `Knative` |
| 가중치는 **S3 캐시 + init container(aws-cli)** | 노드가 뜰 때마다 Hugging Face에서 받지 않게. KServe storage-initializer는 Pod Identity 자격 증명 주입을 장담할 수 없어 직접 받음 |
| init container를 **UID 1337**로 | Istio CNI가 앱 init container 트래픽을 아직 없는 사이드카로 돌리는 경우를 대비. EKS 실측에서는 Istio가 **네이티브 사이드카**(istio-proxy가 init container로 먼저 뜸)로 동작해 필요 없었지만, 해는 없어 그대로 둠 |
| NodePool **gpu-serving** (온디맨드 g6.xlarge, 상한 1) | 학습용 `gpu`(Spot, 쓰고 끄기)와 분리. FP8 가중치 약 9GB → L4 24GB에 KV 캐시 9GiB. **EKS 실측: vLLM 0.33은 L4에서 FP8 연산이 아닌 가중치 전용 FP8(Marlin)을 씀** — 메모리 절약만 얻음 |
| NVIDIA device plugin을 `node-role In [gpu, gpu-serving]`에 | 기존 설정(`node-role=gpu`만)이면 서빙 노드에 `nvidia.com/gpu`가 안 생겨 파드가 영원히 Pending |
| law-llm은 **cluster-local + rag-api principal만** 허용 | 4주차 추론 서비스의 "모두 허용"보다 좁힘. 외부에는 rag-api만 노출 |
| CPU · 메모리 **상한을 반드시 명시** | 비워 두면 KServe가 기본값(cpuLimit 1, memoryLimit 2Gi)을 채워, 요청(cpu 2)보다 작아 Deployment가 거부됨 (EKS 실측) |

### 2.4 색인 파이프라인

```
parse-pdf → chunk → embed → load-index → eval-retrieval ─┬ Recall@5 ≥ 0.8 ─┬ eval-generation → 인용 정확도 ≥ 0.9 → promote
 (KFP 캐시)  (캐시)   (캐시)  (새 색인 버전)                   │                 └ (NO_GEN=1) ───────────────────→ promote
                                                         └ 미달: 종료 — 서비스 중 색인은 그대로
```

- 새 색인은 **적재만 하고 서비스하지 않습니다.** 생성 평가는 rag-api에 `index_version`을 지정해 **승격 전 색인으로** 답하게 해서 잽니다(게이트웨이 경유, 파드 SA 토큰).
- MLflow 실험 `law-rag-index`에 색인 버전마다 실행 하나 — PDF SHA-256, 청크 수, 지표, 승격 여부, 교체된 이전 버전.
- 생성 평가는 law-llm이 떠 있어야 해서, `make rag-index`는 law-llm이 Ready가 아니면 제출을 거부하고 `NO_GEN=1`을 안내합니다.
- 법이 개정되면: 새 PDF를 `docs/`에 넣고 `make rag-data PDF=…` → `make rag-index`. 파이프라인 없이 Job 하나로 하려면 `make rag-index-job`.

### 2.5 EKS 실측 (2026-11-26 ~ 27)

| 단계 | 소요 · 결과 |
|---|---|
| `make infra` | 약 12분. NodePool `gpu-serving`, device plugin 배치, Pod Identity(`law-llm` · `rag-api`), ECR `mlops/rag` 반영 확인 |
| `make images` · 플랫폼 | 22분 · 약 8분 |
| `make llm-cache` | bge-m3 2.30GB 87초, Qwen3-8B-FP8 9.45GB 4분 40초 |
| `make rag-index NO_GEN=1` | 7분 (embed 3분 41초 — CPU 2코어 임베딩 120초). Recall@5 **0.9722** · MRR@5 0.8681 — 로컬과 같음 |
| `make llm` | 적용 → Ready **약 11분**: GPU 노드 46초 · S3 가중치 75초 · **vLLM 이미지(Docker Hub 9.74GB) 4분 55초** · vLLM 로딩 약 3분 |
| law-llm 자원 | CUDA 드라이버 580.178.04 + torch cu130 호환, GPU 메모리 19.5 / 23GiB(가중치 8.9 · KV 캐시 9.06GiB = 65,968토큰, 8K 요청 동시 8개) |
| `make rag-ask` | 200 OK, 약 3초(검색 0.16 + 생성 2.7초). 첫 질의만 12초(검색 초기화 8.4초). 토큰 없이 403 |
| `make rag-index` (생성 평가) | 6분 21초 — parse · chunk · embed **KFP 캐시 0초**, 생성 평가 40문항 3분 21초 → 새 색인 승격 · 이전 색인 교체 |
| 생성 평가 (FP8) | 인용 1.0 · 거절 1.0 · 정답 조 인용 0.944 · 핵심 표현 0.628 · 직접 판정 **정확 26 · 부분 5 · 오답 5** (로컬 4bit 23 · 6 · 7) |

평가 세트 밖의 질문 3개도 `make rag-ask`로 확인했습니다(EKS 실측).

| 질문 | 결과 | 시간 |
|---|---|---|
| 직원 채용 때 지원자의 건강 정보를 받아도 되나요? | 제23조 · 제15조 인용, 원칙 금지 · 예외 설명 (단 "별도로" 받는 동의라는 점은 빠짐) | 5.8초 |
| 주민번호 `900101-1234567`이 든 파일이 유출됐어요 | 통지 · 신고 · 회수 · 300만원 배상까지 맞음. 트레이스에는 `900101-1******`로만 기록. **인용은 비어 있음** — 조문 번호 대신 `[근거 2]`처럼 프롬프트의 근거 번호를 씀 | 5.5초 |
| 부동산 중개수수료는 얼마까지? | 유사도 0.529(기준 0.48 초과)로 LLM까지 갔고, LLM이 거절 | 2.6초 |

매번 토큰 없는 호출은 403이었습니다. `[근거 N]` 표기는 생성 평가의 일부 답변에도 섞여 있었고, 인용 추출에 걸리지 않아 인용 검증이 그냥 통과합니다(다음 과제).

EKS에서 처음 실행하며 고친 것:

| # | 증상 | 원인 → 조치 |
|---|---|---|
| ① | `make images`가 `No space left on device` | 로컬 colima Docker 디스크 가득 참 → 예전 이미지 · 참조 끊긴 빌드 캐시 정리 |
| ② | `make infra` 뒤 AWS provider 6.73.0 → 6.74.0 | `terraform init -upgrade`가 lock 파일 무시 → lock 복원, `-upgrade` 제거 |
| ③ | rag-api가 Ready가 안 됨(readiness 500) | 새 ragdb에 테이블 없음(스키마는 색인 때만 생성) → rag-api 시작 시 스키마 생성 |
| ④ | law-llm Deployment 생성 거부 | CPU 상한을 비워 KServe 기본 cpuLimit 1이 들어감 → `limits.cpu: 4` 명시 |
| ⑤ | law-llm은 Ready인데 `make llm`이 안 끝남 | 재적용 후 KServe가 `observedGeneration`을 갱신하지 않아 `kubectl wait --for=condition`이 무시 → 롤아웃 + jsonpath로 대기 |

다음에 바꿀 것: vLLM 이미지를 ECR에 복사(기동 11분 중 5분), rag-api 시작 시 임베딩 미리 계산(첫 질의 8.4초), 숫자 · 날짜 · 원칙 오류 5건을 겨냥한 프롬프트 개선 후 재평가, 답변의 `[근거 N]`을 조문 번호로 바꾼 뒤 인용 검증.

## 3. 노드 구성 — Karpenter

| 노드 | 관리 | 언제 떠 있나 | 무엇이 올라가나 |
|---|---|---|---|
| 일반 `m6i.xlarge` × 2(6주차는 3 권장) | EKS 관리형 노드그룹, 온디맨드 | 항상 | Kubeflow · MLflow · ragdb · **rag-api** · 추론 파드 |
| GPU 학습 | NodePool `gpu` (Spot 우선) | GPU 학습 파드가 있을 때만 | 4주차 신분증 학습 |
| 임시 CPU | NodePool `burst` (Spot 우선) | 파이프라인 단계가 일반 노드에 못 들어갈 때만 | `workload=batch` 허용 파드 (색인 파이프라인 단계 포함) |
| **GPU 서빙** | NodePool `gpu-serving` (**온디맨드** g6.xlarge) | `make llm` ~ `make llm-down` | **law-llm (vLLM)** |

설정은 `terraform.tfvars`에서 바꿉니다: `llm_capacity_types = ["spot", "on-demand"]`(서빙도 Spot — 회수되면 끊김), `llm_instance_types = ["g6.2xlarge"]`(메모리가 모자랄 때), `enable_llm_serving = false`(NodePool 안 만듦).

5주차까지 EKS에서 확인한 Karpenter 동작: 평소 Karpenter 노드 0대, GPU 학습 파드 Pending → Spot `g4dn.xlarge` 약 45초 만에 준비, 학습 종료 5분 뒤 노드 회수. 모든 AWS 자원에는 `Owner=sejkim` 태그가 붙습니다.

## 4. 비용과 정리

| 항목 (서울, 시간당 USD, 대략) | 평소 | 6주차 LLM 기동 중 |
|---|---|---|
| EKS 컨트롤 플레인 · NAT · ALB | 약 0.19 | 약 0.19 |
| 일반 노드 `m6i.xlarge` | × 2 약 0.48 | × 3 약 0.72 |
| GPU 서빙 `g6.xlarge` 온디맨드 | 0 | 약 1.0 (Spot이면 약 0.47) |
| **합계** | **약 0.7** | **약 2.0** — 4시간 실습이면 약 8 |

S3 모델 캐시(약 12GB)와 ragdb PVC(10GiB)는 월 1달러 미만입니다. 위 금액은 단가를 더한 추정이고 실제 청구액이 아닙니다.

```bash
make llm-down      # LLM만 내림 → 5분 뒤 GPU 서빙 노드 삭제 (make nodes로 확인)
make destroy       # 전체 삭제 (ALB · PVC · Karpenter 노드 정리 후 terraform destroy, S3 · ECR 포함)
```

## 5. 주의할 점 · 한계

- **법률 자문이 아닙니다.** 모든 응답과 화면에 안내 문구를 붙입니다. 생성 평가에서 36개 중 오답이 로컬 4bit 7개, EKS FP8 5개였습니다(최대 과태료 · 시행일 · 원칙 오류는 둘 다 틀림).
- **범위는 법률 PDF 하나입니다.** "대통령령으로 정한다"로 끝나는 세부 내용(시행령 · 고시)은 없습니다. 같은 방식으로 PDF를 추가해 넓힐 수 있습니다.
- **일상어 질문에 약합니다**(q08 "넘기려면" ↔ "제공"). 개선 후보: LLM으로 질문을 법률 용어로 바꿔 검색, 키워드 검색 병행.
- **현행 · 시행 예정이 섞인 조**(제2조처럼)는 답이 어느 쪽을 썼는지 코드로 가릴 수 없어 "개정 내용이 있다"고만 알립니다.
- 질문에 실제 개인정보를 넣지 마세요. 주민번호 형태는 가리지만 다른 개인정보는 가리지 않습니다.
- 로컬 LLM이 매우 느리면(문항당 수십 초 이상) 다른 컨테이너(kind 클러스터 등)가 CPU · 메모리를 쓰고 있지 않은지 확인하세요. 이 실습에서 kind를 멈추자 짧은 요청이 8 ~ 40초 → 0.5초로 빨라졌습니다. 긴 프롬프트(약 1,900토큰) 처리는 그래도 수십 초 걸립니다.

## 문제 해결

### LLM RAG (6주차)

★ 표시는 EKS에서 실제로 겪고 고친 것입니다. 나머지 EKS 항목은 예상한 대응입니다.

| 증상 | 확인할 것 |
|---|---|
| law-llm 파드가 계속 `Pending` | `kubectl get nodes -l node-role=gpu-serving` — 노드가 없으면 `kubectl get nodeclaims`, Karpenter 로그. 노드는 있는데 Pending이면 `kubectl describe node`에 `nvidia.com/gpu`가 있는지 (없으면 `make infra`로 NVIDIA 플러그인 배치 갱신) |
| law-llm init container(`fetch-model`) 실패 | `kubectl logs <파드> -c fetch-model` — AccessDenied면 `aws eks list-pod-identity-associations --cluster-name mlops`에 `law-llm`이 있는지, 타임아웃이면 Istio 가로채기(PLAN.md 10.12의 대안), 캐시가 없으면 `make llm-cache` |
| law-llm이 Ready가 안 됨 / `OOMKilled` | `kubectl logs <파드> -c kserve-container` — CUDA 드라이버 오류면 vLLM 이미지 버전, 메모리 부족이면 `llm_instance_types = ["g6.2xlarge"]` |
| ★ law-llm `Ready=False`, 파드가 아예 없음 (`ReconcileFailed … must be less than or equal to cpu limit`) | 템플릿의 `limits.cpu`가 빠진 것 — KServe가 기본 cpuLimit 1을 채움. `kubectl -n kserve get cm inferenceservice-config -o jsonpath='{.data.inferenceService}'`로 기본값 확인 |
| ★ law-llm은 Ready인데 `make llm`이 끝나지 않음 | 예전 `install.sh`의 `kubectl wait --for=condition=Ready` 문제(observedGeneration). Ctrl-C 해도 law-llm은 정상 — 지금 코드는 롤아웃 + jsonpath로 대기 |
| ★ rag-api가 `1/2`에서 멈춤 (readiness 500, `relation "active_index" does not exist`) | 예전 rag 이미지. 지금 코드는 시작 시 스키마를 만든다 — `make images-rag` 후 `kubectl -n kubeflow-user-example-com rollout restart deploy/rag-api` |
| 첫 질의만 10초 이상 걸림 | rag-api 임베딩 초기화(EKS 실측 8.4초). 두 번째부터 검색 0.2초 안팎 |
| InferenceService가 `deploymentMode` 때문에 거부됨 | `kubectl -n kserve get cm inferenceservice-config -o yaml`에서 지원 값 확인 (KServe 0.21: `Standard` · `Knative`) |
| `/rag/ask`가 503 "서비스 중인 색인이 없습니다" | 색인을 아직 승격하지 않은 것 — `make rag-index NO_GEN=1` 또는 `make rag-index-job`, `make rag-status`로 확인 |
| `/rag/ask`가 403 | `kubectl -n kubeflow-user-example-com get authorizationpolicy allow-rag-api`, 토큰(`make rag-ask`는 default-editor SA 토큰 사용) |
| 답이 항상 "찾지 못함" | law-llm 연결 실패가 아니라면 `top_score`가 `RAG_MIN_SCORE`(0.48)보다 낮은 것. rag-api 로그와 응답의 `skipped_llm` 확인 |
| `make rag-index`가 제출을 거부 | 의도된 동작 — law-llm이 Ready가 아님. `make llm` 후 다시, 또는 `NO_GEN=1` |
| 파이프라인이 eval-retrieval에서 끝남 (promote 없음) | Recall@5가 기준 미달 — 리포트 확인. 서비스 중 색인은 그대로 |
| parse-pdf 단계 실패 (`ParseError`) | 양식이 다른 PDF(다른 출력 양식, 스캔본). 메시지의 조 번호 · 중복 · 머리말 정보 확인 |
| 로컬 `make rag-gen-eval-local`이 DB 오류로 중단 | Docker(colima)를 멈추면 pgvector 컨테이너도 멈춤 — `make rag-db-local`로 다시 띄우면 데이터는 그대로 |

### 인프라 · 공통

| 증상 | 확인 |
|---|---|
| ALB 접속 시 타임아웃 | 내 공인 IP가 `allowed_cidrs`와 같은지 (`curl ifconfig.me`) |
| 로그인 후 계속 로그인 화면 | HTTP 접속이므로 `FORCE_HTTPS=false` 패치 확인: `kubectl -n oauth2-proxy get cm -o yaml \| grep FORCE` |
| 파이프라인 제출에서 401/403 | `kubectl -n kubeflow-user-example-com create token default-editor` 동작 확인, `kubectl -n istio-system get requestauthentication m2m-token-issuer -o yaml`의 issuer가 `terraform output oidc_issuer_url`과 같은지 |
| 플랫폼 파드 · rag-api Pending | 일반 노드가 꽉 찬 것. `cpu_node_count = 3` 또는 `cpu_instance_type = "m6i.2xlarge"` 후 `make infra` |
| 새 클러스터 첫 실행에서 MLflow 아티팩트 다운로드 실패 | 새 S3 버킷의 전역 주소가 한동안 `us-east-1`로 연결됨. MLflow 파드에 `MLFLOW_S3_ENDPOINT_URL`이 있는지 (`make mlflow`) |
| MLflow 파드 `OOMKilled` | `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` 적용 여부 (`make mlflow`) |
| `make infra`에서 `Error locating chart … no cached repo found` | `helm repo update` 후 다시 실행 |
| Public ECR 차트 403 (`authorization token has expired`) | 로컬에 남은 만료 토큰. Terraform은 매번 새 토큰을 쓰지만, 직접 `helm pull` 할 때는 `helm registry logout public.ecr.aws` |
| S3 AccessDenied | `aws eks list-pod-identity-associations --cluster-name mlops`에 해당 네임스페이스 · SA가 있는지 |

이미 만든 클러스터의 일반 노드 수를 바꿀 때는 먼저 AWS CLI로 노드그룹 크기를 맞춥니다(EKS 모듈은 생성 후 `desired_size` 변경을 무시하고, `min_size`만 올리면 AWS가 거부합니다).

```bash
NG=$(aws eks list-nodegroups --cluster-name mlops --query "nodegroups[?starts_with(@,'cpu-')]|[0]" --output text)
aws eks update-nodegroup-config --cluster-name mlops --nodegroup-name "$NG" \
  --scaling-config minSize=3,maxSize=3,desiredSize=3
aws eks wait nodegroup-active --cluster-name mlops --nodegroup-name "$NG"
make infra   # Terraform 상태와 맞춘다
```

## 부록 A: 5주차 Tensorboard + 영화 추천

Kubeflow에 **Tensorboard 컨트롤러 · 웹 앱**을 추가로 설치하고, MovieLens BPR 행렬 분해 학습 로그를 **Tensorboard 인스턴스**로 확인합니다. 자세한 내용은 [5주차 글](https://velog.io/@sejkim/5%EC%A3%BC%EC%B0%A8-EKS-Kubeflow%EC%97%90-TensorBoard-%EC%9D%B8%EC%8A%A4%ED%84%B4%EC%8A%A4-%EB%9D%84%EC%9A%B0%EA%B8%B0-%EC%98%81%ED%99%94-%EC%B6%94%EC%B2%9C-%EB%AA%A8%EB%8D%B8%EC%9D%98-%ED%95%99%EC%8A%B5-%EA%B3%BC%EC%A0%95%EC%9D%84-%EB%88%88%EC%9C%BC%EB%A1%9C-%ED%99%95%EC%9D%B8%ED%95%98%EA%B8%B0)과 PLAN.md 3.2절을 참고하세요.

```bash
make movierec-local && .venv/bin/tensorboard --logdir runs/movierec   # 로컬 (13초, http://localhost:6006)
make tensorboard     # 컨트롤러 · 웹 앱 · 대시보드 메뉴 · 로그 PVC movierec-tb
make movierec        # 파이프라인: prepare-data → train-sweep (EMBED 16/32/64, EKS 3분 18초)
make tb              # 인스턴스 생성 → http://<ALB>/tensorboard/kubeflow-user-example-com/movierec/ (약 1분)
make tb-delete       # 인스턴스 삭제 (로그는 PVC에 남음)
```

| 임베딩 차원 | HR@10 | NDCG@10 |
|---|---|---|
| 16 | 0.7033 | 0.4526 |
| 32 | 0.7148 | 0.4657 |
| 64 | **0.7197** | **0.4720** |

꼭 알아둘 것:

- **실행 순서**: 로그 PVC는 EBS(ReadWriteOnce)라 학습 파드와 Tensorboard 파드가 같은 노드에 있어야 합니다. **학습 먼저, 인스턴스는 나중.** 인스턴스가 떠 있으면 `make movierec`이 제출을 거부합니다(`FORCE=1`로 무시, 대신 `Multi-Attach error` 위험).
- HTTP 접속이라 웹 앱의 Secure 쿠키를 껐습니다(`APP_SECURE_COOKIES=false`). 운영에서는 HTTPS + 기본값.
- 학습 · Tensorboard 파드는 일반 노드에만 둡니다(Karpenter 임시 노드의 taint를 Tensorboard 파드가 허용할 수 없음).
- 뷰어 이미지는 `tensorflow/tensorflow:2.5.1`이지만 `tensorboard 2.21.0` 로그를 문제없이 읽었습니다. Projector는 `INACTIVE` 목록 안에 있습니다.

| 증상 | 확인 |
|---|---|
| 메뉴에 Tensorboards가 없다 | `http://<ALB>/_/tensorboards/`. `make kubeflow`를 다시 했다면 `make tensorboard`도 다시 |
| 목록은 보이는데 생성 403 | 웹 앱 `APP_SECURE_COOKIES`가 `false`인지 |
| "No dashboards are active" | `logspath` 하위 경로(`movierec`)와 학습 로그 폴더가 같은지 |
| 학습 파드 `ContainerCreating`에서 멈춤 | Tensorboard 인스턴스가 PVC를 잡고 있음 → `make tb-delete` |

## 부록 B: 4주차 신분증 인식 파이프라인

합성 신분증 이미지로 검출 모델을 학습 → MLflow 등록 → BentoML 패키징 → KServe 서빙까지 자동화합니다. 자세한 내용은 [4주차 글](https://velog.io/@sejkim/4%EC%A3%BC%EC%B0%A8-EKS-%EC%9C%84%EC%97%90-Kubeflow-MLflow-BentoML-KServe%EB%A1%9C-%EC%8B%A0%EB%B6%84%EC%A6%9D-%EC%9D%B8%EC%8B%9D-MLOps-%ED%8C%8C%EC%9D%B4%ED%94%84%EB%9D%BC%EC%9D%B8-%EB%A7%8C%EB%93%A4%EA%B8%B0)을 참고하세요.

```
data-gen → validate → train(GPU, Karpenter Spot) → evaluate ─(mAP@0.5≥기준)→ register(MLflow)
→ bento-build → image-build(BuildKit→ECR) → deploy(KServe, 카나리) → smoke-test → promote
```

```bash
make pipeline                                # 기본: 3000장, 10 epoch (EKS 실측 28분 24초)
EPOCHS=3 NUM_TRAIN=600 make pipeline         # 빠른 확인용
make test-infer                              # 합성 신분증 1장 생성 후 ALB로 추론 호출
kubectl -n kubeflow-user-example-com get isvc idcard-recognizer   # 두 번째 실행부터 카나리 90 / 10
```

| 증상 | 확인 |
|---|---|
| smoke-test/추론이 `413 Payload Too Large`(HTML) | Host 헤더가 `example.com`(isvc `status.url`의 호스트)인지 — 아니면 대시보드로 라우팅됨 |
| 추론 `401 Jwt issuer is not configured` | `kubectl -n istio-system get requestauthentication cluster-local-gateway-jwt -o yaml`에 EKS issuer 규칙이 있는지 (`make kubeflow` 재실행) |
| image-build 실패 | `kubectl -n buildkit logs deploy/buildkitd` |
| InferenceService가 Ready 안 됨 | `kubectl -n kubeflow-user-example-com describe ksvc idcard-recognizer-predictor`, 이미지 pull 오류면 Knative `registries-skipping-tag-resolving` 설정 |
| GPU 학습 파드 Pending | 노드가 뜨는 중이면 정상(수 분). 오래 걸리면 `kubectl get nodeclaims`, Karpenter 로그 — Spot 용량 · `gpu_node_limit` 확인 |

## 검증 범위

- **6주차 — 로컬에서 확인 (2026-11-25 ~ 26)**: PDF 파싱 · 청킹(143청크), bge-m3 + pgvector 색인 · 검색, 검색 평가(Recall@5 0.972), mlx-lm(Qwen3-8B-4bit)으로 rag-api 전체 흐름과 40문항 생성 평가 + 답변 직접 판정, rag 이미지 빌드(arm64) · 컨테이너 안 실행, 색인 파이프라인 컴파일 · KFP 로컬 실행기(parse → chunk) · 로컬 DB로 전 단계(적재 → 평가 → 승격 → 복구), 테스트 82개 + DB 통합 2개.
- **6주차 — EKS에서 확인 (2026-11-26 ~ 27)**: `make infra`부터 `make rag-index`(생성 평가 포함)까지 전 단계 — 가중치 캐시, ragdb · rag-api, 색인 파이프라인 2회(KFP 캐시 · 승격 · 교체), law-llm 기동(CUDA 드라이버 · 메모리 · S3 접근 · Standard 모드), ALB 경유 질의와 인증 거부(403), FP8 생성 평가와 답변 직접 판정. 실행 중 고친 것 5건.
- **6주차 — 아직 확인하지 않은 것**: law-llm AuthorizationPolicy의 거부 동작(rag-api 외 호출), 브라우저 질의 화면(`/rag/`), Spot 서빙 노드, 프롬프트 개선 후 재평가, `make llm-down` · `make destroy` 후 잔존 자원.
- **5주차 — EKS에서 확인** (2026-11-16): Karpenter(GPU Spot · 임시 노드 기동과 회수), `make tensorboard`, `make movierec`, `make tb` 인스턴스 접속, `make destroy`.
- **4주차 — EKS에서 확인** (2026-11-13 ~ 16): 신분증 파이프라인 20/20, ALB 경유 추론.
- 비용은 단가를 더한 추정이고 실제 청구액이 아닙니다.
