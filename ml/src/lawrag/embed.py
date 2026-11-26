"""bge-m3 dense 임베딩 (CPU).

- 문서 · 질의 모두 접두어(instruction) 없이 인코딩한다 (bge-m3 dense 권장 방식).
- 코사인 유사도를 쓰므로 L2 정규화한 벡터를 저장한다.
- 모델 위치: EMBED_MODEL 환경 변수 (Hugging Face 이름 또는 S3 캐시에서 받은 로컬 디렉터리).
"""

from __future__ import annotations

import os

import numpy as np

DEFAULT_MODEL = "BAAI/bge-m3"
DIM = 1024
# 청크 본문 최대 1,500자 + 머리말 ≈ 900 토큰. 8192까지 받지만 길게 둘 이유가 없어 속도를 위해 줄인다.
MAX_SEQ_LENGTH = 1024


class Embedder:
    def __init__(self, model: str | None = None, device: str = "cpu", batch_size: int = 16):
        from sentence_transformers import SentenceTransformer  # 무거운 의존성은 필요할 때만

        self.name = model or os.environ.get("EMBED_MODEL", DEFAULT_MODEL)
        self.model = SentenceTransformer(self.name, device=device)
        self.model.max_seq_length = MAX_SEQ_LENGTH
        self.batch_size = batch_size
        # sentence-transformers 6.x에서 get_sentence_embedding_dimension → get_embedding_dimension으로 이름이 바뀜
        get_dim = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        dim = get_dim()
        if dim != DIM:
            raise ValueError(f"{self.name}의 차원 {dim} ≠ {DIM} (테이블 스키마와 다름)")

    def encode(self, texts: list[str], show_progress: bool = False) -> np.ndarray:
        vecs = self.model.encode(
            texts, batch_size=self.batch_size, normalize_embeddings=True,
            convert_to_numpy=True, show_progress_bar=show_progress,
        )
        return vecs.astype(np.float32)

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode([text])[0]
