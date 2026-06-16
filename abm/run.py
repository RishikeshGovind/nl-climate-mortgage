"""
CLIMADAPT-NL — Monte-Carlo ensemble runner (Phase 2)
====================================================

Runs the ABM as an N-replication ensemble (ODD §4 Stochasticity; §7), reports the
distribution of portfolio outcomes per year, and writes a JSON the existing
front-end can consume. Performs the t=0 calibration check the supervisor expects
(§8.1): national pop-weighted LTV ~ 68% and ~3% of mortgages already > 100%.

Phase 2 adds adaptation + social learning, so the runner also reports the
adaptation uptake curve (should be S-shaped — §1 pattern 1) and offers a
`--compare` mode that runs the ensemble with adaptation OFF vs ON to quantify the
resilience benefit — a direct answer to "strengthening portfolio resilience".

Usage:
    python3 abm/run.py                 # default ensemble (20 reps, 25k agents), adaptation ON
    python3 abm/run.py --compare       # adaptation OFF vs ON, report the resilience delta
    python3 abm/run.py --no-adapt      # Phase 1 behaviour (adaptation disabled)
    python3 abm/run.py --quick         # fast smoke run (5 reps, 6k agents)
    python3 abm/run.py --reps 50 --agents 40000
"""

from __future__ import annotations

import argparse
import json
import os
import statistics

from model import ClimadaptModel, Params

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "outputs")

METRICS = ["pct_underwater", "mortgage_at_risk_eur", "cumulative_default_rate",
           "lender_cumulative_loss_eur", "lender_capital_ratio", "annual_defaults",
           "adaptation_uptake", "mean_risk_perception"]


def percentile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def calibration_check(model):
    """Weighted mean LTV and % underwater across the freshly built population."""
    wsum = sum(a.weight for a in model.schedule.agents)
    mean_ltv = sum(a.ltv * a.weight for a in model.schedule.agents) / wsum
    pct_uw = 100.0 * sum(a.weight for a in model.schedule.agents if a.ltv > 100) / wsum
    return mean_ltv, pct_uw


