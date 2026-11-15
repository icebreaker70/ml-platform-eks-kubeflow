"""BPR 행렬 분해 학습 + TensorBoard 로깅.

로컬 실행:
  PYTHONPATH=ml/src python -m movierec.train --zip data/movielens/ml-latest-small.zip \
      --logdir runs/movierec --emb-dims 16,32,64 --epochs 20
  tensorboard --logdir runs/movierec

TensorBoard에 남기는 것
  Scalars     train/bpr_loss(스텝), train/epoch_loss, val/hr@10, val/ndcg@10
  Histograms  사용자·영화 임베딩, 영화 편향 (epoch마다)
  Projector   인기 영화 임베딩 + 제목·장르 메타데이터
  Text        예시 사용자 1명의 최근 시청 목록과 추천 결과
  HParams     임베딩 차원·학습률 등과 최종 지표
"""

from __future__ import annotations

import argparse
import os
import time

import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter

from . import data as D
from .model import BPRMF, rank_metrics

PROJECTOR_TOP_N = 1500   # Projector에 올릴 인기 영화 수 (전체를 올리면 브라우저가 느려진다)


def _recommend_markdown(model: BPRMF, ds: D.MovieLens, train_idx: np.ndarray, user: int, k: int = 10) -> str:
    """사용자 1명의 최근 시청 5편과 추천 k편을 Markdown으로 만든다 (TensorBoard Text 탭용)."""
    mine = train_idx[ds.users[train_idx] == user]
    mine = mine[np.argsort(ds.timestamps[mine])]
    seen = set(ds.items[mine].tolist())
    with torch.no_grad():
        dev = model.item_emb.weight.device
        scores = model.score(torch.full((ds.n_items,), user, device=dev), torch.arange(ds.n_items, device=dev))
    scores[list(seen)] = float("-inf")
    top = torch.topk(scores, k).indices.tolist()
    lines = [f"**사용자 #{user}** — 학습 데이터 {len(mine)}편", "", "최근 본 영화 | 장르", "---|---"]
    lines += [f"{ds.titles[i]} | {ds.genres[i]}" for i in ds.items[mine[-5:]].tolist()]
    lines += ["", "추천 순위 | 영화 | 장르", "---|---|---"]
    lines += [f"{n} | {ds.titles[i]} | {ds.genres[i]}" for n, i in enumerate(top, 1)]
    return "\n".join(lines)


