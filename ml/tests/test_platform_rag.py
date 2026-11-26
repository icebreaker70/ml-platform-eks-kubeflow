"""platform/rag 템플릿 검사 (클러스터 없이).

install.sh의 render()는 넘겨준 변수만 envsubst로 치환한다. 템플릿이 쓰는 변수를 넘기지 않으면
`${VAR}`가 그대로 클러스터에 들어가므로, 템플릿 변수 ⊆ install.sh가 넘기는 변수인지 확인한다.
"""

import os
import re

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
RAG = os.path.join(ROOT, "platform", "rag")
VAR = re.compile(r"\$\{(\w+)\}")


def _install_sh() -> str:
    with open(os.path.join(RAG, "install.sh"), encoding="utf-8") as f:
        return f.read()


def _passed_vars() -> dict[str, set[str]]:
    sh = _install_sh()
    common = set(re.search(r"COMMON=\(([^)]*)\)", sh).group(1).split())
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"render (\S+\.yaml\.tmpl)((?:[ \t]+[^|\n]+)?)", sh):
        names = set()
        for tok in m.group(2).split():
            if tok == '"${COMMON[@]}"':
                names |= common
            elif re.fullmatch(r"[A-Z_]+", tok):
                names.add(tok)
        out.setdefault(m.group(1), set()).update(names)
    return out


def _render(name: str) -> list[dict]:
    with open(os.path.join(RAG, name), encoding="utf-8") as f:
        src = f.read()
    fake = {v: '["x"]' if v.endswith("_ARGS") else "x" for v in VAR.findall(src)}
    return [d for d in yaml.safe_load_all(VAR.sub(lambda m: fake[m.group(1)], src)) if d]


TEMPLATES = sorted(f for f in os.listdir(RAG) if f.endswith(".yaml.tmpl"))


def test_every_template_is_rendered_by_install_sh():
    assert set(_passed_vars()) == set(TEMPLATES)


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_vars_are_passed(name):
    with open(os.path.join(RAG, name), encoding="utf-8") as f:
        used = set(VAR.findall(f.read()))
    missing = used - _passed_vars()[name]
    assert not missing, f"{name}: install.sh가 넘기지 않는 변수 {sorted(missing)}"


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_is_valid_yaml(name):
    docs = _render(name)
    assert docs and all("kind" in d and "apiVersion" in d for d in docs)


def test_llm_isvc_essentials():
    isvc = next(d for d in _render("llm-isvc.yaml.tmpl") if d["kind"] == "InferenceService")
    assert isvc["metadata"]["annotations"]["serving.kserve.io/deploymentMode"] == "Standard"
    assert isvc["metadata"]["labels"]["networking.kserve.io/visibility"] == "cluster-local"
    p = isvc["spec"]["predictor"]
    assert p["nodeSelector"] == {"node-role": "gpu-serving"}
    assert p["initContainers"][0]["securityContext"]["runAsUser"] == 1337   # Istio CNI 가로채기 회피
    c = p["containers"][0]
    assert c["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert "--max-model-len=8192" in c["args"]


def test_rag_api_points_to_llm_service():
    dep = next(d for d in _render("rag-api.yaml.tmpl") if d["kind"] == "Deployment")
    env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["LLM_BASE_URL"] == "http://law-llm-predictor.x.svc.cluster.local/v1"  # KServe Standard 모드 Service 이름
    assert env["RAGDB_DSN"].startswith("postgresql://rag:$(RAGDB_PASSWORD)@ragdb.rag.svc")


def test_nvidia_device_plugin_covers_serving_nodes():
    with open(os.path.join(ROOT, "infra", "terraform", "k8s.tf"), encoding="utf-8") as f:
        tf = f.read()
    assert '"gpu", "gpu-serving"' in tf
    with open(os.path.join(ROOT, "platform", "karpenter", "llm-nodepool.yaml.tmpl"), encoding="utf-8") as f:
        assert "node-role: gpu-serving" in f.read()


def test_cache_split_s3_and_local_pdf():
    from lawrag.cache import split_s3
    from lawrag.index import fetch_pdf

    assert split_s3("s3://bucket/models/bge-m3/") == ("bucket", "models/bge-m3")
    with pytest.raises(ValueError):
        split_s3("/local/path")
    assert fetch_pdf("docs/a.pdf") == "docs/a.pdf"


def test_rag_api_creates_schema_on_startup(monkeypatch):
    """새 ragdb(테이블 없음)에서도 healthz가 500이 나지 않도록 rag-api 시작 시 스키마를 만든다."""
    pytest.importorskip("fastapi")
    import sys

    sys.path.insert(0, os.path.join(ROOT, "ml"))
    import rag_api.app as A

    calls = []
    monkeypatch.setattr(A.store, "init_schema", lambda conn: calls.append(conn))
    monkeypatch.setattr(A.State, "conn", lambda self: "fake-conn")
    monkeypatch.setattr(A, "Embedder", lambda: type("E", (), {"name": "fake"})())
    monkeypatch.setattr(A, "LLM", lambda: type("L", (), {"model": "fake"})())
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    A.startup()
    assert calls == ["fake-conn"]


def _qty(v: str) -> float:
    """쿠버네티스 수량 → 숫자 (cpu: 코어, 메모리: 바이트)."""
    v = str(v)
    units = {"m": 1e-3, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
    for u, f in units.items():
        if v.endswith(u):
            return float(v[: -len(u)]) * f
    return float(v)


def test_isvc_containers_set_cpu_and_memory_limits():
    """KServe는 비어 있는 상한을 기본값(cpu 1, 메모리 2Gi)으로 채운다 → 요청보다 작으면 Deployment가 거부된다."""
    isvc = next(d for d in _render("llm-isvc.yaml.tmpl") if d["kind"] == "InferenceService")
    for c in isvc["spec"]["predictor"]["containers"]:
        req, lim = c["resources"]["requests"], c["resources"]["limits"]
        for r in ("cpu", "memory"):
            assert r in lim, f"{c['name']}: limits.{r}가 없으면 KServe 기본값이 들어간다"
            assert _qty(lim[r]) >= _qty(req[r]), f"{c['name']}: limits.{r} < requests.{r}"
