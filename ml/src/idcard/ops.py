"""배포 단계 구현 (idcard-builder 이미지): Bento 빌드 → BuildKit 이미지 빌드/푸시 → KServe 배포 → 스모크 테스트 → 승격."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SERVING_DIR = Path(os.environ.get("IDCARD_SERVING_DIR", Path(__file__).resolve().parents[2] / "serving"))
SRC_DIR = Path(__file__).resolve().parent
BENTO_MODEL_NAME = "idcard-detector"
BENTO_SERVICE_NAME = "idcard-recognizer"
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def _run(cmd: list[str], **kw) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def current_namespace(default: str = "kubeflow-user-example-com") -> str:
    p = SA_DIR / "namespace"
    return p.read_text().strip() if p.exists() else default


# ---------------------------------------------------------------------------
# 1) Bento 빌드
# ---------------------------------------------------------------------------
def bento_build(mlflow_uri: str, model_name: str, model_version: str, export_path: str) -> str:
    """MLflow 모델 버전 + EasyOCR 가중치를 BentoML 모델 저장소에 넣고 Bento를 빌드해 export_path로 내보낸다."""
    import bentoml
    import easyocr
    import mlflow
    import torch
    from mlflow import MlflowClient

    from .labels import CLASSES
    from .model import MODEL_KWARGS
    from .model import STATE_DICT_ARTIFACT

    mlflow.set_tracking_uri(mlflow_uri)
    mv = MlflowClient().get_model_version(model_name, model_version)
    sd_path = mlflow.artifacts.download_artifacts(run_id=mv.run_id, artifact_path=STATE_DICT_ARTIFACT)
    torch.load(sd_path, map_location="cpu", weights_only=True)  # 가중치 파일 검증

    version = f"v{model_version}"
    with bentoml.models.create(
        f"{BENTO_MODEL_NAME}:{version}",
        labels={"mlflow_model": model_name, "mlflow_version": str(model_version)},
        metadata={"mlflow_run_id": mv.run_id, "classes": CLASSES, "model_kwargs": MODEL_KWARGS},
    ) as bento_model:
        shutil.copy(sd_path, os.path.join(bento_model.path, "model.pt"))
        ocr_dir = os.path.join(bento_model.path, "easyocr")
        os.makedirs(ocr_dir, exist_ok=True)
        # 이미지 빌드 시점에 OCR 가중치를 포함시켜 콜드스타트 때 다운로드하지 않도록 한다.
        easyocr.Reader(["ko", "en"], gpu=False, model_storage_directory=ocr_dir,
                       user_network_directory=os.path.join(tempfile.gettempdir(), "easyocr_user"),
                       download_enabled=True, verbose=False)
        with open(os.path.join(bento_model.path, "meta.json"), "w") as f:
            json.dump({"mlflow_model": model_name, "mlflow_version": str(model_version),
                       "mlflow_run_id": mv.run_id}, f)

    with tempfile.TemporaryDirectory() as ctx:
        for name in ("service.py", "bentofile.yaml"):
            shutil.copy(SERVING_DIR / name, Path(ctx) / name)
        shutil.copytree(SRC_DIR, Path(ctx) / "idcard",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "ops.py", "steps.py"))
        # 서비스가 빌드 시점과 실행 시점에 같은 모델 버전을 참조하도록 태그를 파일로 남긴다.
        (Path(ctx) / "model_tag.txt").write_text(f"{BENTO_MODEL_NAME}:{version}\n")
        _run(["bentoml", "build", "--version", version, "."], cwd=ctx)

    tag = f"{BENTO_SERVICE_NAME}:{version}"
    # export_bento는 경로가 .bento로 끝나지 않으면 확장자를 붙인다. KFP 출력 아티팩트 경로(확장자 없음)에
    # 그대로 쓰면 '<path>.bento'가 생겨 KFP가 업로드하지 못하므로, 붙여서 쓴 뒤 정확한 경로로 옮긴다.
    written = bentoml.export_bento(tag, f"{export_path}.bento")
    os.replace(written, export_path)
    print(f"[bento-build] {tag} → {export_path}", flush=True)
    return tag


# ---------------------------------------------------------------------------
# 2) BuildKit으로 컨테이너 이미지 빌드 + ECR 푸시
# ---------------------------------------------------------------------------
def _write_ecr_docker_config(registry: str, region: str) -> str:
    import boto3

    auth = boto3.client("ecr", region_name=region).get_authorization_token()["authorizationData"][0]
    cfg_dir = tempfile.mkdtemp(prefix="docker-")
    with open(os.path.join(cfg_dir, "config.json"), "w") as f:
        json.dump({"auths": {registry: {"auth": auth["authorizationToken"]}}}, f)
    return cfg_dir


def prepare_build_context(bento):
    """`bentoml containerize`와 같은 방식으로 빌드 컨텍스트를 만든다.

    Bento 디렉터리에는 모델이 들어 있지 않으므로(BentoML 1.4) 모델 저장소에서 models/로 복사하고
    Dockerfile을 다시 생성한다. (context_dir, dockerfile_path)를 내주는 context manager.
    """
    from bentoml._internal.container import construct_containerfile

    return construct_containerfile(bento, enable_buildkit=True)


def image_build(bento_archive: str, image_repo: str, region: str, buildkit_addr: str) -> str:
    import bentoml

    bento = bentoml.import_bento(bento_archive)
    tag = str(bento.tag.version)
    image = f"{image_repo}:{tag}"
    registry = image_repo.split("/")[0]
    env = {**os.environ, "DOCKER_CONFIG": _write_ecr_docker_config(registry, region)}
    with prepare_build_context(bento) as (context_dir, dockerfile_path):
        _run([
            "buildctl", "--addr", buildkit_addr, "build",
            "--frontend", "dockerfile.v0",
            "--local", f"context={context_dir}",
            "--local", f"dockerfile={os.path.dirname(dockerfile_path)}",
            "--opt", "platform=linux/amd64",
            "--output", f"type=image,name={image},push=true",
            "--export-cache", f"type=registry,ref={image_repo}:buildcache,mode=max,image-manifest=true,oci-mediatypes=true",
            "--import-cache", f"type=registry,ref={image_repo}:buildcache",
        ], env=env)
    print(f"[image-build] pushed {image}", flush=True)
    return image


# ---------------------------------------------------------------------------
# 3) KServe InferenceService 배포 (처음: 바로 배포, 이후: 카나리)
# ---------------------------------------------------------------------------
GROUP, VERSION, PLURAL = "serving.kserve.io", "v1beta1", "inferenceservices"


def _k8s():
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CustomObjectsApi()


def _container(image: str, model_version: str) -> dict:
    return {
        "name": "kserve-container",
        "image": image,
        "ports": [{"containerPort": 3000, "protocol": "TCP"}],
        "env": [{"name": "MODEL_VERSION", "value": str(model_version)},
                {"name": "OMP_NUM_THREADS", "value": "2"}],
        "readinessProbe": {"httpGet": {"path": "/readyz"}, "initialDelaySeconds": 10, "periodSeconds": 5,
                           "failureThreshold": 60},
        "resources": {"requests": {"cpu": "1", "memory": "3Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}},
    }


def _get_isvc(api, ns: str, name: str):
    from kubernetes.client.exceptions import ApiException

    try:
        return api.get_namespaced_custom_object(GROUP, VERSION, ns, PLURAL, name)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _wait_ready(api, ns: str, name: str, timeout: int, latest_percent: int | None = None) -> dict:
    """Ready이고 최신 리비전이 준비될 때까지 기다린다. latest_percent를 주면 최신 리비전의 트래픽 비율도 확인한다.
    (patch 직후에는 status가 이전 값 그대로라 Ready만 보면 바로 통과해 버린다.)"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        isvc = _get_isvc(api, ns, name) or {}
        status = isvc.get("status", {})
        conds = {c["type"]: c for c in status.get("conditions", [])}
        pred = status.get("components", {}).get("predictor", {})
        ready = conds.get("Ready", {}).get("status") == "True"
        rolled = pred.get("latestCreatedRevision") and pred.get("latestCreatedRevision") == pred.get("latestReadyRevision")
        print(f"[deploy] ready={ready} created={pred.get('latestCreatedRevision')} "
              f"readyRev={pred.get('latestReadyRevision')} traffic={pred.get('traffic')}", flush=True)
        latest = sum(t.get("percent", 0) for t in pred.get("traffic") or [] if t.get("latestRevision"))
        if ready and rolled and (latest_percent is None or latest == latest_percent):
            return isvc
        time.sleep(15)
    raise TimeoutError(f"InferenceService {ns}/{name}가 {timeout}초 안에 Ready가 되지 않았습니다")


