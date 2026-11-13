"""OCR 결과 후처리: 숫자 필드 보정, 주민등록번호 뒷자리 마스킹."""

from __future__ import annotations

import re

# 숫자 필드에서 흔한 OCR 혼동 문자 보정
_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "|": "1",
                            "i": "1", "Z": "2", "z": "2", "S": "5", "s": "5", "B": "8", "g": "9",
                            "—": "-", "–": "-", "_": "-", "~": "-"})

RRN_RE = re.compile(r"(\d{6})\s*-?\s*(\d)(\d{6})")
PASSPORT_RE = re.compile(r"([A-Z])\s*(\d{8})")


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_id_number(text: str, card_type: str | None) -> str:
    text = normalize_space(text).replace(" ", "")
    if card_type == "card_passport":
        head, tail = text[:1].upper(), text[1:].translate(_DIGIT_FIX)
        m = PASSPORT_RE.search(head + tail)
        return m.group(1) + m.group(2) if m else head + re.sub(r"[^0-9]", "", tail)
    fixed = re.sub(r"[^0-9-]", "", text.translate(_DIGIT_FIX))
    m = RRN_RE.search(fixed)
    return f"{m.group(1)}-{m.group(2)}{m.group(3)}" if m else fixed


def mask_rrn(text: str) -> str:
    """주민등록번호 형태(YYMMDD-GXXXXXX)이면 뒤 6자리를 '*'로 가린다."""
    return RRN_RE.sub(lambda m: f"{m.group(1)}-{m.group(2)}******", text)


def is_valid_rrn_format(text: str) -> bool:
    return bool(re.fullmatch(r"\d{6}-\d(\d{6}|\*{6})", text))


def postprocess_field(field: str, text: str, card_type: str | None) -> dict:
    """필드별 후처리. 검출 박스가 부정확해 다른 필드에 주민번호가 섞여 읽힐 수 있으므로
    id_number뿐 아니라 모든 필드 텍스트에 마스킹을 적용한다."""
    text = normalize_space(text)
    if field == "id_number":
        cleaned = clean_id_number(text, card_type)
        if card_type == "card_passport":
            return {"text": mask_rrn(cleaned), "valid_format": bool(re.fullmatch(r"[A-Z]\d{8}", cleaned))}
        return {"text": mask_rrn(cleaned), "valid_format": is_valid_rrn_format(cleaned)}
    if field == "issue_date":
        return {"text": mask_rrn(text), "digits": re.sub(r"[^0-9]", "", text.translate(_DIGIT_FIX))[:8]}
    return {"text": mask_rrn(text)}
