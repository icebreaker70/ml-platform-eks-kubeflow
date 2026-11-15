"""movierec: 데이터 분리 · 순위 지표 · TensorBoard 로그 생성 (네트워크 없이 작은 가짜 데이터로)."""

import glob
import os

import numpy as np
import torch

from movierec import data as D
from movierec.model import BPRMF, rank_metrics
from movierec.train import train_one


def _tiny(n_users=12, n_items=40, per_user=8, seed=0) -> D.MovieLens:
    rng = np.random.default_rng(seed)
    ratings = []
    for u in range(1, n_users + 1):
        for t, m in enumerate(rng.choice(np.arange(100, 100 + n_items), size=per_user, replace=False)):
            ratings.append({"userId": str(u), "movieId": str(m), "rating": "4.0", "timestamp": str(1000 * u + t)})
    movies = [{"movieId": str(m), "title": f"Movie {m} (2000)", "genres": "Comedy|Drama"}
              for m in range(100, 100 + n_items)]
    return D.build(ratings, movies)


def test_ids_are_contiguous_and_metadata_matches():
    ds = _tiny()
    assert ds.n_users == 12
    assert ds.items.max() == ds.n_items - 1
    assert len(ds.titles) == len(ds.genres) == ds.n_items
    assert all(t.startswith("Movie ") for t in ds.titles)


def test_leave_last_out_takes_latest_interaction_per_user():
    ds = _tiny()
    train_idx, val_idx = D.leave_last_out(ds)
    assert len(val_idx) == ds.n_users
    assert len(train_idx) + len(val_idx) == len(ds.users)
    assert not set(train_idx.tolist()) & set(val_idx.tolist())
    for idx in val_idx:
        u = ds.users[idx]
        assert ds.timestamps[idx] == ds.timestamps[ds.users == u].max()


def test_single_interaction_user_stays_in_train():
    ds = D.build(
        [{"userId": "1", "movieId": "10", "rating": "5", "timestamp": "1"},
         {"userId": "2", "movieId": "10", "rating": "5", "timestamp": "1"},
         {"userId": "2", "movieId": "11", "rating": "5", "timestamp": "2"}],
        [{"movieId": "10", "title": "A", "genres": "Drama"}, {"movieId": "11", "title": "B", "genres": "Drama"}],
    )
    train_idx, val_idx = D.leave_last_out(ds)
    assert len(val_idx) == 1 and ds.users[val_idx[0]] == 1   # 2번 사용자(번호 1)만 검증에 들어간다
    assert len(train_idx) == 2


def test_eval_candidates_exclude_seen_items():
    ds = _tiny()
    _, val_idx = D.leave_last_out(ds)
    cands = D.eval_candidates(ds, val_idx, n_neg=20, seed=1)
    assert cands.shape == (len(val_idx), 21)
    for row, idx in enumerate(val_idx):
        seen = set(ds.items[ds.users == ds.users[idx]].tolist())
        assert cands[row, 0] == ds.items[idx]
        assert not seen & set(cands[row, 1:].tolist())


def test_rank_metrics_known_values():
    # 1행: 정답이 1등 → hit, ndcg 1.0 / 2행: 정답이 3등 → hit, ndcg 1/log2(4)=0.5 / 3행: 꼴찌 → miss
    scores = torch.tensor([[9.0, 1, 2, 3], [5.0, 6, 7, 1], [0.0, 1, 2, 3]])
    m = rank_metrics(scores, k=3)
    assert abs(m["hr@3"] - 2 / 3) < 1e-6
    assert abs(m["ndcg@3"] - (1.0 + 0.5 + 0.0) / 3) < 1e-6


def test_score_shapes():
    model = BPRMF(5, 7, emb_dim=4)
    users = torch.tensor([0, 1, 2])
    assert model.score(users, torch.tensor([1, 2, 3])).shape == (3,)
    assert model.score(users, torch.tensor([[1, 2], [3, 4], [5, 6]])).shape == (3, 2)


def test_train_one_writes_tensorboard_logs_and_learns(tmp_path):
    ds = _tiny(n_users=30, n_items=50, per_user=10)
    train_idx, val_idx = D.leave_last_out(ds)
    cands = D.eval_candidates(ds, val_idx, n_neg=20, seed=0)
    logdir = str(tmp_path / "dim8")
    m = train_one(ds, train_idx, val_idx, cands, emb_dim=8, epochs=3, lr=0.05, weight_decay=0.0,
                  batch_size=64, logdir=logdir)
    assert 0.0 <= m["hr@10"] <= 1.0 and 0.0 <= m["ndcg@10"] <= 1.0
    assert glob.glob(os.path.join(logdir, "events.out.tfevents.*"))
    # Projector 메타데이터: 머리글 1줄 + 영화 수(상위 N개 제한보다 적으면 전체)
    meta = glob.glob(os.path.join(logdir, "*", "movies", "metadata.tsv"))
    assert meta
    lines = open(meta[0], encoding="utf-8").read().splitlines()
    assert lines[0].split("\t") == ["title", "genre", "ratings"]
    assert len(lines) == 1 + ds.n_items