def deploy(name: str, image: str, model_version: str, canary_percent: int, timeout: int = 1200,
           namespace: str | None = None) -> str:
    """반환값: 'initial' 또는 'canary'."""
    api = _k8s()
    ns = namespace or current_namespace()
    existing = _get_isvc(api, ns, name)
    container = _container(image, model_version)
    if existing is None:
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "InferenceService",
            "metadata": {"name": name, "namespace": ns,
                         "labels": {"app": name, "model-version": str(model_version)}},
            "spec": {"predictor": {"minReplicas": 1, "maxReplicas": 2,
                                   "scaleMetric": "concurrency", "scaleTarget": 4,
                                   "timeout": 120, "containers": [container]}},
        }
        api.create_namespaced_custom_object(GROUP, VERSION, ns, PLURAL, body)
        mode = "initial"
    else:
        patch = {"metadata": {"labels": {"model-version": str(model_version)}},
                 "spec": {"predictor": {"canaryTrafficPercent": int(canary_percent), "containers": [container]}}}
        api.patch_namespaced_custom_object(GROUP, VERSION, ns, PLURAL, name, patch)
        mode = "canary"
    print(f"[deploy] {mode}: {ns}/{name} ← {image}", flush=True)
    time.sleep(10)
    _wait_ready(api, ns, name, timeout, latest_percent=100 if mode == "initial" else int(canary_percent))
    return mode


