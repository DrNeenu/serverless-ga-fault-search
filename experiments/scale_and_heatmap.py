"""
scale_and_heatmap.py  -  Experiment 2: scalability + fitness heatmap.

Uses the SAME measurement code as compare_runs.py (imported), so every number
is computed exactly the same way.

Part A - Scalability (replaces Table 4 / Fig. 3):
    workloads of 50, 200, 500 and 1000 requests, for three configurations:
      * Baseline  (no faults)
      * GA-best   (GA configuration with the highest mean re-test fitness)
      * RS-best   (random-search configuration with the highest mean re-test fitness)
    Each (workload, configuration) pair is measured 2 times -> 24 evaluations.
    GA-best / RS-best are read automatically from results/all_evaluations.csv.

Part B - Heatmap (replaces Fig. 6):
    symmetric probe d1 = d2 = d, p1 = p2 = p on the grid
    d in {100, 500, 1000, 2000} ms, p in {0.05, 0.15, 0.30, 0.50};
    cells violating the fault budget are recorded as infeasible (not measured);
    each feasible cell is measured 2 times -> 18 evaluations.

A 60-second idle gap separates consecutive evaluations.

USAGE (same folder as compare_runs.py, aws_metrics.py and the results/ folder):
    caffeinate -i python3 scale_and_heatmap.py
If it stops, run it again - finished evaluations are skipped.
Expected duration: about 3-4 hours.
"""

import csv
import os
import statistics
import time

import compare_runs as cr

WORKLOADS = [50, 200, 500, 1000]
SCALE_REPEATS = 2
GRID_D = [100, 500, 1000, 2000]
GRID_P = [0.05, 0.15, 0.30, 0.50]
HEAT_REPEATS = 2
IDLE_GAP_S = 60

OUT = "results"
SCALE_CSV = os.path.join(OUT, "scalability.csv")
HEAT_CSV = os.path.join(OUT, "heatmap.csv")


def best_configs(path=os.path.join(OUT, "all_evaluations.csv")):
    """Best configuration per method = highest MEAN re-test fitness."""
    groups = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["phase"] == "retest":
                key = (r["method"], r["run"])
                c = tuple(float(r[k]) for k in ("d1", "d2", "p1", "p2"))
                groups.setdefault(key, {"c": c, "f": []})["f"].append(float(r["fitness"]))
    best = {}
    for (method, run), g in groups.items():
        m = statistics.mean(g["f"])
        if method not in best or m > best[method][1]:
            best[method] = (list(g["c"]), m, run)
    return best


def done_keys(path, keycols):
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return {tuple(r[k] for k in keycols) for r in csv.DictReader(f)}


def open_writer(path, header):
    new = not os.path.exists(path)
    f = open(path, "a", newline="")
    w = csv.writer(f)
    if new:
        w.writerow(header)
        f.flush()
    return f, w


def measure_row(meas, c, n_requests):
    cr.REQUESTS_PER_EVAL = n_requests
    cr.DRAIN_MAX_S = max(120, 2 * n_requests)      # bigger workloads need longer to drain
    raw = meas.measure(c)
    fit, m = cr.fitness_from_raw(raw)
    time.sleep(IDLE_GAP_S)
    return raw, fit, m


def main():
    meas = cr.AwsMeasurer()
    best = best_configs()
    configs = {"Baseline": [0.0, 0.0, 0.0, 0.0],
               "GA-best": best["GA"][0],
               "RS-best": best["RS"][0]}
    print("Configurations used:")
    for k, v in configs.items():
        print(f"  {k}: {[round(x, 3) for x in v]}")

    # ---------------- Part A: scalability ----------------
    header = (["config", "workload", "repeat", "d1", "d2", "p1", "p2", "fitness",
               "M1", "M2", "M3", "M4", "M5"] + cr.RAW_KEYS)
    done = done_keys(SCALE_CSV, ["config", "workload", "repeat"])
    f, w = open_writer(SCALE_CSV, header)
    for rep in range(1, SCALE_REPEATS + 1):
        for n in WORKLOADS:
            for name, c in configs.items():
                if (name, str(n), str(rep)) in done:
                    continue
                raw, fit, m = measure_row(meas, c, n)
                w.writerow([name, n, rep, *[round(x, 4) for x in c], round(fit, 4),
                            *[round(x, 4) for x in m]] + [raw[k] for k in cr.RAW_KEYS])
                f.flush()
                print(f"[scale {name} n={n} rep={rep}] f={fit:.3f} drain={raw['drain_s']}s "
                      f"thr={raw['fanout_throttles']}/{raw['agg_throttles']}", flush=True)
    f.close()

    # ---------------- Part B: heatmap ----------------
    header = (["delay_ms", "failure_prob", "repeat", "feasible", "fitness",
               "M1", "M2", "M3", "M4", "M5"] + cr.RAW_KEYS)
    done = done_keys(HEAT_CSV, ["delay_ms", "failure_prob", "repeat"])
    f, w = open_writer(HEAT_CSV, header)
    for rep in range(1, HEAT_REPEATS + 1):
        for d in GRID_D:
            for p in GRID_P:
                if (str(d), str(p), str(rep)) in done:
                    continue
                feasible = (2 * d <= cr.D_BUDGET) and (2 * p <= cr.P_BUDGET)
                if not feasible:
                    w.writerow([d, p, rep, False, "", "", "", "", "", ""] + [""] * len(cr.RAW_KEYS))
                    f.flush()
                    continue
                raw, fit, m = measure_row(meas, [d, d, p, p], 50)
                w.writerow([d, p, rep, True, round(fit, 4), *[round(x, 4) for x in m]]
                           + [raw[k] for k in cr.RAW_KEYS])
                f.flush()
                print(f"[heat d={d} p={p} rep={rep}] f={fit:.3f}", flush=True)
    f.close()
    print("\nALL DONE. Send me results/scalability.csv and results/heatmap.csv")


if __name__ == "__main__":
    main()
