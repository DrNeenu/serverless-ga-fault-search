"""
Reproduces every table value and figure in Section 5 of the paper from the raw
CSV files in ../data. Run from the analysis/ folder:

    python3 make_results.py

Requires: Python 3.9+, matplotlib.
Figures are written to ../figures; numbers are printed to the terminal.

Data cleaning (the only rows excluded):
  * all_evaluations.csv: the first 29 rows of GA run 1 come from an attempt that
    stopped at evaluation 29 when the client machine went to sleep; GA run 1 was
    then repeated in full (same seed) and only the complete repetition is used.
  * ablation.csv: the first row of 'no_tournament' run 2 comes from an attempt that
    stopped after one evaluation (network outage); the run was repeated in full.
"""

import csv
import math
import os
import statistics as st

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

DATA = os.path.join("..", "data")
FIGS = os.path.join("..", "figures")
os.makedirs(FIGS, exist_ok=True)
COL = {"GA": "#2a78d6", "RS": "#eb6834", "BASE": "#888888"}


def load(name):
    with open(os.path.join(DATA, name)) as f:
        return list(csv.DictReader(f))


def F(x, k):
    return float(x[k])


def ms(v):
    return f"{st.mean(v):.3f} +- {st.stdev(v):.3f}" if len(v) > 1 else f"{v[0]:.3f}"


# ---------------------------------------------------------------- main data
raw = load("all_evaluations.csv")
ga1_all = [x for x in raw if x["phase"] == "search" and x["method"] == "GA" and x["run"] == "1"]
partial = ga1_all[:-30]
assert len(partial) == 29
ev = [x for x in raw if x not in partial]
search = [x for x in ev if x["phase"] == "search"]
runs = {(m, r): [x for x in search if x["method"] == m and x["run"] == r]
        for m in ("GA", "RS") for r in ("1", "2", "3")}
assert all(len(v) == 30 for v in runs.values())

print("=== Table 3: search performance (per run) ===")
for m in ("RS", "GA"):
    R = [runs[(m, r)] for r in ("1", "2", "3")]
    best = [max(F(x, "fitness") for x in r) for r in R]
    mean = [st.mean(F(x, "fitness") for x in r) for r in R]
    first06 = [next((int(x["eval"]) for x in r if F(x, "best_so_far") >= 0.6), None) for r in R]
    distinct = [len({tuple(x[k] for k in ("d1", "d2", "p1", "p2")) for x in r}) for r in R]
    print(m, "best", [round(b, 3) for b in best], ms(best), "| mean", ms(mean),
          "| evals to 0.6", first06, "| distinct", distinct)

print("\n=== Table 3: repeated measurements ===")
groups = {"Baseline": [x for x in ev if x["phase"] == "baseline"],
          "RS": [x for x in ev if x["phase"] == "retest" and x["method"] == "RS"],
          "GA": [x for x in ev if x["phase"] == "retest" and x["method"] == "GA"]}
rows = [("Fitness", lambda x: F(x, "fitness")),
        ("Fan-out error rate (%)", lambda x: 100 * F(x, "fanout_error_rate")),
        ("Fan-out throttles", lambda x: F(x, "fanout_throttles")),
        ("Aggregate throttles", lambda x: F(x, "agg_throttles")),
        ("Aggregate errors", lambda x: F(x, "agg_errors")),
        ("Fan-out p95 duration (ms)", lambda x: F(x, "fanout_p95_ms")),
        ("Max. queue age (s)", lambda x: F(x, "sqs_max_age_s")),
        ("Cold starts (both stages)", lambda x: F(x, "fanout_cold_starts") + F(x, "agg_cold_starts"))]
for label, fn in rows:
    print(f"{label:28s}", " | ".join(f"{g}: {ms([fn(x) for x in v])}" for g, v in groups.items()))
ret = [F(x, "fitness") for x in ev if x["phase"] == "retest"]
print("repeated-measurement range:", round(min(ret), 2), "-", round(max(ret), 2))
fs = [x for x in ev if x["phase"] in ("search", "retest")]
print("cold-start rate fan-out %.3f, aggregate %.3f" % (
    sum(F(x, "fanout_cold_starts") for x in search) / sum(F(x, "fanout_invocations") for x in search),
    sum(F(x, "agg_cold_starts") for x in search) / sum(F(x, "agg_invocations") for x in search)))

