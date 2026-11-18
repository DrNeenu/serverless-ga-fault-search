"""
compare_runs.py  -  Budget-matched, replicated comparison of GA vs random search.

What it runs (in this order, interleaved to spread out time-of-day effects):
  1. Baseline: 3 evaluations with NO faults injected.
  2. GA run 1, Random run 1, GA run 2, Random run 2, GA run 3, Random run 3.
     Each run = exactly 30 FRESH evaluations (no cached / reused scores).
  3. Re-test: the best configuration of each run is measured 3 more times.

Every evaluation follows the same protocol:
  write config (with version number) -> wait 5 s -> send 50 requests
  (10 concurrent client threads) -> wait for the SQS queue to drain
  (max 120 s, then purge) -> wait until CloudWatch has the data -> read metrics.

All raw metrics (M1..M5 inputs, cold starts, excess latency) are saved to CSV.

USAGE (run from the folder that also contains aws_metrics.py):
  python3 compare_runs.py --check      # ONE real test with zero faults (~3 min)
  python3 compare_runs.py              # the full experiment (~8 hours)
  python3 compare_runs.py --dry-run    # simulated metrics, for testing the code only;
                                       # writes to results_DRYRUN/ - never use in paper

If the full run stops (laptop sleeps, Wi-Fi drops), just run it again:
finished runs are skipped automatically.
"""

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import json
import math
import os
import random
import re
import statistics
import time

# ----------------------------------------------------------------------
# SETTINGS (these are the values reported in the paper)
# ----------------------------------------------------------------------
D_MAX, P_MAX = 2000.0, 0.50
D_BUDGET, P_BUDGET = 2600.0, 0.65          # = 0.65 * (2*Dmax), 0.65 * (2*Pmax)

POP_SIZE, N_GENERATIONS = 6, 5             # 30 evaluations per GA run
EVALS_PER_RUN = POP_SIZE * N_GENERATIONS   # random search gets the same 30
TOURNAMENT_K = 3
MUTATION_RATE = 0.15                       # applied independently to each gene
MUTATION_STD_D, MUTATION_STD_P = 100.0, 0.05

WEIGHTS = [0.2, 0.2, 0.2, 0.2, 0.2]

N_REPLICATES = 3                           # runs per method
SEEDS = {"GA": [101, 102, 103], "RS": [201, 202, 203]}
N_BASELINE = 3
N_RETEST = 3

REQUESTS_PER_EVAL = 50
CLIENT_THREADS = 10
CONFIG_PROPAGATION_S = 5
DRAIN_MAX_S = 120
CW_WAIT_MAX_S = 300

FANOUT_TIMEOUT_MS = 10000.0
ERR_NORM, THR_NORM, AGE_NORM = 0.2, 50.0, 120.0

OUT_DIR = "results"


# ----------------------------------------------------------------------
# Search-space helpers
# ----------------------------------------------------------------------
def clip_and_repair(c):
    d1, d2, p1, p2 = c
    d1, d2 = min(D_MAX, max(0.0, d1)), min(D_MAX, max(0.0, d2))
    p1, p2 = min(P_MAX, max(0.0, p1)), min(P_MAX, max(0.0, p2))
    if d1 + d2 > D_BUDGET:
        s = D_BUDGET / (d1 + d2)
        d1, d2 = d1 * s, d2 * s
    if p1 + p2 > P_BUDGET:
        s = P_BUDGET / (p1 + p2)
        p1, p2 = p1 * s, p2 * s
    return [d1, d2, p1, p2]


def random_chromosome(rng):
    """Uniform sample from the budgeted region (rejection sampling)."""
    while True:
        c = [rng.uniform(0, D_MAX), rng.uniform(0, D_MAX),
             rng.uniform(0, P_MAX), rng.uniform(0, P_MAX)]
        if c[0] + c[1] <= D_BUDGET and c[2] + c[3] <= P_BUDGET:
            return c


def tournament(pop, fits, rng):
    idx = rng.sample(range(len(pop)), TOURNAMENT_K)
    return pop[max(idx, key=lambda i: fits[i])]


