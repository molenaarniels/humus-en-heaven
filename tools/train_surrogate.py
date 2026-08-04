#!/usr/bin/env python3
"""Train the airflow surrogate that replaces the Newton pressure solve in the browser.

Consumes `airflow_distill.py`'s dataset: (open fractions, wind, T_out, zone temps,
cp_shelter) → (per-zone fresh outdoor air m³/s, per-doorway mixing flow m³/s).

Two things make this different from the usual regression job:

**Random splits are correct here.** Everywhere else in this project a random split
would leak future into past (15-min rows are strongly autocorrelated). This
dataset is i.i.d. synthetic samples from a deterministic function, so a random
split is exactly right — there is no time axis to leak across.

**The target is heavy-tailed and non-negative.** Flows sit at ~0 for most of the
space and reach ~3.7 m³/s wide open, so plain MSE would spend all its capacity on
the tail and be useless at the low-ACH end where comfort decisions are actually
made. We fit in `asinh(y/s)` space (smooth through zero, log-like in the tail) and
report errors back in physical units — m³/s and, more usefully, ACH.

No monotonicity constraint: per-zone flow is genuinely non-monotone in opening
area because opening one window can short-circuit another (PLAN.md §2.2). The
invariants worth enforcing hold end-to-end, not here.

Usage:
    python tools/train_surrogate.py --data data/surrogate/airflow.npz
    python tools/train_surrogate.py --epochs 60 --hidden 384 --export docs/surrogate.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import torch
import torch.nn as nn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import vent_io as vio   # noqa: E402


class MLP(nn.Module):
    def __init__(self, d_in, d_out, hidden=256, depth=3):
        super().__init__()
        layers, d = [], d_in
        for _ in range(depth):
            layers += [nn.Linear(d, hidden), nn.SiLU()]
            d = hidden
        layers += [nn.Linear(d, d_out)]
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data/surrogate/airflow.npz")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--hidden", type=int, default=256)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--batch", type=int, default=4096)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--scale-q", type=float, default=20.0,
                    help="percentile of non-zero targets used as the asinh scale; LOW keeps "
                         "relative accuracy at small flows (see comment in the code)")
    ap.add_argument("--fp16", action="store_true",
                    help="pack weights as base64 float16 — ~6x smaller JSON, which matters "
                         "because this file ships to the browser on a static page")
    ap.add_argument("--export", default="data/surrogate/surrogate.json")
    args = ap.parse_args()

    spec_path = os.path.splitext(args.data)[0] + "_spec.json"
    spec = json.load(open(spec_path))
    d = np.load(args.data)
    X, Y = d["X"].astype(np.float32), d["Y"].astype(np.float32)
    print(f"data {X.shape} -> {Y.shape}")

    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    torch.manual_seed(0)

    # asinh scale per output column. This choice is load-bearing: asinh(y/s) is
    # ~linear for y << s and ~log for y >> s, so `s` sets where resolution lives.
    #
    # A first version used p90 of the non-zero flows (e.g. 0.64 m³/s for living).
    # That is far above the closed-house band (~0.02 m³/s), so the transform was
    # linear across the entire low end and the fit had no incentive to resolve it —
    # relative error there came out at 130-415 %. Since a shut house sits at ~0.3 ACH
    # and that is precisely what "will it stay cool overnight?" depends on, the low
    # band matters more than the tail.
    #
    # Using a LOW quantile instead makes the transform log-like over almost the whole
    # range, which equalises *relative* error across magnitudes.
    scale = np.ones(Y.shape[1], dtype=np.float32)
    for j in range(Y.shape[1]):
        nz = Y[:, j][Y[:, j] > 1e-9]
        scale[j] = max(float(np.percentile(nz, args.scale_q)) if nz.size else 1.0, 1e-4)

    def fwd(y):
        return np.arcsinh(y / scale)

    def inv(t):
        return np.sinh(t) * scale

    Yt = fwd(Y)
    x_mu, x_sd = X.mean(0), X.std(0) + 1e-6
    Xn = (X - x_mu) / x_sd

    n = len(X)
    rng = np.random.default_rng(0)
    perm = rng.permutation(n)          # i.i.d. synthetic data — random split is correct
    cut = int(n * 0.9)
    tr, va = perm[:cut], perm[cut:]

    Xtr = torch.tensor(Xn[tr], device=dev)
    Ytr = torch.tensor(Yt[tr], device=dev)
    Xva = torch.tensor(Xn[va], device=dev)

    model = MLP(X.shape[1], Y.shape[1], args.hidden, args.depth).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-5)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=args.lr, total_steps=args.epochs * max(1, len(tr) // args.batch))
    lossf = nn.SmoothL1Loss(beta=0.1)

    for ep in range(args.epochs):
        model.train()
        idx = torch.randperm(len(tr), device=dev)
        tot = nb = 0.0
        for i in range(0, len(tr) - args.batch + 1, args.batch):
            b = idx[i:i + args.batch]
            opt.zero_grad()
            loss = lossf(model(Xtr[b]), Ytr[b])
            loss.backward()
            opt.step()
            sched.step()
            tot += loss.item()
            nb += 1
        if (ep + 1) % 5 == 0 or ep == 0:
            model.eval()
            with torch.no_grad():
                pv = inv(model(Xva).cpu().numpy())
            yv = Y[va]
            mae = np.abs(pv - yv).mean()
            print(f"  epoch {ep + 1:3d}  train {tot / max(nb, 1):.5f}   "
                  f"valid MAE {mae:.5f} m³/s", flush=True)

    # ---- report in units a human can judge: ACH per zone -----------------------
    model.eval()
    with torch.no_grad():
        pv = inv(model(Xva).cpu().numpy())
    yv = Y[va]
    house = vio.load_house()
    vols = {**{r: c.get("volume_m3", 1.0) for r, c in house.get("rooms", {}).items()},
            **{j: c.get("volume_m3", 1.0) for j, c in house.get("junctions", {}).items()}}

    print(f"\n{'output':26}{'MAE m³/s':>11}{'p95 |err|':>11}{'MAE ACH':>10}{'p95 ACH':>10}")
    for j, c in enumerate(spec["output_cols"]):
        e = np.abs(pv[:, j] - yv[:, j])
        row = f"{c:26}{e.mean():11.5f}{np.percentile(e, 95):11.5f}"
        if c.startswith("fresh_"):
            k = 3600.0 / vols.get(c[len("fresh_"):], 1.0)
            row += f"{e.mean() * k:10.4f}{np.percentile(e, 95) * k:10.4f}"
        print(row)

    # Acceptance test, stratified by true ACH — and expressed as ventilation HEAT FLOW,
    # not as relative ACH error.
    #
    # An earlier version reported relative ACH error per band and flagged 130-400 % in
    # the 0-0.5 band. That metric is an artefact: it divides by a band mean that tends
    # to zero, so it explodes no matter how small the absolute error is. What actually
    # propagates into room temperature is Q = ACH * V * rho_cp * dT, so that is what to
    # judge. For reference, internal gains are ~150-300 W per room, which is the scale
    # an error has to reach before it matters.
    print("\nACCEPTANCE: ventilation heat-flow error at dT=5 K (internal gains ~150-300 W)")
    print(f"{'zone':10}{'band ACH':>10}{'n':>8}{'MAE W':>9}{'p95 W':>9}{'true mean W':>13}")
    RHO_CP, DT = 1.2 * 1005.0, 5.0
    worst = 0.0
    for j, c in enumerate(spec["output_cols"]):
        if not c.startswith("fresh_"):
            continue
        z = c[len("fresh_"):]
        k = 3600.0 / vols.get(z, 1.0)
        ta = yv[:, j] * k
        for lo, hi in [(0, 0.5), (0.5, 1), (1, 3), (3, 10)]:
            m = (ta >= lo) & (ta < hi)
            if m.sum() < 50:
                continue
            eW = np.abs(pv[m, j] - yv[m, j]) * RHO_CP * DT
            tW = yv[m, j] * RHO_CP * DT
            worst = max(worst, eW.mean())
            print(f"{z:10}{f'{lo}-{hi}':>10}{m.sum():8}{eW.mean():9.1f}"
                  f"{np.percentile(eW, 95):9.1f}{tW.mean():13.1f}")
    print(f"\nworst mean heat-flow error across zones/bands: {worst:.0f} W")
    if worst > 120:
        print("  !! comparable to a room's internal gains — train longer/bigger first")
    print("  NB: this is a proxy. The real acceptance test is end-to-end — "
          "surrogate-in-the-loop 12h RMSE must reproduce solver-in-the-loop (PLAN.md 2.6).")

    # ---- export weights for the JS runtime -------------------------------------
    w = {"format": "mlp-silu-v1",
         "input_cols": spec["input_cols"], "output_cols": spec["output_cols"],
         "x_mu": x_mu.tolist(), "x_sd": x_sd.tolist(),
         "y_scale": scale.tolist(), "y_transform": "asinh",
         "layers": []}
    import base64

    def pack(a):
        a = np.ascontiguousarray(a)
        if not args.fp16:
            return a.tolist()
        return {"__f16": base64.b64encode(a.astype(np.float16).tobytes()).decode(),
                "shape": list(a.shape)}

    for m in model.net:
        if isinstance(m, nn.Linear):
            w["layers"].append({"W": pack(m.weight.detach().cpu().numpy()),
                                "b": pack(m.bias.detach().cpu().numpy())})
    w["packed"] = "f16-b64" if args.fp16 else "json"

    os.makedirs(os.path.dirname(args.export) or ".", exist_ok=True)
    with open(args.export, "w") as f:
        json.dump(w, f)
    size = os.path.getsize(args.export) / 1e6
    print(f"\nexported {args.export}  ({size:.2f} MB, {len(w['layers'])} layers)")
    if size > 4:
        print("  !! large for a static page — consider --hidden 192 or float16 packing")


if __name__ == "__main__":
    main()
