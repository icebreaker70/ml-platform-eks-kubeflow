"""개인정보 보호법 Q&A RAG API (6주차).

    POST /rag/ask      {"question": "...", "index_version": (선택), "k": (선택)}
    GET  /rag/healthz  준비 상태 + 서비스 중인 색인 버전
    GET  /rag/         질의 화면

환경 변수: RAGDB_DSN, EMBED_MODEL, LLM_BASE_URL, LLM_MODEL, RAG_MIN_SCORE,
          MLFLOW_TRACKING_URI(있으면 질의마다 트레이스 기록, 실험 MLFLOW_EXPERIMENT_NAME 기본 law-rag)

로컬: make rag-api-local  (http://127.0.0.1:18000/rag/)
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from lawrag import store
from lawrag.answer import DEFAULT_MIN_SCORE, LLM, ask
from lawrag.embed import Embedder
from lawrag.retrieve import PgBackend

log = logging.getLogger("rag-api")
STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_QUESTION_CHARS = 500


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=MAX_QUESTION_CHARS)
    index_version: str | None = None   # 평가 파이프라인이 승격 전 색인을 시험할 때
    k: int = Field(default=5, ge=1, le=10)


class State:
    def __init__(self):
        self.embedder: Embedder | None = None
        self.llm: LLM | None = None
        self.min_score = float(os.environ.get("RAG_MIN_SCORE", DEFAULT_MIN_SCORE))
        self._local = threading.local()      # psycopg 연결은 스레드마다 따로
        self._embed_lock = threading.Lock()  # SentenceTransformer.encode는 스레드 안전을 보장하지 않음
        self.tracing = False

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None or c.closed:
            c = self._local.conn = store.connect()
        return c

    def embed_query(self, text: str):
        with self._embed_lock:
            return self.embedder.encode_query(text)


state = State()
app = FastAPI(title="law-rag", docs_url="/rag/docs", openapi_url="/rag/openapi.json")


@app.on_event("startup")
def startup() -> None:
    state.embedder = Embedder()
    state.llm = LLM()
    if os.environ.get("MLFLOW_TRACKING_URI"):
        import mlflow

        mlflow.set_experiment(os.environ.get("MLFLOW_EXPERIMENT_NAME", "law-rag"))
        state.tracing = True
    log.info("준비됨: embed=%s llm=%s tracing=%s", state.embedder.name, state.llm.model, state.tracing)


def _span(name: str):
    if not state.tracing:
        return contextlib.nullcontext(None)
    import mlflow
    return mlflow.start_span(name=name)


@app.get("/rag/healthz")
def healthz() -> dict:
    return {"ok": state.embedder is not None, "index_version": store.get_active(state.conn()),
            "llm_model": state.llm.model if state.llm else None}


@app.post("/rag/ask")
def ask_endpoint(req: AskRequest) -> dict:
    conn = state.conn()
    version = req.index_version or store.get_active(conn)
    if not version:
        raise HTTPException(503, "서비스 중인 색인이 없습니다")
    with _span("law-rag.ask") as span:
        a = ask(req.question, state.embed_query, PgBackend(conn), version, state.llm, k=req.k,
                min_score=state.min_score)
        out = a.to_dict()
        if span is not None:
            # 입력은 ask()가 주민번호를 가린 질문만 기록한다 (원문은 남기지 않음)
            span.set_inputs({"question": a.question, "index_version": version, "k": req.k})
            span.set_attributes({"top_score": a.top_score, "refused": a.refused, "skipped_llm": a.skipped_llm,
                                 "citations_verified": a.citations_verified, "model": a.model,
                                 **{f"timing.{k}": v for k, v in a.timings.items()}})
            span.set_outputs({"answer": a.answer, "citations": [c["key"] for c in out["citations"]],
                              "retrieved": [r["key"] for r in out["retrieved"]]})
    return out


@app.get("/")
def root():
    return RedirectResponse("/rag/")


@app.get("/rag/")
def page():
    return FileResponse(os.path.join(STATIC, "index.html"))
