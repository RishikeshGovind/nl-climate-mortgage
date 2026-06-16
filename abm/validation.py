"""
CLIMADAPT-NL — Validation & global sensitivity analysis (Phase 4 / §8)
======================================================================

Two things any ABM paper is expected to show (ABM_DESIGN §8):

  1. Calibration / pattern checks — the t=0 anchors and the qualitative patterns
     the model must reproduce (done live in run.py; re-exposed here as functions).
  2. Global sensitivity analysis — a variance-based Sobol decomposition of an
     output of interest onto the uncertain behavioural parameters, reporting
     first-order (S1: a parameter's own effect) and total-order (ST: its effect
     including interactions) indices. This is the standard robustness evidence.

Pure-stdlib Sobol via the Saltelli/Jansen estimators (Saltelli et al. 2010). No
SALib/numpy dependency. A common random seed is fixed across all model evaluations
so the variance attributed to each parameter is *parametric*, not Monte-Carlo noise
— i.e. we hold the stochastic stream constant and vary only the inputs.

Usage:
    python3 abm/validation.py            # Sobol SA, N=48 base samples (~60 s)
    python3 abm/validation.py --n 96     # tighter estimates (slower)
    python3 abm/validation.py --quick    # N=16 screening
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics

from model import ClimadaptModel, Params

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "outputs")

# Uncertain behavioural parameters and plausible ranges (§8.4 SA targets).
SA_PARAMS = {
    "default_base":        (0.04, 0.12),
    "default_slope":       (0.15, 0.45),
    "damage_fraction":     (0.10, 0.26),
    "adapt_effectiveness": (0.30, 0.70),
    "contagion_strength":  (0.40, 1.20),
    "rp_event_learn":      (0.30, 0.70),
}
SA_SEED = 42  # fixed stochastic stream so SA isolates parametric variance


def _evaluate(point, names, agents):
    """Run one model with the SA parameters set to `point`; return final-year
    cumulative default rate (the headline portfolio-risk output)."""
    overrides = {n: v for n, v in zip(names, point)}
    params = Params(n_agents=agents, adaptation_enabled=True, **overrides)
    rows = ClimadaptModel(params, seed=SA_SEED).run()
    return rows[-1]["cumulative_default_rate"]


def sobol(n, agents):
    names = list(SA_PARAMS)
    k = len(names)
    rng = random.Random(7)

    def sample():
        return [[lo + rng.random() * (hi - lo) for lo, hi in
                 (SA_PARAMS[nm] for nm in names)] for _ in range(n)]

    A, B = sample(), sample()
    fA = [_evaluate(row, names, agents) for row in A]
    fB = [_evaluate(row, names, agents) for row in B]

    # Variance of the output over the base samples.
    allf = fA + fB
    var = statistics.pvariance(allf)
    if var <= 0:
        raise SystemExit("Output variance is ~0; widen ranges or increase N.")

    S1, ST = {}, {}
    for i, nm in enumerate(names):
        # AB_i = A with column i replaced by B's column i.
        fAB = []
        for j in range(n):
            row = list(A[j])
            row[i] = B[j][i]
            fAB.append(_evaluate(row, names, agents))
        # Jansen (2010) estimators.
        s1 = sum(fB[j] * (fAB[j] - fA[j]) for j in range(n)) / n / var
        st = sum((fA[j] - fAB[j]) ** 2 for j in range(n)) / (2 * n) / var
        S1[nm] = s1
        ST[nm] = st
    return S1, ST, var, len(names)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=48, help="base samples (model runs = N*(k+2))")
    ap.add_argument("--agents", type=int, default=3_000)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    if args.quick:
        args.n, args.agents = 16, 2_000

    k = len(SA_PARAMS)
    print("=" * 64)
    print(f"SOBOL GLOBAL SENSITIVITY ANALYSIS  (output: 2080 cumulative default rate)")
    print(f"  N={args.n} base samples, k={k} params -> {args.n * (k + 2)} model runs, "
          f"{args.agents} agents each")
    print("=" * 64)
    S1, ST, var, _ = sobol(args.n, args.agents)

    print(f"  output std across parameter space: {var ** 0.5:.2f} pp\n")
    print(f"  {'parameter':<22}{'S1 (first-order)':>18}{'ST (total)':>14}")
    print("  " + "-" * 52)
    for nm in sorted(ST, key=ST.get, reverse=True):
        print(f"  {nm:<22}{S1[nm]:>18.3f}{ST[nm]:>14.3f}")
    print("  " + "-" * 52)
    print(f"  sum S1 = {sum(S1.values()):.2f} (≈1 => near-additive; <1 => interactions)")
    print(f"  most influential: {max(ST, key=ST.get)}")

    os.makedirs(OUT, exist_ok=True)
    out_path = os.path.join(OUT, "abm_sobol.json")
    with open(out_path, "w") as f:
        json.dump({
            "output": "cumulative_default_rate_2080",
            "n_base": args.n, "n_agents": args.agents,
            "ranges": SA_PARAMS,
            "S1": S1, "ST": ST,
            "output_std": var ** 0.5,
        }, f, indent=1)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
