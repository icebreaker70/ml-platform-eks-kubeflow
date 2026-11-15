"""MovieLens(ml-latest-small) 내려받기 · 읽기 · 학습/검증 분리.

표준 라이브러리 csv + numpy만 쓴다 (pandas 불필요).
데이터셋은 GroupLens 라이선스상 재배포할 수 없으므로 저장소에 넣지 않고 실행 시 내려받는다.
"""

from __future__ import annotations

import csv
import io
import os
import ssl
import urllib.request
import zipfile
from dataclasses import dataclass

import numpy as np

MOVIELENS_URL = "https://files.grouplens.org/datasets/movielens/ml-latest-small.zip"


@dataclass
class MovieLens:
    """아이디를 0부터 시작하는 연속 번호로 바꾼 상호작용 데이터."""

    users: np.ndarray        # (N,) 사용자 번호
    items: np.ndarray        # (N,) 영화 번호
    ratings: np.ndarray      # (N,) 평점 0.5~5.0
    timestamps: np.ndarray   # (N,) 유닉스 시각
    titles: list[str]        # 영화 번호 → 제목
    genres: list[str]        # 영화 번호 → 장르("Action|Comedy")

    @property
    def n_users(self) -> int:
        return int(self.users.max()) + 1

    @property
    def n_items(self) -> int:
        return len(self.titles)

    def stats(self) -> dict:
        n = len(self.users)
        return {
            "interactions": n,
            "users": self.n_users,
            "items": self.n_items,
            "density": round(n / (self.n_users * self.n_items), 5),
            "rating_mean": round(float(self.ratings.mean()), 3),
        }


def download(dest: str, url: str = MOVIELENS_URL) -> str:
    """zip을 dest 경로에 내려받는다 (이미 있으면 건너뜀). 반환: zip 경로."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    tmp = dest + ".part"
    # uv·pyenv로 설치한 파이썬은 시스템 인증서를 못 찾는 경우가 있어, certifi가 있으면 그 묶음을 쓴다.
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    with urllib.request.urlopen(url, timeout=60, context=ctx) as r, open(tmp, "wb") as f:
        f.write(r.read())
    os.replace(tmp, dest)
    return dest


def _read_csv(zf: zipfile.ZipFile, suffix: str) -> list[dict]:
    name = next(n for n in zf.namelist() if n.endswith(suffix))
    with zf.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")))


def load(zip_path: str) -> MovieLens:
    """ratings.csv / movies.csv를 읽는다. 평점이 하나도 없는 영화는 뺀다."""
    with zipfile.ZipFile(zip_path) as zf:
        ratings = _read_csv(zf, "ratings.csv")
        movies = _read_csv(zf, "movies.csv")
    return build(ratings, movies)


def build(ratings: list[dict], movies: list[dict]) -> MovieLens:
    user_ids = sorted({int(r["userId"]) for r in ratings})
    item_ids = sorted({int(r["movieId"]) for r in ratings})
    u_index = {u: i for i, u in enumerate(user_ids)}
    i_index = {m: i for i, m in enumerate(item_ids)}
    meta = {int(m["movieId"]): (m["title"], m["genres"]) for m in movies}
    return MovieLens(
        users=np.array([u_index[int(r["userId"])] for r in ratings], dtype=np.int64),
        items=np.array([i_index[int(r["movieId"])] for r in ratings], dtype=np.int64),
        ratings=np.array([float(r["rating"]) for r in ratings], dtype=np.float32),
        timestamps=np.array([int(r["timestamp"]) for r in ratings], dtype=np.int64),
        titles=[meta.get(m, (f"movie-{m}", ""))[0] for m in item_ids],
        genres=[meta.get(m, ("", "(no genres listed)"))[1] for m in item_ids],
    )


def leave_last_out(ds: MovieLens) -> tuple[np.ndarray, np.ndarray]:
    """사용자마다 가장 마지막에 본 영화 1편을 검증용으로 뗀다.

    반환: (train_idx, val_idx) — ds 배열에 대한 인덱스. 상호작용이 1건뿐인 사용자는 학습에만 쓴다.
    """
    order = np.lexsort((ds.timestamps, ds.users))      # 사용자별, 시간순
    users_sorted = ds.users[order]
    is_last = np.ones(len(order), dtype=bool)
    is_last[:-1] = users_sorted[1:] != users_sorted[:-1]
    counts = np.bincount(ds.users, minlength=ds.n_users)
    is_val = is_last & (counts[users_sorted] > 1)
    return order[~is_val], order[is_val]


def eval_candidates(ds: MovieLens, val_idx: np.ndarray, n_neg: int = 99, seed: int = 0) -> np.ndarray:
    """검증용 후보 목록 (사용자 수, 1 + n_neg). 0번 열이 정답, 나머지는 그 사용자가 보지 않은 영화.

    NCF 논문의 평가 방식: 정답 1편을 무작위 99편 사이에서 몇 등으로 올리는지 본다.
    """
    rng = np.random.default_rng(seed)
    seen: dict[int, set] = {}
    for u, i in zip(ds.users.tolist(), ds.items.tolist()):
        seen.setdefault(u, set()).add(i)
    out = np.empty((len(val_idx), 1 + n_neg), dtype=np.int64)
    for row, idx in enumerate(val_idx):
        u = int(ds.users[idx])
        out[row, 0] = ds.items[idx]
        negs: list[int] = []
        while len(negs) < n_neg:
            cand = rng.integers(0, ds.n_items, size=n_neg * 2)
            negs.extend(int(c) for c in cand if c not in seen[u])
        out[row, 1:] = negs[:n_neg]
    return out