# convergence figure
fig, ax = plt.subplots(figsize=(6, 3.6), dpi=200)
lab = {"GA": "Genetic algorithm", "RS": "Random search"}
for m in ("GA", "RS"):
    curves = [[F(x, "best_so_far") for x in runs[(m, r)]] for r in ("1", "2", "3")]
    for c in curves:
        ax.plot(range(1, 31), c, color=COL[m], lw=1, alpha=0.35)
    ax.plot(range(1, 31), [sum(c[j] for c in curves) / 3 for j in range(30)], color=COL[m], lw=2,
            label=f"{lab[m]} (mean of 3 runs; thin lines = individual runs)")
base = st.mean(F(x, "fitness") for x in groups["Baseline"])
ax.axhline(base, color="#888", lw=1, ls="--")
ax.text(1.5, base + 0.015, "No-fault baseline (mean of 3)", fontsize=8, color="#555")
ax.set_xlabel("Evaluations used"); ax.set_ylabel("Best fitness observed so far")
ax.set_xlim(1, 30); ax.set_ylim(0, 0.85); ax.grid(axis="y", color="#e5e5e5", lw=0.6)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
ax.legend(frameon=False, fontsize=7.5, loc="lower right", bbox_to_anchor=(1, 0.2))
fig.tight_layout(); fig.savefig(os.path.join(FIGS, "convergence.pdf")); plt.close(fig)

# ---------------------------------------------------------------- latency attribution
print("\n=== Latency attribution ===")
def fit(X, Y):
    mx, my = st.mean(X), st.mean(Y)
    b = sum((a - mx) * (c - my) for a, c in zip(X, Y)) / sum((a - mx) ** 2 for a in X)
    a0 = my - b * mx
    r2 = 1 - sum((c - (a0 + b * a)) ** 2 for a, c in zip(X, Y)) / sum((c - my) ** 2 for c in Y)
    return a0, b, r2
fits = {}
for d, p in (("d1", "fanout_p95_ms"), ("d2", "agg_p95_ms")):
    fits[d] = fit([F(x, d) for x in fs], [F(x, p) for x in fs])
    print(p, "= %.0f + %.2f * %s (R2 = %.2f, n = %d)" % (*fits[d][:2], d, fits[d][2], len(fs)))
for k in ("fanout_excess_p95_ms", "agg_excess_p95_ms"):
    for m in ("GA", "RS"):
        v = [F(x, k) for x in fs if x["method"] == m]
        print(k, m, "%.0f +- %.0f" % (st.mean(v), st.stdev(v)))
    print(k, "baseline", [round(F(x, k)) for x in groups["Baseline"]])