def run_ensemble(params, reps, label, quiet=False):
    """Run `reps` replications and aggregate per-year percentiles + gemeente clusters."""
    if not quiet:
        print(f"Running {reps} reps x {params.n_agents} agents [{label}], "
              f"{params.start_year}-{params.end_year} ...")
    per_year, gem_rates = {}, {}
    for rep in range(reps):
        m = ClimadaptModel(params, seed=1000 + rep)
        for row in m.run():
            bucket = per_year.setdefault(row["year"], {mt: [] for mt in METRICS})
            for mt in METRICS:
                bucket[mt].append(row[mt])
        for gm, gr in m.gemeente_default_rates().items():
            slot = gem_rates.setdefault(gm, {"name": gr["name"],
                                             "worst_exposure": gr["worst_exposure"],
                                             "rates": []})
            slot["rates"].append(gr["default_rate"])
        if not quiet:
            print(f"  [{label}] rep {rep + 1}/{reps} done")

    ensemble = []
    for year in sorted(per_year):
        entry = {"year": year}
        for mt in METRICS:
            vals = sorted(per_year[year][mt])
            entry[mt] = {"mean": statistics.fmean(vals), "p5": percentile(vals, 0.05),
                         "p50": percentile(vals, 0.50), "p95": percentile(vals, 0.95)}
        ensemble.append(entry)

    gem_out = sorted(
        ({"gm": gm, "name": v["name"], "worst_exposure": v["worst_exposure"],
          "default_rate": statistics.fmean(v["rates"])} for gm, v in gem_rates.items()),
        key=lambda d: d["default_rate"], reverse=True)
    # Concentration: share of all defaults in the worst-exposure decile (§1 pattern 3).
    by_exp = sorted(gem_rates.values(), key=lambda v: v["worst_exposure"], reverse=True)
    k = max(1, len(by_exp) // 10)
    top = sum(statistics.fmean(v["rates"]) for v in by_exp[:k])
    alld = sum(statistics.fmean(v["rates"]) for v in by_exp) or 1.0
    concentration = 100.0 * top / alld
    return ensemble, gem_out, concentration


def summarize(ensemble, concentration, gem_out, label):
    first, last = ensemble[0], ensemble[-1]
    print("-" * 64)
    print(f"[{label}] emergent portfolio trajectory ({first['year']} -> {last['year']}):")
    print(f"  % underwater            : {first['pct_underwater']['mean']:5.1f}%"
          f"  ->  {last['pct_underwater']['mean']:5.1f}%   (p95 {last['pct_underwater']['p95']:5.1f}%)")
    print(f"  cumulative default rate -> {last['cumulative_default_rate']['mean']:5.2f}%"
          f"   (p95 {last['cumulative_default_rate']['p95']:5.2f}%)")
    print(f"  lender capital ratio    -> {last['lender_capital_ratio']['mean']:5.2f}"
          f"   (p5 {last['lender_capital_ratio']['p5']:5.2f})")
    print(f"  adaptation uptake       -> {last['adaptation_uptake']['mean']:5.1f}%"
          f"   (S-curve; mean risk perception {last['mean_risk_perception']['mean']:.2f})")
    print(f"  spatial clustering      : worst-exposure decile holds "
          f"{concentration:4.1f}% of all defaults (uniform ~10%)")
    print(f"  hardest-hit gemeenten   : " +
          ", ".join(f"{g['name']} {g['default_rate']:.0f}%" for g in gem_out[:5]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--agents", type=int, default=25_000)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--compare", action="store_true", help="adaptation OFF vs ON")
    ap.add_argument("--no-adapt", action="store_true", help="disable adaptation (Phase 1)")
    args = ap.parse_args()
    if args.quick:
        args.reps, args.agents = 5, 6_000

    params = Params(n_agents=args.agents, adaptation_enabled=not args.no_adapt)

    # --- t=0 calibration gate (§8.1) ---
    mean_ltv, pct_uw = calibration_check(ClimadaptModel(params, seed=0))
    ok = (64 <= mean_ltv <= 72) and (1.5 <= pct_uw <= 5.0)
    print("=" * 64)
    print("CALIBRATION CHECK at t=0 (target: LTV~68%, ~3% underwater)")
    print(f"  national pop-weighted mean LTV : {mean_ltv:5.1f}%   (target 68%)")
    print(f"  share of mortgages LTV > 100%  : {pct_uw:5.1f}%   (target ~3%)")
    print(f"  calibration {'PASSED' if ok else 'OUT OF RANGE — review params'}")
    print("=" * 64)

    comparison = None
    if args.compare:
        off_params = Params(n_agents=args.agents, adaptation_enabled=False)
        ens_off, gem_off, conc_off = run_ensemble(off_params, args.reps, "no-adapt")
        summarize(ens_off, conc_off, gem_off, "no-adapt")
        ens_on, gem_on, conc_on = run_ensemble(params, args.reps, "adapt")
        summarize(ens_on, conc_on, gem_on, "adapt")
        a, b = ens_off[-1], ens_on[-1]
        comparison = {
            "underwater_pp_avoided": a["pct_underwater"]["mean"] - b["pct_underwater"]["mean"],
            "default_pp_avoided": a["cumulative_default_rate"]["mean"] - b["cumulative_default_rate"]["mean"],
            "lender_loss_eur_avoided": a["lender_cumulative_loss_eur"]["mean"] - b["lender_cumulative_loss_eur"]["mean"],
        }
        print("=" * 64)
        print(f"RESILIENCE BENEFIT of household adaptation (by {b['year']}):")
        print(f"  underwater share      : {a['pct_underwater']['mean']:.1f}% -> "
              f"{b['pct_underwater']['mean']:.1f}%  ({comparison['underwater_pp_avoided']:+.1f} pp)")
        print(f"  cumulative defaults   : {a['cumulative_default_rate']['mean']:.1f}% -> "
              f"{b['cumulative_default_rate']['mean']:.1f}%  ({comparison['default_pp_avoided']:+.1f} pp)")
        print(f"  lender cumulative loss: €{comparison['lender_loss_eur_avoided'] / 1e9:.1f}bn avoided")
        print("=" * 64)
        ensemble, gem_out, concentration = ens_on, gem_on, conc_on
    else:
        ensemble, gem_out, concentration = run_ensemble(params, args.reps, "adapt"
                                                        if params.adaptation_enabled else "no-adapt")
        summarize(ensemble, concentration, gem_out,
                  "adapt" if params.adaptation_enabled else "no-adapt")

    os.makedirs(OUT, exist_ok=True)
    out_path = os.path.join(OUT, "abm_results.json")
    with open(out_path, "w") as f:
        json.dump({
            "meta": {
                "model": "CLIMADAPT-NL Phase 2",
                "reps": args.reps, "n_agents": args.agents,
                "adaptation_enabled": params.adaptation_enabled,
                "calibration": {"mean_ltv": mean_ltv, "pct_underwater_t0": pct_uw, "passed": ok},
                "resilience_comparison": comparison,
            },
            "params": vars(params),
            "ensemble": ensemble,
            "gemeente_clusters": gem_out,
        }, f, indent=1)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
