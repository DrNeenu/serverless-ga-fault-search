# GA-based fault impact analysis in a serverless pipeline

Code and raw data for the paper *"A Genetic Algorithm-Based Approach for Fault Impact Analysis in Serverless Architectures"* (V. Arora, S. Mangat, N. Garg), submitted to *The Journal of Supercomputing*.

## Contents

| Folder | What it contains |
|---|---|
| `lambda/` | The three AWS Lambda functions (ingest, fan-out, aggregate). Fan-out and aggregate read the fault configuration (d1, d2, p1, p2) from DynamoDB with strongly consistent reads. |
| `experiments/` | Controller scripts used for the live experiments: GA vs. random search and baseline (`compare_runs.py`), scalability and heatmap (`scale_and_heatmap.py`), ablation (`ablation.py`). Deployment names go in `aws_metrics.py`. |
| `data/` | Raw per-evaluation logs from all experiments (CloudWatch metrics, evaluation windows, configurations, fitness) and the run settings (`settings.json`). |
| `analysis/` | `make_results.py` reproduces every table value and figure in Section 5 from the CSV files. |
| `figures/` | Figures produced by `make_results.py`. |

## Reproducing the tables and figures (no AWS needed)

```
cd analysis
python3 make_results.py
```

Requires Python 3.9+ and matplotlib. Numbers are printed to the terminal and figures are written to `figures/`. The only rows excluded from the analysis are two interrupted partial runs, described at the top of `make_results.py`.

## Re-running the live experiments (needs your own AWS account)

1. Deploy the pipeline: API Gateway (HTTP API) → ingest Lambda → SQS queue → fan-out Lambda → aggregate Lambda (asynchronous invocation, fan-out factor 3), plus a DynamoDB table with an item `config_id = "current"`. Runtime Python 3.9, 256 MB memory, 10 s timeout.
2. Set the Lambda environment variables: `CONFIG_TABLE` (fan-out and aggregate), `AGGREGATE_FUNCTION_NAME` and `FANOUT_FACTOR=3` (fan-out). Set the aggregate function's asynchronous `MaximumRetryAttempts` to 0.
3. Fill in your deployment names in `experiments/aws_metrics.py`.
4. Run from `experiments/` (requires `boto3` and `requests`, and AWS credentials for the account):
   ```
   python3 compare_runs.py --dry-run   # code test with simulated metrics, no AWS calls
   python3 compare_runs.py --check     # one real zero-fault evaluation
   python3 compare_runs.py             # GA, random search and baseline runs
   python3 scale_and_heatmap.py
   python3 ablation.py
   ```

The original experiments ran in us-east-1 with an account concurrency quota of 10; results depend on this quota (see Section 4 of the paper). Running the experiments incurs AWS charges.

## License

MIT License (see `LICENSE`).
