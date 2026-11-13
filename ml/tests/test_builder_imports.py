"""builder 이미지 단계(register, bento-build, image-build, deploy, smoke-test, promote)가
builder 이미지에 없는 패키지(torchmetrics, pycocotools)를 끌어오지 않는지 검사한다.

단계 함수는 import를 함수 안에서 늦게 하므로, 각 함수 본문의 import 문을 직접 실행해 본다.
"""
from __future__ import annotations

import ast
import importlib
import importlib.abc
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "idcard"
ML_ONLY = {"torchmetrics", "pycocotools"}          # requirements-ml.txt 에만 있는 패키지
BUILDER_FUNCS = {"ops": None, "steps": {"register"}}  # None = 모듈의 모든 함수


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in ML_ONLY:
            raise ModuleNotFoundError(f"No module named '{name}' (builder 이미지에 없음)")
        return None


def _cases():
    for mod, only in BUILDER_FUNCS.items():
        tree = ast.parse((SRC / f"{mod}.py").read_text(encoding="utf-8"))
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef) and (only is None or fn.name in only):
                stmts = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, (ast.Import, ast.ImportFrom))]
                if stmts:
                    yield pytest.param(mod, stmts, id=f"{mod}.{fn.name}")


@pytest.fixture
def builder_env(monkeypatch):
    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    # 이미 로드된 idcard 모듈(train/evaluate 등)이 캐시로 통과되지 않도록 비운다.
    for name in [m for m in sys.modules if m == "idcard" or m.startswith("idcard.") or m.split(".")[0] in ML_ONLY]:
        monkeypatch.delitem(sys.modules, name)
    yield
    sys.meta_path.remove(blocker)


@pytest.mark.parametrize("mod,stmts", list(_cases()))
def test_builder_step_imports(builder_env, mod, stmts):
    importlib.import_module(f"idcard.{mod}")
    for stmt in stmts:
        exec(stmt, {"__name__": f"idcard.{mod}", "__package__": "idcard"})