def train_one(ds: D.MovieLens, train_idx: np.ndarray, val_idx: np.ndarray, cands: np.ndarray, *,
              emb_dim: int, epochs: int, lr: float, weight_decay: float, batch_size: int,
              logdir: str, seed: int = 42, mlflow_uri: str = "", run_name: str = "") -> dict:
    """모델 하나를 학습하고 logdir에 TensorBoard 이벤트를 남긴다. 반환: 최종 지표."""
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = BPRMF(ds.n_users, ds.n_items, emb_dim).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)

    users = torch.as_tensor(ds.users[train_idx], device=device)
    items = torch.as_tensor(ds.items[train_idx], device=device)
    val_users = torch.as_tensor(ds.users[val_idx], device=device)
    val_cands = torch.as_tensor(cands, device=device)
    hparams = {"emb_dim": emb_dim, "lr": lr, "weight_decay": weight_decay, "batch_size": batch_size, "epochs": epochs}

    ml_run = None
    if mlflow_uri:
        import mlflow

        mlflow.set_tracking_uri(mlflow_uri)
        mlflow.set_experiment("movierec")
        ml_run = mlflow.start_run(run_name=run_name or os.path.basename(logdir))
        mlflow.log_params(hparams)
        mlflow.set_tag("tensorboard_logdir", logdir)

    writer = SummaryWriter(logdir)
    step, metrics, t0 = 0, {}, time.time()
    try:
        for epoch in range(1, epochs + 1):
            model.train()
            perm = torch.randperm(len(users), device=device)
            total = 0.0
            for start in range(0, len(perm), batch_size):
                b = perm[start:start + batch_size]
                # 음성 샘플은 전체 영화에서 무작위로 뽑는다. 이미 본 영화가 드물게 섞이지만 데이터가 희소해 영향이 작다.
                neg = torch.as_tensor(rng.integers(0, ds.n_items, size=len(b)), device=device)
                loss = model.bpr_loss(users[b], items[b], neg)
                opt.zero_grad()
                loss.backward()
                opt.step()
                total += loss.item() * len(b)
                if step % 10 == 0:
                    writer.add_scalar("train/bpr_loss", loss.item(), step)
                step += 1

            model.eval()
            with torch.no_grad():
                metrics = rank_metrics(model.score(val_users, val_cands), k=10)
            epoch_loss = total / len(users)
            writer.add_scalar("train/epoch_loss", epoch_loss, epoch)
            for k, v in metrics.items():
                writer.add_scalar(f"val/{k}", v, epoch)
            writer.add_histogram("embeddings/user", model.user_emb.weight, epoch)
            writer.add_histogram("embeddings/item", model.item_emb.weight, epoch)
            writer.add_histogram("embeddings/item_bias", model.item_bias.weight, epoch)
            writer.flush()   # 학습 중에도 TensorBoard에서 바로 보이도록
            if ml_run:
                import mlflow

                mlflow.log_metrics({"epoch_loss": epoch_loss, "hr_at_10": metrics["hr@10"],
                                    "ndcg_at_10": metrics["ndcg@10"]}, step=epoch)
            print(f"[dim={emb_dim} epoch {epoch}/{epochs}] loss={epoch_loss:.4f} "
                  f"hr@10={metrics['hr@10']:.4f} ndcg@10={metrics['ndcg@10']:.4f}", flush=True)

        # Projector: 평점 수가 많은 영화만 올린다
        popular = np.argsort(-np.bincount(ds.items, minlength=ds.n_items))[:PROJECTOR_TOP_N]
        counts = np.bincount(ds.items, minlength=ds.n_items)
        writer.add_embedding(
            model.item_emb.weight.detach().cpu()[popular],
            metadata=[[ds.titles[i], ds.genres[i].split("|")[0], str(int(counts[i]))] for i in popular],
            metadata_header=["title", "genre", "ratings"],
            tag="movies", global_step=epochs,
        )
        sample_user = int(np.argsort(np.bincount(ds.users[train_idx]))[ds.n_users // 2])   # 시청 수가 중간인 사용자
        writer.add_text("recommendations", _recommend_markdown(model, ds, train_idx, sample_user), epochs)
        writer.add_hparams(hparams, {"hparam/hr@10": metrics["hr@10"], "hparam/ndcg@10": metrics["ndcg@10"]},
                           run_name=".")
    finally:
        writer.close()
        if ml_run:
            import mlflow

            mlflow.end_run()
    metrics["seconds"] = round(time.time() - t0, 1)
    return metrics


def run(zip_path: str, logdir: str, emb_dims: list[int], epochs: int = 20, lr: float = 0.005,
        weight_decay: float = 1e-5, batch_size: int = 1024, seed: int = 42, mlflow_uri: str = "",
        run_prefix: str = "") -> dict[str, dict]:
    """임베딩 차원별로 하나씩 학습한다. 실행마다 logdir/<run 이름>/ 아래에 기록한다."""
    ds = D.load(zip_path)
    train_idx, val_idx = D.leave_last_out(ds)
    cands = D.eval_candidates(ds, val_idx, n_neg=99, seed=seed)
    print(f"[data] {ds.stats()} train={len(train_idx)} val={len(val_idx)}", flush=True)
    out = {}
    for dim in emb_dims:
        name = f"{run_prefix}dim{dim}"
        out[name] = train_one(ds, train_idx, val_idx, cands, emb_dim=dim, epochs=epochs, lr=lr,
                              weight_decay=weight_decay, batch_size=batch_size,
                              logdir=os.path.join(logdir, name), seed=seed, mlflow_uri=mlflow_uri, run_name=name)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default="data/movielens/ml-latest-small.zip", help="없으면 내려받는다")
    ap.add_argument("--logdir", default="runs/movierec")
    ap.add_argument("--emb-dims", default="16,32,64")
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--lr", type=float, default=0.005)
    ap.add_argument("--batch-size", type=int, default=1024)
    ap.add_argument("--mlflow-uri", default=os.environ.get("MLFLOW_TRACKING_URI", ""))
    args = ap.parse_args()
    D.download(args.zip)
    result = run(args.zip, args.logdir, [int(x) for x in args.emb_dims.split(",")], epochs=args.epochs,
                 lr=args.lr, batch_size=args.batch_size, mlflow_uri=args.mlflow_uri)
    for name, m in result.items():
        print(f"{name}: {m}")


if __name__ == "__main__":
    main()