fig, axs = plt.subplots(1, 2, figsize=(7.5, 3.3), dpi=200)
for ax, (d, p, title) in zip(axs, (("d1", "fanout_p95_ms", "Fan-out stage"), ("d2", "agg_p95_ms", "Aggregate stage"))):
    for m, l in (("GA", "GA"), ("RS", "Random search")):
        v = [x for x in fs if x["method"] == m]
        ax.scatter([F(x, d) for x in v], [F(x, p) for x in v], s=10, color=COL[m], alpha=0.6, label=l, edgecolors="none")
    a0, b, _ = fits[d]
    ax.plot([0, 2000], [a0, a0 + b * 2000], color="#444", lw=1, label=f"Fit: {a0:.0f} + {b:.2f}·d")
    ax.plot([0, 2000], [0, 2000], color="#999", lw=1, ls="--", label="p95 = injected delay")
    ax.set_title(title, fontsize=9); ax.set_xlabel(f"Injected delay ${d[0]}_{d[1]}$ (ms)"); ax.set_ylabel("p95 duration (ms)")
    ax.grid(color="#eee", lw=0.6); ax.legend(frameon=False, fontsize=6.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
fig.tight_layout(); fig.savefig(os.path.join(FIGS, "latency_attribution.pdf")); plt.close(fig)

# ---------------------------------------------------------------- sensitivity
print("\n=== Sensitivity to weights and normalisation ===")
def Ms(x, thr=50, age=120):
    return [min(1, F(x, "fanout_error_rate") / 0.2), min(1, F(x, "fanout_throttles") / thr),
            min(1, F(x, "fanout_p95_ms") / 10000), min(1, F(x, "agg_throttles") / thr),
            min(1, F(x, "sqs_max_age_s") / age)]
def rank(v):
    o = sorted(range(len(v)), key=lambda i: v[i]); rk = [0] * len(v)
    for j, i in enumerate(o):
        rk[i] = j
    return rk
def spearman(a, b):
    ra, rb, n = rank(a), rank(b), len(a)
    return 1 - 6 * sum((x - y) ** 2 for x, y in zip(ra, rb)) / (n * (n * n - 1))
schemes = [("Uniform (used)", [.2] * 5, {}), ("Error-weighted", [.4, .15, .15, .15, .15], {}),
           ("Throttle-weighted", [.1, .3, .1, .3, .2], {}), ("Latency-weighted", [.1, .1, .6, .1, .1], {}),
           ("Queue-weighted", [.15, .15, .15, .15, .4], {}), ("Without M1", [0, .25, .25, .25, .25], {}),
           ("Without M2 and M4", [1 / 3, 0, 1 / 3, 0, 1 / 3], {}), ("Throttle divisor 25", [.2] * 5, {"thr": 25}),
           ("Throttle divisor 100", [.2] * 5, {"thr": 100}), ("Queue-age divisor 60 s", [.2] * 5, {"age": 60}),
           ("Queue-age divisor 240 s", [.2] * 5, {"age": 240})]
uni = [sum(w * m for w, m in zip([.2] * 5, Ms(x))) for x in search]
retest = [x for x in ev if x["phase"] == "retest"]
for name, w, kw in schemes:
    fn = lambda x: sum(a * b for a, b in zip(w, Ms(x, **kw)))
    gb = [max(fn(x) for x in runs[("GA", r)]) for r in "123"]
    rb = [max(fn(x) for x in runs[("RS", r)]) for r in "123"]
    gm = [st.mean(fn(x) for x in runs[("GA", r)]) for r in "123"]
    rm = [st.mean(fn(x) for x in runs[("RS", r)]) for r in "123"]
    means = {}
    for x in retest:
        means.setdefault((x["method"], x["run"]), []).append(fn(x))
    top = max(means, key=lambda k: st.mean(means[k]))
    print(f"{name:24s} GA wins {sum(g > q for g, q in zip(gb, rb))}/3  best {st.mean(gb):.3f}/{st.mean(rb):.3f}"
          f"  mean {st.mean(gm):.3f}/{st.mean(rm):.3f}  rho {spearman(uni, [fn(x) for x in search]):.2f}  top {top}")
hi = [x for x in search if F(x, "fitness") >= 0.6]; lo = [x for x in search if F(x, "fitness") < 0.3]
rule = lambda x: F(x, "fanout_p95_ms") > 3000 or F(x, "fanout_error_rate") > 0.05
print("failure rule: f>=0.6 %d/%d, f<0.3 %d/%d, all %d/%d" % (
    sum(map(rule, hi)), len(hi), sum(map(rule, lo)), len(lo), sum(map(rule, search)), len(search)))

# ---------------------------------------------------------------- scalability
print("\n=== Table 4: scalability (mean of 2) ===")
sc = load("scalability.csv")
W = [50, 200, 500, 1000]
for c in ("Baseline", "RS-best", "GA-best"):
    for n in W:
        v = [x for x in sc if x["config"] == c and x["workload"] == str(n)]
        g = lambda fn: st.mean(fn(x) for x in v)
        print(f"{c:9s} {n:5d}  rej {g(lambda x: 100 * F(x, 'requests_failed') / n):5.1f}%"
              f"  fan-err {g(lambda x: 100 * F(x, 'fanout_error_rate')):5.1f}%  agg-err {g(lambda x: F(x, 'agg_errors')):6.1f}"
              f"  age {g(lambda x: F(x, 'sqs_max_age_s')):6.1f}  drain {g(lambda x: F(x, 'drain_s')):6.1f}")
fig, axs = plt.subplots(1, 2, figsize=(7.5, 3.2), dpi=200)
lab2 = {"Baseline": "Baseline (no faults)", "RS-best": "Random-search best", "GA-best": "GA best"}
col2 = {"Baseline": COL["BASE"], "RS-best": COL["RS"], "GA-best": COL["GA"]}
for ax, (k, yl) in zip(axs, (("sqs_max_age_s", "Max. queue age (s)"), ("drain_s", "Time to drain workload (s)"))):
    for c in ("Baseline", "RS-best", "GA-best"):
        means = []
        for n in W:
            v = [F(x, k) for x in sc if x["config"] == c and x["workload"] == str(n)]
            ax.scatter([n] * len(v), v, s=12, color=col2[c], alpha=0.5, edgecolors="none")
            means.append(st.mean(v))
        ax.plot(W, means, color=col2[c], lw=2, marker="o", ms=4, label=lab2[c])
    ax.set_xlabel("Requests per evaluation"); ax.set_ylabel(yl); ax.set_xticks(W)
    ax.grid(color="#eee", lw=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
axs[0].legend(frameon=False, fontsize=7)
fig.tight_layout(); fig.savefig(os.path.join(FIGS, "scalability.pdf")); plt.close(fig)

# ---------------------------------------------------------------- heatmap
print("\n=== Heatmap (mean of 2 per feasible cell) ===")
hm = load("heatmap.csv")
D, P = [100, 500, 1000, 2000], [0.05, 0.15, 0.30, 0.50]
cmap = LinearSegmentedColormap.from_list("b", ["#cde2fb", "#2a78d6", "#0d366b"])
fig, ax = plt.subplots(figsize=(5.2, 3.8), dpi=200)
diffs = []
for i, d in enumerate(D):
    for j, p in enumerate(P):
        v = [F(x, "fitness") for x in hm if x["delay_ms"] == str(d)
             and abs(float(x["failure_prob"]) - p) < 1e-9 and x["feasible"] == "True"]
        if not v:
            ax.add_patch(plt.Rectangle((j, i), 1, 1, color="#d9d9d9", ec="white", lw=2))
            ax.text(j + .5, i + .5, "infeasible", ha="center", va="center", fontsize=7, color="#555")
            continue
        m = st.mean(v); diffs.append(abs(v[0] - v[1]))
        print(f"d={d:4d} p={p:.2f}: {m:.3f} {v}")
        ax.add_patch(plt.Rectangle((j, i), 1, 1, color=cmap((m - 0.1) / 0.5), ec="white", lw=2))
        tc = "white" if m > 0.25 else "#1a1a19"
        ax.text(j + .5, i + .58, f"{m:.3f}", ha="center", va="center", fontsize=9, color=tc, weight="bold")
        ax.text(j + .5, i + .3, f"({min(v):.2f}–{max(v):.2f})", ha="center", va="center", fontsize=6.5, color=tc)
print("repeat difference: mean %.2f, max %.2f" % (st.mean(diffs), max(diffs)))
ax.set_xlim(0, 4); ax.set_ylim(0, 4)
ax.set_xticks([k + .5 for k in range(4)]); ax.set_xticklabels([f"{p:.2f}" for p in P])
ax.set_yticks([k + .5 for k in range(4)]); ax.set_yticklabels([str(d) for d in D])
ax.set_xlabel("Failure probability $p_1 = p_2$"); ax.set_ylabel("Injected delay $d_1 = d_2$ (ms)")
for s in ax.spines:
    ax.spines[s].set_visible(False)
ax.tick_params(length=0)
fig.tight_layout(); fig.savefig(os.path.join(FIGS, "heatmap.pdf")); plt.close(fig)

# ---------------------------------------------------------------- ablation
print("\n=== Table 6: ablation (full fitness) ===")
ab = load("ablation.csv")
nt2 = [x for x in ab if x["variant"] == "no_tournament" and x["run"] == "2"]
if len(nt2) == 31:
    ab = [x for x in ab if x is not nt2[0]]
def summ(rows, key):
    fsv = [F(x, key) for x in rows]
    first = next((i + 1 for i in range(len(rows)) if max(fsv[:i + 1]) >= 0.6), None)
    return max(fsv), st.mean(fsv), first
for name, R, key in [("Full GA", [runs[("GA", "1")], runs[("GA", "2")]], "fitness")] + \
        [(v, [[x for x in ab if x["variant"] == v and x["run"] == r] for r in "12"], "fitness_full")
         for v in ("no_mutation", "no_crossover", "no_tournament", "no_queue")]:
    s = [summ(r, key) for r in R]
    print(f"{name:14s} best {[round(b, 3) for b, _, _ in s]} (mean {st.mean(b for b, _, _ in s):.3f})"
          f"  mean-of-all {st.mean(m for _, m, _ in s):.3f}  evals-to-0.6 {[e for _, _, e in s]}")

print("\nFigures written to", os.path.abspath(FIGS))
