"""KFP v2 컴포넌트 정의.

컴포넌트 함수는 얇게 두고, 실제 로직은 이미지에 설치된 idcard 패키지(ml/src/idcard)를 호출한다.
베이스 이미지는 컴파일 시점의 환경 변수(IMAGE_ML, IMAGE_BUILDER)로 정한다 (pipelines/run.py가 설정).
"""

import os
from typing import NamedTuple

from kfp import dsl
from kfp.dsl import Artifact, Input, Markdown, Metrics, Output

IMAGE_ML = os.environ.get("IMAGE_ML", "idcard-ml:local")
IMAGE_BUILDER = os.environ.get("IMAGE_BUILDER", "idcard-builder:local")


# ---------------------------------------------------------------------------
# idcard-ml 이미지 (CUDA torch)
# ---------------------------------------------------------------------------
@dsl.component(base_image=IMAGE_ML, install_kfp_package=False)
def data_gen(bucket: str, dataset_version: str, num_train: int, num_val: int, seed: int) -> str:
    """합성 신분증 데이터셋 생성 → s3://<bucket>/datasets/idcard/<version>/{train,val}.tar.gz"""
    from idcard import steps

    return steps.data_gen(bucket, dataset_version, num_train, num_val, seed)


@dsl.component(base_image=IMAGE_ML, install_kfp_package=False)
def validate_data(dataset_uri: str, report: Output[Markdown]) -> bool:
    """데이터셋 품질 검사 (클래스 분포, 박스 유효성). 실패하면 파이프라인을 중단한다."""
    from idcard import steps

    ok, md = steps.validate_data(dataset_uri)
    with open(report.path, "w", encoding="utf-8") as f:
        f.write(md)
    if not ok:
        raise RuntimeError("데이터셋 검증 실패 — 리포트를 확인하세요")
    return ok


@dsl.component(base_image=IMAGE_ML, install_kfp_package=False)
def train(dataset_uri: str, mlflow_uri: str, epochs: int, batch_size: int, lr: float
          ) -> NamedTuple("TrainOutputs", [("run_id", str), ("model_uri", str)]):
    """GPU 학습 + MLflow 기록 (파라미터, 손실, state_dict, MLflow 모델)."""
    from collections import namedtuple

    from idcard import steps

    out = steps.train(dataset_uri, mlflow_uri, epochs, batch_size, lr)
    return namedtuple("TrainOutputs", ["run_id", "model_uri"])(out["run_id"], out["model_uri"])


@dsl.component(base_image=IMAGE_ML, install_kfp_package=False)
def evaluate(dataset_uri: str, mlflow_uri: str, run_id: str, metrics: Output[Metrics]
             ) -> NamedTuple("EvalOutputs", [("map50", float), ("card_type_accuracy", float)]):
    """검증셋 mAP / 신분증 종류 정확도 계산 → KFP Metrics + MLflow."""
    from collections import namedtuple

    from idcard import steps

    m = steps.evaluate(dataset_uri, mlflow_uri, run_id)
    for k, v in m.items():
        metrics.log_metric(k, round(float(v), 4))
    return namedtuple("EvalOutputs", ["map50", "card_type_accuracy"])(m["map50"], m["card_type_accuracy"])


# ---------------------------------------------------------------------------
# idcard-builder 이미지 (CPU torch + BentoML + EasyOCR + buildctl + kubernetes client)
# ---------------------------------------------------------------------------
@dsl.component(base_image=IMAGE_BUILDER, install_kfp_package=False)
def register_model(mlflow_uri: str, run_id: str, model_uri: str, model_name: str, map50: float) -> str:
    """MLflow Model Registry 등록 + alias 'staging'. 반환: 모델 버전."""
    from idcard import steps

    return steps.register(mlflow_uri, run_id, model_uri, model_name, map50)


@dsl.component(base_image=IMAGE_BUILDER, install_kfp_package=False)
def bento_build(mlflow_uri: str, model_name: str, model_version: str, bento: Output[Artifact]) -> str:
    """MLflow 모델 + EasyOCR 가중치 → Bento (KFP 아티팩트로 내보냄). 반환: Bento 태그."""
    from idcard import ops

    bento.metadata["model_version"] = model_version
    return ops.bento_build(mlflow_uri, model_name, model_version, bento.path)


@dsl.component(base_image=IMAGE_BUILDER, install_kfp_package=False)
def image_build(bento: Input[Artifact], image_repo: str, region: str, buildkit_addr: str) -> str:
    """BuildKit(buildkitd 데몬)으로 Bento 이미지를 빌드해 ECR에 푸시. 반환: 이미지 URI."""
    from idcard import ops

    return ops.image_build(bento.path, image_repo, region, buildkit_addr)


@dsl.component(base_image=IMAGE_BUILDER, install_kfp_package=False)
def deploy_kserve(isvc_name: str, image: str, model_version: str, canary_percent: int) -> str:
    """KServe InferenceService 생성(최초) 또는 카나리 배포(이후). 반환: 'initial' | 'canary'."""
    from idcard import ops

    return ops.deploy(isvc_name, image, model_version, canary_percent)


@dsl.component(base_image=IMAGE_BUILDER, install_kfp_package=False)
def smoke_test(isvc_name: str, gateway_url: str, deploy_mode: str, report: Output[Metrics]) -> float:
    """Istio 게이트웨이 경유 추론 확인 (응답 형식, 종류 정확도, 주민번호 마스킹)."""
    from idcard import ops

    s = ops.smoke_test(isvc_name, gateway_url)
    report.log_metric("smoke_card_type_accuracy", s["card_type_accuracy"])
    report.log_metric("smoke_avg_latency_ms", s["avg_latency_ms"])
    print(f"deploy mode: {deploy_mode}")
    return s["card_type_accuracy"]


@dsl.component(base_image=IMAGE_BUILDER, install_kfp_package=False)
def promote(isvc_name: str, mlflow_uri: str, model_name: str, model_version: str):
    """카나리 트래픽 100% 승격 + MLflow alias 'production'."""
    from idcard import ops

    ops.promote(isvc_name, mlflow_uri, model_name, model_version)
