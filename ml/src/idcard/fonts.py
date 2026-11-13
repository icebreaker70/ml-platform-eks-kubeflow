"""한글 폰트 탐색. 컨테이너에서는 fonts-noto-cjk, macOS에서는 Apple SD Gothic Neo를 사용한다."""

from __future__ import annotations

import os
from functools import lru_cache

from PIL import ImageFont

CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/AppleGothic.ttf",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
]


@lru_cache(maxsize=1)
def font_path() -> str:
    env = os.environ.get("IDCARD_FONT_PATH")
    if env:
        if not os.path.exists(env):
            raise FileNotFoundError(f"IDCARD_FONT_PATH={env} 파일이 없습니다")
        return env
    for path in CANDIDATES:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "한글 폰트를 찾지 못했습니다. IDCARD_FONT_PATH 환경 변수로 .ttf/.ttc 경로를 지정하세요."
    )


@lru_cache(maxsize=64)
def load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(), size=size)