def crossover(a, b, rng):
    """Whole arithmetic crossover: child = alpha*a + (1-alpha)*b, alpha ~ U(0,1)."""
    alpha = rng.random()
    return [alpha * x + (1 - alpha) * y for x, y in zip(a, b)], alpha


def mutate(c, rng):
    stds = [MUTATION_STD_D, MUTATION_STD_D, MUTATION_STD_P, MUTATION_STD_P]
    return clip_and_repair([g + rng.gauss(0, s) if rng.random() < MUTATION_RATE else g
                            for g, s in zip(c, stds)])


def fitness_from_raw(raw):
    m = [
        min(1.0, raw["fanout_error_rate"] / ERR_NORM),
        min(1.0, raw["fanout_throttles"] / THR_NORM),
        min(1.0, raw["fanout_p95_ms"] / FANOUT_TIMEOUT_MS),
        min(1.0, raw["agg_throttles"] / THR_NORM),
        min(1.0, raw["sqs_max_age_s"] / AGE_NORM),
    ]
    return sum(w * x for w, x in zip(WEIGHTS, m)), m


# ----------------------------------------------------------------------
# AWS measurement
# ----------------------------------------------------------------------
class AwsMeasurer:
    def __init__(self):
        import boto3
        import requests
        from aws_metrics import CONFIG
        self.requests = requests
        self.cfg = CONFIG
        r = CONFIG["REGION"]
        self.table = boto3.resource("dynamodb", region_name=r).Table(CONFIG["CONFIG_TABLE"])
        self.cw = boto3.client("cloudwatch", region_name=r)
        self.sqs = boto3.client("sqs", region_name=r)
        self.logs = boto3.client("logs", region_name=r)
        self.lam = boto3.client("lambda", region_name=r)
        self.fan = CONFIG["FANOUT_FUNCTION_NAME"]
        self.agg = CONFIG["AGGREGATE_FUNCTION_NAME"]

        urls = self.sqs.list_queues(QueueNamePrefix="cascading-failure-research").get("QueueUrls", [])
        if len(urls) != 1:
            raise SystemExit(f"Expected exactly one queue, found: {urls}. "
                             "Add QUEUE_URL to CONFIG in aws_metrics.py.")
        self.queue_url = CONFIG.get("QUEUE_URL", urls[0])
        self.queue_name = self.queue_url.rstrip("/").split("/")[-1]
        self.version = 0

        # Async invocations of the aggregate function: no automatic Lambda retries,
        # so failed aggregate calls cannot re-appear in later evaluations.
        self.lam.put_function_event_invoke_config(FunctionName=self.agg,
                                                  MaximumRetryAttempts=0)

    def account_settings(self):
        s = self.lam.get_account_settings()["AccountLimit"]
        return {"concurrent_executions_quota": s.get("ConcurrentExecutions"),
                "unreserved_concurrency": s.get("UnreservedConcurrentExecutions")}

    # --- helpers ---
    def _write_config(self, c):
        self.version += 1
        self.table.put_item(Item={"config_id": "current",
                                  "d1": str(c[0]), "d2": str(c[1]),
                                  "p1": str(c[2]), "p2": str(c[3]),
                                  "version": str(self.version)})

    def _send(self):
        def one(_):
            t0 = time.time()
            try:
                r = self.requests.post(self.cfg["API_URL"], json={}, timeout=10)
                return r.status_code < 400, (time.time() - t0) * 1000
            except Exception:
                return False, (time.time() - t0) * 1000
        with cf.ThreadPoolExecutor(CLIENT_THREADS) as ex:
            res = list(ex.map(one, range(REQUESTS_PER_EVAL)))
        ok = sum(1 for a, _ in res if a)
        lat = sorted(l for _, l in res)
        return ok, len(res) - ok, lat

    def _queue_depth(self):
        a = self.sqs.get_queue_attributes(
            QueueUrl=self.queue_url,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])

    def _drain(self):
        t0 = time.time()
        time.sleep(15)          # SQS depth counters lag; never declare "empty" too early
        zeros = 0
        while time.time() - t0 < DRAIN_MAX_S:
            zeros = zeros + 1 if self._queue_depth() == 0 else 0
            if zeros >= 2:      # empty on two consecutive checks
                return time.time() - t0, False
            time.sleep(5)
        self.sqs.purge_queue(QueueUrl=self.queue_url)
        time.sleep(60)  # SQS purge can take up to 60 s
        return time.time() - t0, True

    def _stat(self, ns, metric, dims, start, end, stat):
        kw = dict(Namespace=ns, MetricName=metric, Dimensions=dims,
                  StartTime=start, EndTime=end, Period=60)
        if stat.startswith("p"):
            kw["ExtendedStatistics"] = [stat]
        else:
            kw["Statistics"] = [stat]
        pts = self.cw.get_metric_statistics(**kw).get("Datapoints", [])
        if not pts:
            return 0.0
        vals = [p["ExtendedStatistics"][stat] if stat.startswith("p") else p[stat] for p in pts]
        if stat in ("Sum",):
            return float(sum(vals))
        return float(max(vals))   # Maximum / percentiles: worst minute in the window

    def _cold_starts(self, fn, start, end):
        n, inits, token = 0, [], None
        while True:
            kw = dict(logGroupName=f"/aws/lambda/{fn}",
                      startTime=int(start.timestamp() * 1000),
                      endTime=int(end.timestamp() * 1000),
                      filterPattern='"Init Duration"')
            if token:
                kw["nextToken"] = token
            try:
                r = self.logs.filter_log_events(**kw)
            except self.logs.exceptions.ResourceNotFoundException:
                return 0, 0.0
            for e in r.get("events", []):
                m = re.search(r"Init Duration: ([\d.]+) ms", e["message"])
                if m:
                    n += 1
                    inits.append(float(m.group(1)))
            token = r.get("nextToken")
            if not token:
                break
        return n, (statistics.mean(inits) if inits else 0.0)

    # --- one evaluation ---
    def measure(self, c):
        self._write_config(c)
        time.sleep(CONFIG_PROPAGATION_S)

        start = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        ok, failed, client_lat = self._send()
        drain_s, purged = self._drain()
        end = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)

        fdims = [{"Name": "FunctionName", "Value": self.fan}]
        adims = [{"Name": "FunctionName", "Value": self.agg}]
        qdims = [{"Name": "QueueName", "Value": self.queue_name}]

        # wait until CloudWatch has received the fan-out invocations
        t0 = time.time()
        while True:
            time.sleep(30)
            inv = self._stat("AWS/Lambda", "Invocations", fdims, start, end, "Sum")
            if inv >= ok or time.time() - t0 > CW_WAIT_MAX_S:
                break
        time.sleep(30)  # let the remaining metrics land

        raw = {
            "requests_ok": ok, "requests_failed": failed,
            "client_p50_ms": client_lat[len(client_lat) // 2],
            "fanout_invocations": self._stat("AWS/Lambda", "Invocations", fdims, start, end, "Sum"),
            "fanout_errors": self._stat("AWS/Lambda", "Errors", fdims, start, end, "Sum"),
            "fanout_throttles": self._stat("AWS/Lambda", "Throttles", fdims, start, end, "Sum"),
            "fanout_p95_ms": self._stat("AWS/Lambda", "Duration", fdims, start, end, "p95"),
            "agg_invocations": self._stat("AWS/Lambda", "Invocations", adims, start, end, "Sum"),
            "agg_errors": self._stat("AWS/Lambda", "Errors", adims, start, end, "Sum"),
            "agg_throttles": self._stat("AWS/Lambda", "Throttles", adims, start, end, "Sum"),
            "agg_p95_ms": self._stat("AWS/Lambda", "Duration", adims, start, end, "p95"),
            "sqs_max_age_s": self._stat("AWS/SQS", "ApproximateAgeOfOldestMessage", qdims, start, end, "Maximum"),
            "drain_s": round(drain_s, 1), "purged": purged,
            "config_version": self.version,
            "window_start": start.isoformat(), "window_end": end.isoformat(),
        }
        inv = raw["fanout_invocations"]
        raw["fanout_error_rate"] = raw["fanout_errors"] / inv if inv else 0.0
        raw["fanout_cold_starts"], raw["fanout_init_ms_mean"] = self._cold_starts(self.fan, start, end)
        raw["agg_cold_starts"], raw["agg_init_ms_mean"] = self._cold_starts(self.agg, start, end)
        # latency beyond what was injected (reviewer request)
        raw["fanout_excess_p95_ms"] = raw["fanout_p95_ms"] - c[0]
        raw["agg_excess_p95_ms"] = raw["agg_p95_ms"] - c[1]
        return raw


class DryRunMeasurer:
    """Simulated metrics - ONLY for checking that the code runs. Never for the paper."""
    def __init__(self):
        self.rng = random.Random(0)
        self.version = 0

    def account_settings(self):
        return {"concurrent_executions_quota": "DRYRUN", "unreserved_concurrency": "DRYRUN"}

    def measure(self, c):
        self.version += 1
        p95 = c[0] + 150 + self.rng.gauss(0, 200)
        return {
            "requests_ok": 50, "requests_failed": 0, "client_p50_ms": 80.0,
            "fanout_invocations": 50 / (1 - c[2] + 1e-9), "fanout_errors": 50 * c[2],
            "fanout_throttles": 0, "fanout_p95_ms": max(0, p95),
            "agg_invocations": 150, "agg_errors": 150 * c[3], "agg_throttles": 0,
            "agg_p95_ms": c[1] + 100, "sqs_max_age_s": 30 * c[2] + self.rng.random() * 5,
            "drain_s": 10, "purged": False, "config_version": self.version,
            "window_start": "", "window_end": "",
            "fanout_error_rate": min(1, c[2] + self.rng.gauss(0, 0.05)) if c[2] else 0.0,
            "fanout_cold_starts": self.rng.randint(0, 5), "fanout_init_ms_mean": 300.0,
            "agg_cold_starts": self.rng.randint(0, 10), "agg_init_ms_mean": 300.0,
            "fanout_excess_p95_ms": p95 - c[0], "agg_excess_p95_ms": 100.0,
        }


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
RAW_KEYS = ["requests_ok", "requests_failed", "client_p50_ms",
            "fanout_invocations", "fanout_errors", "fanout_error_rate", "fanout_throttles",
            "fanout_p95_ms", "fanout_excess_p95_ms", "fanout_cold_starts", "fanout_init_ms_mean",
            "agg_invocations", "agg_errors", "agg_throttles", "agg_p95_ms", "agg_excess_p95_ms",
            "agg_cold_starts", "agg_init_ms_mean", "sqs_max_age_s", "drain_s", "purged",
            "config_version", "window_start", "window_end"]
HEADER = (["phase", "method", "run", "seed", "eval", "generation", "d1", "d2", "p1", "p2",
           "fitness", "M1", "M2", "M3", "M4", "M5", "best_so_far", "timestamp"] + RAW_KEYS)


class Logger:
    def __init__(self, path):
        new = not os.path.exists(path)
        self.f = open(path, "a", newline="")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(HEADER)
            self.f.flush()

    def log(self, phase, method, run, seed, ev, gen, c, f, m, best, raw):
        self.w.writerow([phase, method, run, seed, ev, gen, *[round(x, 4) for x in c],
                         round(f, 4), *[round(x, 4) for x in m], round(best, 4),
                         dt.datetime.now().isoformat(timespec="seconds")]
                        + [raw[k] for k in RAW_KEYS])
        self.f.flush()


def evaluate(meas, logger, phase, method, run, seed, ev, gen, c, best):
    raw = meas.measure(c)
    f, m = fitness_from_raw(raw)
    best = max(best, f)
    logger.log(phase, method, run, seed, ev, gen, c, f, m, best, raw)
    print(f"[{phase} {method}{run} eval {ev:02d}] c={[round(x, 3) for x in c]} "
          f"f={f:.3f} M={[round(x, 2) for x in m]} cold={raw['fanout_cold_starts']}/{raw['agg_cold_starts']}",
          flush=True)
    return f, best


# ----------------------------------------------------------------------
# Search methods (each = exactly EVALS_PER_RUN fresh evaluations)
# ----------------------------------------------------------------------
def run_random(meas, logger, run, seed):
    rng = random.Random(seed)
    best, best_c = -1.0, None
    for ev in range(1, EVALS_PER_RUN + 1):
        c = random_chromosome(rng)
        f, best = evaluate(meas, logger, "search", "RS", run, seed, ev, "", c, best)
        if f >= best:
            best_c = c
    return best_c, best


def run_ga(meas, logger, run, seed):
    rng = random.Random(seed)
    pop = [random_chromosome(rng) for _ in range(POP_SIZE)]
    best, best_c, ev = -1.0, None, 0
    for gen in range(1, N_GENERATIONS + 1):
        fits = []
        for c in pop:                      # every member is measured fresh, elite included
            ev += 1
            f, best = evaluate(meas, logger, "search", "GA", run, seed, ev, gen, c, best)
            fits.append(f)
            if f >= best:
                best_c = c
        if gen == N_GENERATIONS:
            break
        elite = pop[max(range(POP_SIZE), key=lambda i: fits[i])]
        if all(f == 0 for f in fits):
            pop = [random_chromosome(rng) for _ in range(POP_SIZE)]
            continue
        new = [list(elite)]                # elitism of size 1
        while len(new) < POP_SIZE:
            a, b = tournament(pop, fits, rng), tournament(pop, fits, rng)
            child, _ = crossover(a, b, rng)
            new.append(mutate(clip_and_repair(child), rng))
        pop = new
    return best_c, best


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def completed_runs(path):
    done = {}
    if not os.path.exists(path):
        return done
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["phase"] == "search":
                done.setdefault((r["method"], r["run"]), []).append(r)
            elif r["phase"] in ("baseline", "retest"):
                done.setdefault((r["phase"], r["method"], r["run"]), []).append(r)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="one real zero-fault test")
    ap.add_argument("--dry-run", action="store_true", help="simulated metrics, code test only")
    args = ap.parse_args()

    out = OUT_DIR + ("_DRYRUN" if args.dry_run else "")
    os.makedirs(out, exist_ok=True)
    meas = DryRunMeasurer() if args.dry_run else AwsMeasurer()

    if args.check:
        raw = meas.measure([0.0, 0.0, 0.0, 0.0])
        f, m = fitness_from_raw(raw)
        print(json.dumps(raw, indent=2, default=str))
        print(f"fitness={f:.3f}  M={m}")
        print("CHECK OK: if fanout_invocations is about 50 and agg_invocations about 150, "
              "everything is wired correctly.")
        return

    with open(os.path.join(out, "settings.json"), "w") as f:
        json.dump({k: v for k, v in globals().items()
                   if k.isupper() and isinstance(v, (int, float, str, list, dict))}
                  | {"account": meas.account_settings(),
                     "started": dt.datetime.now().isoformat()}, f, indent=2)

    path = os.path.join(out, "all_evaluations.csv")
    logger = Logger(path)
    done = completed_runs(path)

    if len(done.get(("baseline", "BASE", "0"), [])) < N_BASELINE:
        for ev in range(1, N_BASELINE + 1):
            evaluate(meas, logger, "baseline", "BASE", 0, "", ev, "", [0, 0, 0, 0], -1)

    bests = {}
    for r in range(1, N_REPLICATES + 1):
        for method, fn in (("GA", run_ga), ("RS", run_random)):
            rows = done.get((method, str(r)), [])
            if len(rows) >= EVALS_PER_RUN:
                b = max(rows, key=lambda x: float(x["fitness"]))
                bests[(method, r)] = [float(b[k]) for k in ("d1", "d2", "p1", "p2")]
                print(f"skip {method}{r} (already complete)")
                continue
            if rows:
                print(f"WARNING: {method}{r} was incomplete; rerunning it. "
                      f"Delete its partial rows from the CSV later (marked by run={r}).")
            seed = SEEDS[method][r - 1]
            bests[(method, r)], _ = fn(meas, logger, r, seed)

    for (method, r), c in bests.items():
        if len(done.get(("retest", method, str(r)), [])) >= N_RETEST:
            continue
        for ev in range(1, N_RETEST + 1):
            evaluate(meas, logger, "retest", method, r, "", ev, "", c, -1)

    print(f"\nALL DONE. Send me the folder '{out}' (all_evaluations.csv + settings.json).")


if __name__ == "__main__":
    main()
