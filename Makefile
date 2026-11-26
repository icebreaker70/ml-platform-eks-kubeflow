SHELL := /bin/bash
.DEFAULT_GOAL := help
TF := terraform -chdir=infra/terraform
ENV := source scripts/env.sh

.PHONY: help check-tools venv test local-demo infra kubeconfig images kubeflow ingress mlflow extras \
        platform compile pipeline info test-infer destroy \
        movierec-local tensorboard movierec tb tb-delete tb-status nodes \
        rag-parse-local rag-db-local rag-index-local rag-search-local rag-eval-local \
        llm-local rag-gen-eval-local rag-api-local \
        images-rag rag-data llm-cache rag-platform rag-index-job rag-index rag-compile llm llm-down rag-ask rag-status

help: ## 명령 목록
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

check-tools: ## 로컬 도구 확인
	@bash scripts/check-tools.sh

# ---------------------------------------------------------------- 로컬 ----
venv: ## 로컬 개발 가상환경(.venv) 생성
	uv venv -p 3.12 .venv && VIRTUAL_ENV=.venv uv pip install -r ml/requirements-dev.txt

test: ## 단위 테스트
	.venv/bin/python -m pytest -q ml/tests

local-demo: ## 로컬에서 소량 데이터 생성 → 1 epoch 학습 (CPU)
	PYTHONPATH=ml/src .venv/bin/python -m idcard.datagen --out data/train --n 60 --seed 1
	PYTHONPATH=ml/src .venv/bin/python -m idcard.datagen --out data/val --n 20 --seed 2
	MLFLOW_TRACKING_URI=sqlite:///mlflow.db PYTHONPATH=ml/src .venv/bin/python -m idcard.train \
	  --train-dir data/train --val-dir data/val --epochs 1 --batch-size 4 --num-workers 0

# ------------------------------------------------- LLM RAG (6주차, 로컬) ----
LAW_PDF ?= docs/개인정보 보호법(법률)(제21445호)(20260911).pdf

RAGDB_LOCAL_IMAGE ?= pgvector/pgvector:0.8.6-pg17
LAWRAG := PYTHONPATH=ml/src .venv/bin/python -m lawrag

rag-parse-local: ## 법률 PDF 파싱 · 청킹 → data/law/{articles,chunks}.jsonl + report.md (PDF="..." 로 교체)
	$(LAWRAG).prepare --pdf "$${PDF:-$(LAW_PDF)}" --out data/law

rag-db-local: ## 로컬 pgvector 컨테이너 (localhost:55432, 데이터는 Docker 볼륨 lawrag-pg)
	@docker start lawrag-pg >/dev/null 2>&1 || docker run -d --name lawrag-pg -p 127.0.0.1:55432:5432 \
	  -e POSTGRES_USER=rag -e POSTGRES_PASSWORD=rag -e POSTGRES_DB=rag -v lawrag-pg:/var/lib/postgresql/data \
	  $(RAGDB_LOCAL_IMAGE) >/dev/null
	@until docker exec lawrag-pg pg_isready -U rag -d rag >/dev/null 2>&1; do sleep 1; done; echo "lawrag-pg 준비됨"

rag-index-local: rag-db-local ## PDF → bge-m3 임베딩(CPU) → 로컬 pgvector 적재 + 승격
	$(LAWRAG).index load --pdf "$${PDF:-$(LAW_PDF)}" --promote

rag-search-local: ## 로컬 색인 검색 (Q="질문" K=5)
	$(LAWRAG).index search "$(Q)" -k $${K:-5}

rag-eval-local: ## 평가 세트로 로컬 색인 검색 평가 → data/law/eval_report.md, 지표를 색인 버전에 기록
	$(LAWRAG).evaluate -k $${K:-5} --save

# 로컬 LLM: Apple Silicon용 mlx-lm 서버 (OpenAI 호환 API — EKS의 vLLM과 같은 호출 방식)
MLX_LM_VERSION ?= 0.32.2
MLX_VERSION ?= 0.32.4
LOCAL_LLM_MODEL ?= mlx-community/Qwen3-8B-4bit

llm-local: ## 로컬 LLM 서버 (127.0.0.1:18080, Apple Silicon). 첫 실행 시 모델 약 4.6GB 내려받음
	uvx --from "mlx-lm==$(MLX_LM_VERSION)" --with "mlx==$(MLX_VERSION)" mlx_lm.server \
	  --model $(LOCAL_LLM_MODEL) --host 127.0.0.1 --port 18080

rag-gen-eval-local: ## 생성 평가 (make llm-local 필요) → data/law/eval_generation.md (ONLY=q01,u04 로 일부만)
	LLM_MODEL=$(LOCAL_LLM_MODEL) $(LAWRAG).evaluate -k $${K:-5} --generate --save $${ONLY:+--only $$ONLY}

rag-api-local: ## rag-api 로컬 실행 → http://127.0.0.1:18000/rag/ (make rag-db-local · llm-local 필요)
	LLM_MODEL=$(LOCAL_LLM_MODEL) PYTHONPATH=ml/src:ml .venv/bin/uvicorn rag_api.app:app --host 127.0.0.1 --port 18000

# ---------------------------------------------------------------- AWS ----
infra: ## Terraform: VPC, EKS(1.36), 노드그룹, S3, ECR, IAM, ALB 컨트롤러, NVIDIA 플러그인
	@# -upgrade를 쓰지 않는다: provider 버전은 커밋한 .terraform.lock.hcl로 고정 (올릴 때만 직접 terraform init -upgrade)
	$(TF) init
	$(TF) apply
	$(MAKE) kubeconfig
	bash platform/karpenter/apply.sh

