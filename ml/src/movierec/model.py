"""BPR 행렬 분해 모델과 순위 지표."""

from __future__ import annotations

import torch
from torch import nn


class BPRMF(nn.Module):
    """사용자·영화 임베딩의 내적 + 영화 편향으로 선호 점수를 낸다."""

    def __init__(self, n_users: int, n_items: int, emb_dim: int = 32):
        super().__init__()
        self.user_emb = nn.Embedding(n_users, emb_dim)
        self.item_emb = nn.Embedding(n_items, emb_dim)
        self.item_bias = nn.Embedding(n_items, 1)
        nn.init.normal_(self.user_emb.weight, std=0.05)
        nn.init.normal_(self.item_emb.weight, std=0.05)
        nn.init.zeros_(self.item_bias.weight)

    def score(self, users: torch.Tensor, items: torch.Tensor) -> torch.Tensor:
        """users (B,), items (B,) 또는 (B, K) → 점수 (B,) 또는 (B, K)."""
        u = self.user_emb(users)
        i = self.item_emb(items)
        if items.dim() == 2:
            u = u.unsqueeze(1)
        return (u * i).sum(-1) + self.item_bias(items).squeeze(-1)

    def bpr_loss(self, users: torch.Tensor, pos: torch.Tensor, neg: torch.Tensor) -> torch.Tensor:
        """본 영화(pos)의 점수가 안 본 영화(neg)보다 높아지도록 하는 손실."""
        return -torch.nn.functional.logsigmoid(self.score(users, pos) - self.score(users, neg)).mean()


def rank_metrics(scores: torch.Tensor, k: int = 10) -> dict[str, float]:
    """scores (U, 1+N): 0번 열이 정답 후보. HR@k와 NDCG@k를 계산한다."""
    # 정답보다 점수가 높은 후보 수 = 정답의 등수(0부터)
    rank = (scores[:, 1:] > scores[:, :1]).sum(dim=1)
    hit = rank < k
    ndcg = torch.where(hit, 1.0 / torch.log2(rank.float() + 2.0), torch.zeros_like(rank, dtype=torch.float))
    return {f"hr@{k}": hit.float().mean().item(), f"ndcg@{k}": ndcg.mean().item()}
