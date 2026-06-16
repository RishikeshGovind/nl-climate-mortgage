"""
CLIMADAPT-NL — Resilience-lever experiments (Phase 4)
=====================================================

The thesis contribution (ABM_DESIGN §7): run the Monte-Carlo ensemble under a
common climate scenario, once per government intervention, and compare portfolio
outcomes. Each lever is just a `Params` configuration consumed by the agents —
the model machinery is unchanged, so the comparison is apples-to-apples.

All arms run *on top of* autonomous household adaptation (Phase 2), so each number
is the **marginal** resilience gain of the policy beyond what households do on
their own.

Levers (§7):
  - LTV cap at origination        -> lower starting leverage
  - Adaptation subsidy            -> cheaper adaptation -> faster/earlier uptake
  - Foundation-repair program     -> pre-repair high-subsidence homes at t=0
  - Mandatory risk disclosure     -> households perceive risk (and adapt) earlier
  - Climate-risk insurance        -> pools acute damage, dampens default cascades

Usage:
    python3 abm/experiments.py            # 10 reps x 15k agents per arm (~40 s)
    python3 abm/experiments.py --quick    # 4 reps x 6k agents
    python3 abm/experiments.py --reps 20 --agents 25000
"""

from __future__ import annotations

import argparse
import json
import os

from model import Params
from run import run_ensemble

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "outputs")

# Each lever = the Params overrides that switch it on (all share the baseline otherwise).
LEVERS = {
    "baseline":          {},
    "ltv_cap_90":        {"policy_ltv_cap": 90.0},
    "adapt_subsidy_50":  {"policy_adapt_subsidy": 0.5},
    "foundation_repair": {"policy_foundation_repair": True},
    "disclosure":        {"policy_disclosure": True},
    "insurance":         {"policy_insurance": True},
}

REPORT = [
    ("pct_underwater", "underwater %", 1),
    ("cumulative_default_rate", "cum.default %", 2),
    ("lender_cumulative_loss_eur", "lender loss €bn", 0),
    ("lender_capital_ratio", "capital ratio", 2),
    ("adaptation_uptake", "adapt uptake %", 1),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--agents", type=int, default=15_000)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    if args.quick:
        args.reps, args.agents = 4, 6_000

    results = {}
    for name, overrides in LEVERS.items():
        print(f"  running arm: {name} ({args.reps} reps x {args.agents} agents) ...")
        params = Params(n_agents=args.agents, adaptation_enabled=True, **overrides)
        ensemble, gem_out, concentration = run_ensemble(params, args.reps, name, quiet=True)
        last = ensemble[-1]
        results[name] = {
            "year": last["year"],
            "metrics": {k: last[k]["mean"] for k, _, _ in REPORT},
            "hardest_hit": [(g["name"], round(g["default_rate"], 1)) for g in gem_out[:3]],
            "concentration": concentration,
        }

    base = results["baseline"]["metrics"]
    year = results["baseline"]["year"]

    # --- comparison table (vs baseline) ---
    print("=" * 92)
    print(f"RESILIENCE-LEVER COMPARISON at {year} (all on top of autonomous adaptation)")
    print("=" * 92)
    header = f"{'lever':<18}" + "".join(f"{lbl:>18}" for _, lbl, _ in REPORT)
    print(header)
    print("-" * len(header))
    for name in LEVERS:
        m = results[name]["metrics"]
        cells = ""
        for k, _, dec in REPORT:
            v = m[k] / 1e9 if k == "lender_cumulative_loss_eur" else m[k]
            cells += f"{v:>18.{dec}f}"
        print(f"{name:<18}{cells}")
    print("-" * len(header))

    # --- ranked by € lender loss avoided vs baseline ---
    print(f"\nMarginal benefit vs baseline (by {year}), ranked by lender loss avoided:")
    ranked = sorted(
        ((name, base["lender_cumulative_loss_eur"] - results[name]["metrics"]["lender_cumulative_loss_eur"])
         for name in LEVERS if name != "baseline"),
        key=lambda t: t[1], reverse=True)
    for name, loss_avoided in ranked:
        m = results[name]["metrics"]
        uw = base["pct_underwater"] - m["pct_underwater"]
        df = base["cumulative_default_rate"] - m["cumulative_default_rate"]
        print(f"  {name:<18} €{loss_avoided / 1e9:5.1f}bn loss avoided | "
              f"{uw:+5.1f}pp underwater | {df:+5.1f}pp defaults")

    os.makedirs(OUT, exist_ok=True)
    out_path = os.path.join(OUT, "abm_experiments.json")
    with open(out_path, "w") as f:
        json.dump({"reps": args.reps, "n_agents": args.agents,
                   "levers": list(LEVERS), "results": results}, f, indent=1)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
