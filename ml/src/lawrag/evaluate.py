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


# ── 생성 평가 ─────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    """핵심 표현 비교용: 공백 제거, 가운뎃점 통일(법령 'ㆍ' ↔ 모델 '·')."""
    import re
    return re.sub(r"\s+", "", s).replace("·", "ㆍ").replace("・", "ㆍ")


def points_coverage(qa: QA, answer: str) -> float:
    if not qa.points:
        return 1.0
    a = _norm(answer)
    return sum(_norm(p) in a for p in qa.points) / len(qa.points)


def gen_metrics(pairs: list[tuple[QA, "object"]]) -> dict:
    """pairs: (QA, answer.Answer). 답변 텍스트는 사람이 리포트로 함께 본다 — 숫자는 경향을 보는 용도."""
    ans = [(q, a) for q, a in pairs if q.answerable]
    unans = [(q, a) for q, a in pairs if not q.answerable]
    answered = [(q, a) for q, a in ans if not a.refused]
    cites = [c for _, a in answered for c in a.citations]
    fut = [(q, a) for q, a in ans if q.expect_status == "시행예정"]
    gen = [a.timings.get("generate_s", 0) for _, a in pairs if not a.skipped_llm]
    return {
        "refusal_accuracy": round(sum(a.refused for _, a in unans) / len(unans), 4) if unans else None,
        "false_refusal_rate": round(sum(a.refused for _, a in ans) / len(ans), 4),
        "citation_accuracy": round(sum(c.status != "unsupported" for c in cites) / len(cites), 4) if cites else None,
        "expected_cited_rate": round(sum(bool(set(q.expected) & {c.key for c in a.citations})
                                         for q, a in answered) / len(answered), 4) if answered else None,
        "no_citation_rate": round(sum(not a.citations for _, a in answered) / len(answered), 4) if answered else None,
        "points_coverage": round(sum(points_coverage(q, a.answer) for q, a in answered) / len(answered), 4)
        if answered else None,
        "future_date_rate": round(sum("2027" in a.answer for _, a in fut) / len(fut), 4) if fut else None,
        "future_notice_added": sum(a.future_notice_added for _, a in pairs),
        "llm_skipped": sum(a.skipped_llm for _, a in pairs),
        "generate_s_avg": round(sum(gen) / len(gen), 1) if gen else None,
    }


def gen_report_markdown(index_version: str, model: str, pairs: list, m: dict) -> str:
    lines = [f"# 생성 평가 — {index_version} · {model}", "", "| 지표 | 값 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in m.items()]
    lines.append("")
    for q, a in pairs:
        cites = ", ".join(f"{c.key}{'' if c.status == 'retrieved' else '(' + c.status + ')'}" for c in a.citations)
        flag = "거절" if a.refused else f"인용 [{cites or '없음'}]"
        lines += [f"## {q.id} ({q.type}) — 정답 {', '.join(q.expected) or '없음'} · {flag} · "
                  f"핵심 표현 {points_coverage(q, a.answer):.0%} · {a.timings.get('generate_s', 0)}초",
                  "", f"**Q.** {q.question}", "", a.answer, ""]
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
    p.add_argument("--generate", action="store_true", help="LLM 답변까지 생성해 생성 지표를 잰다 (LLM_BASE_URL)")
    p.add_argument("--gen-out", default="data/law/eval_generation.md")
    p.add_argument("--only", default=None, help="쉼표로 구분한 문항 id만 (예: q01,u04)")
    args = p.parse_args(argv)

    conn = store.connect(args.dsn)
    version = args.version or store.get_active(conn)
    if not version:
        raise SystemExit("서비스 중인 색인이 없습니다")
    emb = Embedder(args.model)
    qas = load_qa(args.qa)
    if args.only:
        qas = [q for q in qas if q.id in set(args.only.split(","))]
    backend = PgBackend(conn)
    results = run(qas, emb.encode_query, backend, version, args.k)
    m = metrics(results, args.k)
    report = report_markdown(version, results, m, args.k)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    saved = {"retrieval": m}

    if args.generate:
        from .answer import LLM, ask

        llm = LLM()
        pairs = []
        for q in qas:
            a = ask(q.question, emb.encode_query, backend, version, llm, k=args.k)
            pairs.append((q, a))
            print(f"{q.id}: {'거절' if a.refused else [c.key for c in a.citations]} ({a.timings.get('generate_s')}초)",
                  flush=True)
        gm = gen_metrics(pairs)
        with open(args.gen_out, "w", encoding="utf-8") as f:
            f.write(gen_report_markdown(version, llm.model, pairs, gm))
        print("\n".join(f"{k}: {v}" for k, v in gm.items()))
        print(f"→ {args.gen_out}")
        saved["generation"] = {**gm, "model": llm.model}

    if args.save:
        store.set_metrics(conn, version, saved)
        print(f"→ index_versions.metrics 기록: {version}")


if __name__ == "__main__":
    main()
