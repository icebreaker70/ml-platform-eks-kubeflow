"""프롬프트 조립 — 시스템 규칙 + 검색된 조문 + 질문."""

from __future__ import annotations

from .store import Hit

# 모델이 근거를 찾지 못했을 때 그대로 쓰게 하는 문장. 거절 판단(평가 · 화면 표시)에 이 문장을 쓴다.
NOT_FOUND = "제공된 조문에서 이 질문에 답할 근거를 찾지 못했습니다."
DISCLAIMER = "이 답변은 개인정보 보호법 조문을 바탕으로 한 참고 정보이며 법률 자문이 아닙니다."

SYSTEM = f"""당신은 대한민국 「개인정보 보호법」 조문을 근거로 질문에 답하는 도우미입니다.

규칙
1. 아래 [근거]로 주어진 조문에 있는 내용만 사용합니다. 조문에 없는 내용은 추측하거나 지어내지 않습니다.
2. 답의 근거가 된 조문을 「개인정보 보호법」 제N조 또는 제N조제M항 형식으로 문장 안에 밝힙니다. [근거]에 없는 조문 번호는 인용하지 않습니다.
3. [근거]에 '시행 예정'으로 표시된 조문을 쓰면, 그 조문이 언제부터 시행되는지 함께 밝힙니다.
4. 조문이 세부 사항을 "대통령령으로 정한다"고만 하면, 세부 내용은 시행령에 있다고 말하고 법률에 있는 범위까지만 답합니다.
5. [근거]가 질문과 관련이 없거나 답할 수 없으면 다른 말 없이 다음 문장만 씁니다: {NOT_FOUND}
6. 한국어로, 핵심부터 간결하게 답합니다(5문장 이내, 필요하면 목록). 법률 자문을 한다고 말하지 않습니다."""


def format_context(hits: list[Hit]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        blocks.append(f"[근거 {i}] {h.chunk.header}\n{h.chunk.body}")
    return "\n\n".join(blocks)


def build_messages(question: str, hits: list[Hit]) -> list[dict]:
    user = f"[근거]\n{format_context(hits)}\n\n[질문]\n{question}"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
