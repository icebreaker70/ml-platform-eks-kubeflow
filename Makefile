SHELL := /bin/bash
.DEFAULT_GOAL := help
TF := terraform -chdir=infra/terraform
ENV := source scripts/env.sh

.PHONY: help check-tools venv test local-demo infra kubeconfig images kubeflow ingress mlflow extras \
        platform compile pipeline info test-infer destroy \
        movierec-local tensorboard movierec tb tb-delete tb-status nodes \
        rag-parse-local

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

rag-parse-local: ## 법률 PDF 파싱 · 청킹 → data/law/{articles,chunks}.jsonl + report.md (PDF="..." 로 교체)
	PYTHONPATH=ml/src .venv/bin/python -m lawrag.prepare --pdf "$${PDF:-$(LAW_PDF)}" --out data/law

# ---------------------------------------------------------------- AWS ----
infra: ## Terraform: VPC, EKS(1.36), 노드그룹, S3, ECR, IAM, ALB 컨트롤러, NVIDIA 플러그인
	$(TF) init -upgrade
	$(TF) apply
	$(MAKE) kubeconfig
	bash platform/karpenter/apply.sh

nodes: ## 노드 · Karpenter NodePool · 구매 방식(Spot/온디맨드) 확인
	@bash platform/karpenter/apply.sh status

kubeconfig: ## kubeconfig 갱신
	$$($(TF) output -raw configure_kubectl)

images: ## 컴포넌트 이미지 빌드/푸시 (idcard-ml, idcard-builder, mlflow-server)
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

info: ## 접속 주소 출력
	@bash scripts/info.sh

test-infer: ## ALB 경유 추론 테스트 (IMG=경로 생략 시 합성 이미지)
	bash scripts/test-infer.sh $(IMG)

destroy: ## 전체 삭제 (ALB/EBS 정리 후 terraform destroy)
	bash scripts/destroy.sh
