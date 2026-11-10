"""SERP L2 model: DCNv2 (cross network in parallel with an MLP) trained with a listwise softmax loss per search.

It scores each candidate on its own (no cross-candidate attention), so serving scores rows and training sees whole
searches only through the loss. Input standardization is part of the module, so the logged model takes the raw
`rts_ml.serp_l2.model_matrix`.
"""

from __future__ import annotations

import copy
from typing import Callable, Iterable

import numpy as np
import pandas as pd
import torch
from torch import nn

from rts_ml.serp_l2 import model_matrix, ndcg_per_group

Batch = tuple[torch.Tensor, torch.Tensor, torch.Tensor]  # x [B, L, F], label [B, L], mask [B, L]


class DCNv2(nn.Module):
    def __init__(self, n_features: int, mean, std, n_cross: int = 3, deep: tuple[int, ...] = (128, 64),
                 dropout: float = 0.1):
        super().__init__()
        self.register_buffer("mean", torch.as_tensor(np.asarray(mean), dtype=torch.float32))
        # Constant columns (std 0) pass through centred instead of dividing by zero.
        std = torch.as_tensor(np.asarray(std), dtype=torch.float32)
        self.register_buffer("std", torch.where(std > 1e-6, std, torch.ones_like(std)))
        self.cross = nn.ModuleList(nn.Linear(n_features, n_features) for _ in range(n_cross))
        layers, width = [], n_features
        for size in deep:
            layers += [nn.Linear(width, size), nn.ReLU(), nn.Dropout(dropout)]
            width = size
        self.deep = nn.Sequential(*layers)
        self.head = nn.Linear(n_features + width, 1)

    def standardize(self, x: torch.Tensor) -> torch.Tensor:
        return (x - self.mean) / self.std

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x [..., F] -> one score per candidate [...]."""
        x0 = self.standardize(x)
        xl = x0
        for layer in self.cross:
            xl = x0 * layer(xl) + xl  # DCNv2 cross: x_{l+1} = x_0 * (W x_l + b) + x_l
        return self.head(torch.cat([xl, self.deep(x0)], dim=-1)).squeeze(-1)


def listwise_loss(logits: torch.Tensor, label: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """Softmax cross-entropy per search against label / sum(label); padding is excluded, searches with no
    engagement are skipped."""
    logp = torch.log_softmax(logits.masked_fill(mask == 0, float("-inf")), dim=-1).masked_fill(mask == 0, 0.0)
    target = label * mask
    total = target.sum(-1)
    keep = total > 0
    return -((target[keep] / total[keep, None]) * logp[keep]).sum(-1).mean()


def _scores(model: nn.Module, x: torch.Tensor, device: str, chunk: int = 512) -> torch.Tensor:
    model.eval()
    with torch.no_grad():
        return torch.cat([model(x[i:i + chunk].to(device)).cpu() for i in range(0, len(x), chunk)])


def packed_ndcg(scores: torch.Tensor, label: torch.Tensor, mask: torch.Tensor, k: int = 10) -> float:
    """Mean NDCG@k over packed searches, through the same `ndcg_per_group` as the evaluation report."""
    g, pos = torch.nonzero(mask > 0, as_tuple=True)
    df = pd.DataFrame({"group_id": g.numpy(), "label": label[g, pos].numpy(), "s": scores[g, pos].numpy()})
    return float(ndcg_per_group(df, "s", k=k)["ndcg"].mean())


def fit_dcn(train_batches: Callable[[], Iterable[Batch]], valid: Batch, mean, std, max_epochs: int = 20,
            patience: int = 3, lr: float = 1e-3, weight_decay: float = 1e-5, device: str = "cpu",
            log: Callable[[dict], None] | None = None) -> DCNv2:
    """Adam on `train_batches()` (called once per epoch: a DataLoader over UCDataset, or in-memory batches);
    keeps the epoch with the best validation NDCG@10 and stops after `patience` epochs without improvement.
    Returns the best model on the CPU, in eval mode."""
    model = DCNv2(n_features=valid[0].shape[-1], mean=mean, std=std).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
    best, best_state, since_best = -1.0, None, 0
    for epoch in range(1, max_epochs + 1):
        model.train()
        losses = []
        for x, y, mask in train_batches():
            x, y, mask = x.to(device), y.to(device), mask.to(device)
            loss = listwise_loss(model(x), y, mask)
            if torch.isnan(loss):  # a batch with no engaged search
                continue
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            losses.append(loss.item())
        ndcg = packed_ndcg(_scores(model, valid[0], device), valid[1], valid[2])
        if log:
            log({"epoch": epoch, "train_loss": float(np.mean(losses)) if losses else float("nan"), "valid_ndcg10": ndcg})
        if ndcg > best:
            best, best_state, since_best = ndcg, copy.deepcopy(model.state_dict()), 0
        else:
            since_best += 1
            if since_best >= patience:
                break
    model.load_state_dict(best_state)
    model.best_epoch, model.best_valid_ndcg10 = epoch - since_best, best
    return model.cpu().eval()


def score(model: DCNv2, df: pd.DataFrame) -> np.ndarray:
    """L2 scores for raw candidate rows, in row order; higher ranks first. Runs on the model's device."""
    device = next(model.parameters()).device
    x = torch.as_tensor(model_matrix(df).to_numpy(), device=device)
    with torch.no_grad():
        return model.eval()(x).cpu().numpy().astype(np.float64)
