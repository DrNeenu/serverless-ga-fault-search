"""
ablation.py  -  Experiment 3: live ablation of the GA.

Four variants, each with ONE component disabled:
  no_mutation    : children are not mutated (budget repair still applied)
  no_crossover   : child = copy of the first tournament-selected parent
  no_tournament  : parents chosen uniformly at random
  no_queue       : search objective ignores M5 (its weight spread over M1-M4)

Each variant: 2 runs x 30 fresh evaluations, seeds 101 and 102 - the SAME seeds as
GA runs 1 and 2 in compare_runs.py, so every variant starts from the identical
initial population as the full GA.

Every evaluation is ALSO scored with the full, unchanged fitness f(C), so all
variants can be compared on one common criterion (Reviewer 1).

USAGE (same folder as compare_runs.py):
    caffeinate -i python3 ablation.py
If it stops, run it again - completed runs are skipped.
Expected duration: about 8-10 hours (240 evaluations).
"""

import csv
import datetime as dt
import os
import random

import compare_runs as cr

VARIANTS = ["no_mutation", "no_crossover", "no_tournament", "no_queue"]
SEEDS = [101, 102]
OUT_CSV = os.path.join("results", "ablation.csv")
NO_QUEUE_WEIGHTS = [0.25, 0.25, 0.25, 0.25, 0.0]

HEADER = (["variant", "run", "seed", "eval", "generation", "d1", "d2", "p1", "p2",
           "fitness_objective", "fitness_full", "M1", "M2", "M3", "M4", "M5",
           "best_objective_so_far", "timestamp"] + cr.RAW_KEYS)


def run_variant(meas, writer, f, variant, run, seed):
    rng = random.Random(seed)
    pop = [cr.random_chromosome(rng) for _ in range(cr.POP_SIZE)]
    best, ev = -1.0, 0
    for gen in range(1, cr.N_GENERATIONS + 1):
        fits = []
        for c in pop:
            ev += 1
            raw = meas.measure(c)
            full, m = cr.fitness_from_raw(raw)
            obj = sum(w * x for w, x in zip(NO_QUEUE_WEIGHTS, m)) if variant == "no_queue" else full
            best = max(best, obj)
            fits.append(obj)
            writer.writerow([variant, run, seed, ev, gen, *[round(x, 4) for x in c],
                             round(obj, 4), round(full, 4), *[round(x, 4) for x in m],
                             round(best, 4), dt.datetime.now().isoformat(timespec="seconds")]
                            + [raw[k] for k in cr.RAW_KEYS])
            f.flush()
            print(f"[{variant} run{run} eval {ev:02d}] obj={obj:.3f} full={full:.3f}", flush=True)
        if gen == cr.N_GENERATIONS:
            break
        if all(x == 0 for x in fits):
            pop = [cr.random_chromosome(rng) for _ in range(cr.POP_SIZE)]
            continue
        elite = pop[max(range(cr.POP_SIZE), key=lambda i: fits[i])]
        new = [list(elite)]
        while len(new) < cr.POP_SIZE:
            if variant == "no_tournament":
                a, b = rng.choice(pop), rng.choice(pop)
            else:
                a, b = cr.tournament(pop, fits, rng), cr.tournament(pop, fits, rng)
            if variant == "no_crossover":
                child = list(a)
            else:
                child, _ = cr.crossover(a, b, rng)
            child = cr.clip_and_repair(child)
            if variant != "no_mutation":
                child = cr.mutate(child, rng)
            new.append(child)
        pop = new


def main():
    meas = cr.AwsMeasurer()
    counts = {}
    if os.path.exists(OUT_CSV):
        with open(OUT_CSV) as fh:
            for r in csv.DictReader(fh):
                counts[(r["variant"], r["run"])] = counts.get((r["variant"], r["run"]), 0) + 1
    new_file = not os.path.exists(OUT_CSV)
    f = open(OUT_CSV, "a", newline="")
    w = csv.writer(f)
    if new_file:
        w.writerow(HEADER)
        f.flush()
    for run, seed in enumerate(SEEDS, start=1):
        for v in VARIANTS:
            n = counts.get((v, str(run)), 0)
            if n >= cr.EVALS_PER_RUN:
                print(f"skip {v} run{run} (complete)")
                continue
            if n:
                print(f"NOTE: {v} run{run} was incomplete; rerunning (partial rows removed in analysis)")
            run_variant(meas, w, f, v, run, seed)
    f.close()
    print("\nALL DONE. Send me results/ablation.csv")


if __name__ == "__main__":
    main()
