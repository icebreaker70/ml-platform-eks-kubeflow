"""검색 평가 — 평가 세트(eval/qa.jsonl)로 색인 버전의 Recall@k · MRR@k를 잰다.

평가 세트 항목
- expected      : 정답 조 키 ("24의2", 부칙은 "부칙1"). 비어 있으면 답이 없는 질문
- expect_status : "시행예정"이면 그 조의 시행 예정 청크가 검색되어야 한다
- points        : 답변에 들어가야 할 핵심 표현 (생성 평가용, 정답 조 본문에 실제로 있어야 함 — 테스트로 확인)

지표 (답이 있는 질문 기준)
- recall@k : 질문별 (찾은 정답 조 수 / 정답 조 수)의 평균
- hit@k    : 정답 조를 하나라도 찾은 비율
- mrr@k    : 첫 정답 조 순위의 역수 평균
- status@k : expect_status가 있는 질문에서 그 상태의 청크를 찾은 비율
- 답 없음 기준값: 벡터 검색 최고 유사도로 답 있음/없음을 가를 때 정확도가 가장 높은 값 (생성 단계 "찾지 못함" 판단용)

    python -m lawrag.evaluate               # 서비스 중인 색인
    python -m lawrag.evaluate --save        # 결과를 index_versions.metrics에 기록
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field

from .store import Hit

QA_PATH = os.path.join(os.path.dirname(__file__), "eval", "qa.jsonl")


@dataclass
class QA:
    id: str
    type: str
    question: str
    expected: list[str]
    points: list[str] = field(default_factory=list)
    expect_status: str | None = None

    @property
    def answerable(self) -> bool:
        return bool(self.expected)


def load_qa(path: str = QA_PATH) -> list[QA]:
    with open(path, encoding="utf-8") as f:
        return [QA(**json.loads(line)) for line in f if line.strip()]


def ref_key(part: str, article_key: str) -> str:
    """청크의 (part, article_key) → 평가 세트 표기 ("24의2", "부칙1")."""
    return f"부칙{article_key}" if part == "부칙" else article_key


@dataclass
class Result:
    qa: QA
    hits: list[Hit]

    @property
    def keys(self) -> list[str]:
        return [ref_key(h.chunk.part, h.chunk.article_key) for h in self.hits]

    @property
    def recall(self) -> float:
        return len(set(self.qa.expected) & set(self.keys)) / len(self.qa.expected)

    @property
    def rank(self) -> int | None:
        return next((i for i, k in enumerate(self.keys, 1) if k in self.qa.expected), None)

    @property
    def status_ok(self) -> bool | None:
        if not self.qa.expect_status:
            return None
        return any(ref_key(h.chunk.part, h.chunk.article_key) in self.qa.expected
                   and h.chunk.status == self.qa.expect_status for h in self.hits)

    @property
    def top_vector_score(self) -> float:
        """벡터 검색 최고 유사도. 조문 번호를 직접 짚은 질문은 답이 있다고 보고 1.0."""
        if any(h.source == "article" for h in self.hits):
            return 1.0
        return max((h.score for h in self.hits), default=0.0)


def best_threshold(results: list[Result]) -> tuple[float, float]:
    """top_vector_score ≥ t 이면 '답 있음'으로 볼 때 정확도가 가장 높은 t (동률이면 가운데 값)."""
    scores = sorted({round(r.top_vector_score, 3) for r in results})
    cands = [(a + b) / 2 for a, b in zip(scores, scores[1:])] or scores
    best: list[tuple[float, float]] = []
    for t in cands:
        acc = sum((r.top_vector_score >= t) == r.qa.answerable for r in results) / len(results)
        if not best or acc > best[0][1] + 1e-9:
            best = [(t, acc)]
        elif abs(acc - best[0][1]) < 1e-9:
            best.append((t, acc))
    t = best[len(best) // 2][0]
    return round(t, 3), best[0][1]


def metrics(results: list[Result], k: int) -> dict:
    ans = [r for r in results if r.qa.answerable]
    st = [r.status_ok for r in ans if r.status_ok is not None]
    unans = [r for r in results if not r.qa.answerable]
    t, acc = best_threshold(results)
    return {
        f"recall_at_{k}": round(sum(r.recall for r in ans) / len(ans), 4),
        f"hit_at_{k}": round(sum(r.rank is not None for r in ans) / len(ans), 4),
        f"mrr_at_{k}": round(sum(1 / r.rank for r in ans if r.rank) / len(ans), 4),
        f"status_at_{k}": round(sum(st) / len(st), 4) if st else None,
        "questions": len(results),
        "answerable": len(ans),
        "no_answer_threshold": t,
        "no_answer_threshold_accuracy": round(acc, 4),
        "answerable_min_score": round(min(r.top_vector_score for r in ans), 3),
        "unanswerable_max_score": round(max((r.top_vector_score for r in unans), default=0.0), 3),
    }


def report_markdown(index_version: str, results: list[Result], m: dict, k: int) -> str:
    lines = [f"# 검색 평가 — {index_version}", "", "| 지표 | 값 |", "|---|---|"]
    lines += [f"| {key} | {val} |" for key, val in m.items()]
    lines += ["", "| id | 유형 | 정답 | 순위 | recall | 최고 유사도 | 검색된 조 (top-k) |", "|---|---|---|---|---|---|---|"]
    for r in results:
        rank = r.rank or ("-" if r.qa.answerable else "")
        rec = f"{r.recall:.2f}" if r.qa.answerable else ""
        status = "" if r.status_ok is None else (" ✓예정" if r.status_ok else " ✗예정")
        got = ", ".join(f"{key}{'(예정)' if h.chunk.status == '시행예정' else ''}" for key, h in zip(r.keys, r.hits))
        lines.append(f"| {r.qa.id} | {r.qa.type} | {', '.join(r.qa.expected) or '없음'} | {rank}{status} | {rec} "
                     f"| {r.top_vector_score:.3f} | {got} |")
    misses = [r for r in results if r.qa.answerable and r.recall < 1]
    lines += ["", f"놓친 질문 {len(misses)}개 (k={k}): " + (", ".join(r.qa.id for r in misses) or "없음")]
    return "\n".join(lines) + "\n"


def run(qas: list[QA], embed_query, backend, index_version: str, k: int = 5) -> list[Result]:
    from .retrieve import retrieve
    return [Result(q, retrieve(q.question, embed_query, backend, index_version, k=k)) for q in qas]


def main(argv: list[str] | None = None) -> None:
    from . import store
    from .embed import Embedder
    from .retrieve import PgBackend

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--qa", default=QA_PATH)
    p.add_argument("--version", default=None, help="기본: 서비스 중인 색인")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--dsn", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--out", default="data/law/eval_report.md")
    p.add_argument("--save", action="store_true", help="index_versions.metrics에 기록")
    args = p.parse_args(argv)

    conn = store.connect(args.dsn)
    version = args.version or store.get_active(conn)
    if not version:
        raise SystemExit("서비스 중인 색인이 없습니다")
    emb = Embedder(args.model)
    results = run(load_qa(args.qa), emb.encode_query, PgBackend(conn), version, args.k)
    m = metrics(results, args.k)
    report = report_markdown(version, results, m, args.k)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    if args.save:
        store.set_metrics(conn, version, {"retrieval": m})
        print(f"→ index_versions.metrics 기록: {version}")


if __name__ == "__main__":
    main()