# ---------------------------------------------------------------------------
# 4) 스모크 테스트 (Istio 게이트웨이 경유, ServiceAccount 토큰 사용)
# ---------------------------------------------------------------------------
def _sample_image_b64(seed: int) -> tuple[str, str]:
    import random

    from faker import Faker

    from .datagen import generate_sample

    s = generate_sample(random.Random(seed), Faker("ko_KR"))
    buf = io.BytesIO()
    s.image.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode(), s.card_type


def smoke_test(name: str, gateway_url: str, requests_count: int = 6, min_card_accuracy: float = 0.5,
               namespace: str | None = None, token: str | None = None) -> dict:
    import requests

    from urllib.parse import urlparse

    ns = namespace or current_namespace()
    token = token or (SA_DIR / "token").read_text().strip()
    url = f"{gateway_url.rstrip('/')}/serving/{ns}/{name}/recognize_b64"
    # KServe 경로 라우팅(/serving/<ns>/<name>)은 Host가 ingressDomain(기본 example.com)일 때만 맞는다.
    # 다른 Host로 보내면 대시보드 catch-all(/)로 빠져 413/404가 난다. InferenceService URL에서 Host를 읽는다.
    isvc_host = urlparse((_get_isvc(_k8s(), ns, name) or {}).get("status", {}).get("url", "")).hostname
    if not isvc_host:
        raise RuntimeError(f"InferenceService {ns}/{name}의 status.url이 없습니다")
    headers = {"Authorization": f"Bearer {token}", "Host": isvc_host}
    ok = correct = 0
    latencies = []
    for i in range(requests_count):
        img_b64, card_type = _sample_image_b64(1000 + i)
        r = None
        for attempt in range(10):  # AuthorizationPolicy/라우팅 전파 대기
            r = requests.post(url, json={"image_b64": img_b64, "score_threshold": 0.5},
                              headers=headers, timeout=120)
            if r.status_code not in (403, 404, 503):
                break
            time.sleep(6)
        if r.status_code != 200:
            raise RuntimeError(f"추론 실패 HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        ok += 1
        correct += int(body.get("card_type") == card_type)
        latencies.append(body.get("latency_ms", 0))
        idn = body.get("fields", {}).get("id_number", {}).get("text", "")
        if card_type != "card_passport" and idn and "******" not in idn:
            raise RuntimeError(f"주민번호 마스킹 실패: {idn}")
        print(f"[smoke] #{i} expected={card_type} got={body.get('card_type')} "
              f"fields={list(body.get('fields', {}))} latency={body.get('latency_ms')}ms", flush=True)

    acc = correct / max(ok, 1)
    summary = {"requests": requests_count, "success": ok, "card_type_accuracy": acc,
               "avg_latency_ms": sum(latencies) / max(len(latencies), 1)}
    print(json.dumps(summary, indent=2), flush=True)
    if acc < min_card_accuracy:
        raise RuntimeError(f"스모크 테스트 정확도 {acc:.2f} < {min_card_accuracy}")
    return summary


# ---------------------------------------------------------------------------
# 5) 승격: 카나리 → 100%, MLflow alias 'production'
# ---------------------------------------------------------------------------
def promote(name: str, mlflow_uri: str, model_name: str, model_version: str, namespace: str | None = None) -> None:
    import mlflow
    from mlflow import MlflowClient

    api = _k8s()
    ns = namespace or current_namespace()
    isvc = _get_isvc(api, ns, name)
    if isvc and isvc["spec"]["predictor"].get("canaryTrafficPercent") is not None:
        api.patch_namespaced_custom_object(GROUP, VERSION, ns, PLURAL, name,
                                           {"spec": {"predictor": {"canaryTrafficPercent": 100}}})
        print(f"[promote] {ns}/{name}: canary → 100%", flush=True)
        _wait_ready(api, ns, name, 600, latest_percent=100)

    mlflow.set_tracking_uri(mlflow_uri)
    MlflowClient().set_registered_model_alias(model_name, "production", model_version)
    print(f"[promote] MLflow {model_name} v{model_version} → alias 'production'", flush=True)