nodes: ## 노드 · Karpenter NodePool · 구매 방식(Spot/온디맨드) 확인
	@bash platform/karpenter/apply.sh status

kubeconfig: ## kubeconfig 갱신
	$$($(TF) output -raw configure_kubectl)

images: ## 컴포넌트 이미지 빌드/푸시 (idcard-ml, idcard-builder, mlflow-server). 6주차 rag는 make images-rag
	bash scripts/build-images.sh

kubeflow: ## Kubeflow 26.10.0 필수 구성 설치
	bash platform/kubeflow/install.sh

ingress: ## ALB Ingress (HTTP, 허용 IP 제한)
	bash platform/ingress/apply.sh

mlflow: ## MLflow + PostgreSQL
	bash platform/mlflow/install.sh

extras: ## BuildKit 데몬 + KServe 접근 정책
	bash platform/install-extras.sh

platform: kubeflow ingress mlflow extras info ## 플랫폼 전체 설치

# ------------------------------------------------------------ 파이프라인 ----
compile: ## 파이프라인 컴파일 (pipelines/idcard_pipeline.yaml)
	$(ENV) && .venv/bin/python pipelines/run.py --compile-only

pipeline: ## 파이프라인 실행 제출 (EPOCHS=10 NUM_TRAIN=3000 DATASET_VERSION=v1)
	$(ENV) && .venv/bin/python pipelines/run.py --host "http://$$(alb_host)/pipeline" \
	  --epochs $${EPOCHS:-10} --num-train $${NUM_TRAIN:-3000} --num-val $${NUM_VAL:-500} \
	  --dataset-version $${DATASET_VERSION:-v1}

# ------------------------------------------- 영화 추천 + TensorBoard (5주차) ----
movierec-local: ## 로컬에서 MovieLens 학습 → runs/movierec (확인: .venv/bin/tensorboard --logdir runs/movierec)
	PYTHONPATH=ml/src .venv/bin/python -m movierec.train --logdir runs/movierec \
	  --emb-dims $${EMB_DIMS:-16,32,64} --epochs $${EPOCHS:-20}

tensorboard: ## Tensorboard 컨트롤러 + 웹 앱 + 로그용 PVC 설치 (make kubeflow 이후)
	bash platform/tensorboard/install.sh

movierec: ## 영화 추천 파이프라인 제출 (EPOCHS=20 EMB_DIMS=16,32,64)
	$(ENV) && .venv/bin/python pipelines/run_movierec.py --host "http://$$(alb_host)/pipeline" \
	  --epochs $${EPOCHS:-20} --emb-dims $${EMB_DIMS:-16,32,64} $${FORCE:+--force}

tb: ## Tensorboard 인스턴스 생성 (pvc://movierec-tb/movierec)
	bash platform/tensorboard/instance.sh create

tb-delete: ## Tensorboard 인스턴스 삭제 (로그는 PVC에 남음)
	bash platform/tensorboard/instance.sh delete

tb-status: ## Tensorboard 인스턴스 · PVC 상태
	bash platform/tensorboard/instance.sh status

# ------------------------------------------------- LLM RAG (6주차, EKS) ----
# 순서: images-rag → rag-data → llm-cache → rag-platform → rag-index-job → llm → rag-ask → (끝나면) llm-down
images-rag: ## rag 이미지 빌드/푸시 (rag-api · 색인 · 캐시 Job 공용, CPU torch)
	bash scripts/build-images.sh rag

rag-data: ## 개인정보 보호법 PDF → S3 datasets/law/<번호>-<시행일>/source.pdf (PDF="..." 로 교체)
	bash platform/rag/install.sh data $${PDF:+"$$PDF"}

llm-cache: ## LLM · bge-m3 가중치 Hugging Face → S3 models/ (클러스터 Job, 한 번만)
	bash platform/rag/install.sh cache

rag-platform: ## ragdb(pgvector) + rag-api + 접근 정책 (make llm-cache 이후)
	bash platform/rag/install.sh platform

rag-index-job: ## 색인 Job(파이프라인 없이 한 번에): PDF → 임베딩 → ragdb → 검색 평가 → Recall@5 ≥ GATE_RECALL이면 승격
	bash platform/rag/install.sh index

rag-index: ## 색인 KFP 파이프라인 제출 (생성 평가에 make llm 필요, 검색 평가만: NO_GEN=1)
	$(ENV) && .venv/bin/python pipelines/run_rag.py --host "http://$$(alb_host)/pipeline" \
	  --min-recall $${MIN_RECALL:-0.8} $${NO_GEN:+--no-generation}

rag-compile: ## 색인 파이프라인 컴파일만 (pipelines/rag_index_pipeline.yaml)
	$(ENV) && .venv/bin/python pipelines/run_rag.py --compile-only

llm: ## law-llm(vLLM, Qwen3-8B-FP8) 배포 → GPU 노드(g6.xlarge 온디맨드) 기동 → Ready 대기
	bash platform/rag/install.sh llm

llm-down: ## law-llm 삭제 → GPU 노드 정리 (실습 후 꼭 실행 — 시간당 약 $1)
	bash platform/rag/install.sh llm-down

rag-ask: ## ALB 경유 질의 (Q="질문")
	bash platform/rag/install.sh ask "$(Q)"

rag-status: ## ragdb · rag-api · law-llm · GPU 서빙 노드 · 색인 버전
	bash platform/rag/install.sh status

info: ## 접속 주소 출력
	@bash scripts/info.sh

test-infer: ## ALB 경유 추론 테스트 (IMG=경로 생략 시 합성 이미지)
	bash scripts/test-infer.sh $(IMG)

destroy: ## 전체 삭제 (ALB/EBS 정리 후 terraform destroy)
	bash scripts/destroy.sh
