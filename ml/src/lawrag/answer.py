"""질문 → 답변 (검색 → 프롬프트 → LLM → 인용 검증).

LLM은 OpenAI 호환 API로 부른다 — EKS에서는 KServe의 vLLM, 로컬에서는 mlx-lm 서버(make llm-local).
    LLM_BASE_URL  기본 http://127.0.0.1:18080/v1
    LLM_MODEL     기본 mlx-community/Qwen3-8B-4bit (EKS: Qwen/Qwen3-8B-FP8)
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field

from idcard.postprocess import mask_rrn

from .evaluate import ref_key
from .prompt import DISCLAIMER, NOT_FOUND, build_messages
from .retrieve import retrieve
from .store import Hit

DEFAULT_BASE_URL = "http://127.0.0.1:18080/v1"
DEFAULT_MODEL = "mlx-community/Qwen3-8B-4bit"
# 벡터 유사도가 이보다 낮으면 LLM을 부르지 않고 바로 "찾지 못함" (PLAN.md 10.6 — 검색 평가에서 0.498)
DEFAULT_MIN_SCORE = 0.48

THINK = re.compile(r"<think>.*?</think>\s*", re.S)
# 「개인정보 보호법」 제24조의2제1항, 부칙 제1조, 같은 법 제15조 … — 조 단위로 뽑는다
CITE = re.compile(r"(부칙\s*)?제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")
# 다른 법령 인용(「행정기본법」 제20조, 「정부조직법」 제2조 등)은 검증 대상에서 뺀다
OTHER_LAW = re.compile(r"「(?!개인정보\s*보호법)[^」]+」\s*$")


@dataclass
class Citation:
    key: str                  # "24의2", "부칙1"
    status: str               # retrieved(검색된 조) | referenced(검색된 조 본문이 언급) | unsupported


@dataclass
class Answer:
    question: str             # 마스킹된 질문
    answer: str
    refused: bool
    citations: list[Citation]
    citations_verified: bool  # unsupported 인용이 없으면 true
    retrieved: list[dict]
    top_score: float
    skipped_llm: bool
    index_version: str
    model: str
    timings: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    future_notice_added: bool = False   # 시행 예정 안내를 코드가 덧붙였는지
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict:
        return asdict(self)


class LLM:
    def __init__(self, base_url: str | None = None, model: str | None = None, timeout: float | None = None):
        from openai import OpenAI

        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        # 첫 요청은 서버가 모델을 불러오느라 느릴 수 있다. 재시도는 하지 않는다(같은 생성을 두 번 돌리지 않도록).
        self.client = OpenAI(base_url=base_url or os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
                             api_key=os.environ.get("LLM_API_KEY", "none"), max_retries=0,
                             timeout=timeout or float(os.environ.get("LLM_TIMEOUT", "300")))

    def chat(self, messages: list[dict], max_tokens: int = 768, temperature: float = 0.2) -> tuple[str, dict]:
        r = self.client.chat.completions.create(
            model=self.model, messages=messages, max_tokens=max_tokens, temperature=temperature,
            # Qwen3 thinking 끄기 (vLLM · mlx-lm 모두 chat_template_kwargs를 chat template에 넘긴다)
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        usage = r.usage.model_dump() if r.usage else {}
        return clean(r.choices[0].message.content or ""), usage


def clean(text: str) -> str:
    """thinking 출력이 섞여 나오면 지운다 (닫히지 않은 <think>는 그 뒤를 모두 버림)."""
    text = THINK.sub("", text)
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


def extract_citations(answer: str) -> list[str]:
    keys: list[str] = []
    for m in CITE.finditer(answer):
        if OTHER_LAW.search(answer[max(0, m.start() - 40):m.start()]):
            continue
        key = ref_key("부칙" if m.group(1) else "본문", m.group(2) + (f"의{m.group(3)}" if m.group(3) else ""))
        if key not in keys:
            keys.append(key)
    return keys


def verify_citations(answer: str, hits: list[Hit]) -> list[Citation]:
    retrieved = {ref_key(h.chunk.part, h.chunk.article_key) for h in hits}
    mentioned = {k for h in hits for k in extract_citations(h.chunk.body)}
    out = []
    for k in extract_citations(answer):
        status = "retrieved" if k in retrieved else "referenced" if k in mentioned else "unsupported"
        out.append(Citation(k, status))
    return out


def is_refusal(answer: str) -> bool:
    return NOT_FOUND.rstrip(".") in answer


def future_notices(answer: str, citations: list[Citation], hits: list[Hit]) -> list[str]:
    """인용한 조의 시행 예정 청크가 검색됐는데 답변에 시행 연도가 없으면 안내 문장을 만든다.

    프롬프트 규칙 3(시행 예정이면 시행일 명시)을 모델이 지키지 않는 경우가 있어 코드로 보장한다.
    로컬 Qwen3-8B-4bit에서 확인한 두 경우:
    - 제28조의12(시행 예정본만 있음) 답변에 시행일이 빠짐 → "시행될 예정인 조문"
    - 제2조(현행 · 예정 함께 검색) 답변이 예정본에만 있는 제9호(인공지능기술)를 쓰면서 시행일이 빠짐
      → 어느 쪽을 썼는지 코드로 가릴 수 없으므로 "개정 내용이 시행 예정"이라고 알린다
    """
    notes = []
    for c in citations:
        chunks = [h.chunk for h in hits if ref_key(h.chunk.part, h.chunk.article_key) == c.key]
        future = [ch for ch in chunks if ch.status == "시행예정"]
        if not future:
            continue
        y, m, d = (int(x) for x in min(ch.effective_from for ch in future).split("-"))
        if str(y) in answer:
            continue
        label = future[0].article_label
        if len(future) == len(chunks):
            notes.append(f"※ 「개인정보 보호법」 {label}는 {y}년 {m}월 {d}일부터 시행될 예정인 조문입니다.")
        else:
            notes.append(f"※ 「개인정보 보호법」 {label}에는 {y}년 {m}월 {d}일부터 시행될 개정 내용이 있습니다. "
                         "답변이 개정 내용을 포함하는지 아래 근거 조문의 '시행 예정' 표시를 확인하세요.")
    return notes


def ask(question: str, embed_query, backend, index_version: str, llm: LLM | None,
        k: int = 5, min_score: float = DEFAULT_MIN_SCORE) -> Answer:
    q = mask_rrn(question)
    t0 = time.time()
    hits = retrieve(q, embed_query, backend, index_version, k=k)
    t1 = time.time()
    direct = any(h.source == "article" for h in hits)
    top = 1.0 if direct else max((h.score for h in hits), default=0.0)
    retrieved = [{"key": ref_key(h.chunk.part, h.chunk.article_key), "label": h.chunk.article_label,
                  "title": h.chunk.title, "status": h.chunk.status, "effective_from": h.chunk.effective_from,
                  "paragraphs": h.chunk.paragraphs, "items": h.chunk.items, "page": h.chunk.page,
                  "score": round(h.score, 4), "source": h.source, "header": h.chunk.header, "body": h.chunk.body}
                 for h in hits]
    model = llm.model if llm else ""
    if top < min_score or llm is None:
        return Answer(q, NOT_FOUND, True, [], True, retrieved, round(top, 4), True, index_version, model,
                      {"retrieve_s": round(t1 - t0, 3), "generate_s": 0.0})
    text, usage = llm.chat(build_messages(q, hits))
    t2 = time.time()
    text = mask_rrn(text)
    cites = verify_citations(text, hits)
    notes = [] if is_refusal(text) else future_notices(text, cites, hits)
    if notes:
        text = text + "\n\n" + "\n".join(notes)
    return Answer(
        question=q, answer=text, refused=is_refusal(text), citations=cites,
        citations_verified=all(c.status != "unsupported" for c in cites), retrieved=retrieved,
        top_score=round(top, 4), skipped_llm=False, index_version=index_version, model=model,
        timings={"retrieve_s": round(t1 - t0, 3), "generate_s": round(t2 - t1, 3)}, usage=usage,
        future_notice_added=bool(notes),
    )
