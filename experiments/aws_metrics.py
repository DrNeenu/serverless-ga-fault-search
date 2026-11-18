"""
Deployment settings read by compare_runs.py, scale_and_heatmap.py and ablation.py.
Fill in the values for your own deployment (see README.md).
"""

CONFIG = {
    "API_URL": "https://<api-id>.execute-api.us-east-1.amazonaws.com/ingest",
    "CONFIG_TABLE": "<ChaosConfigTable name>",
    "FANOUT_FUNCTION_NAME": "<FanoutFunction name>",
    "AGGREGATE_FUNCTION_NAME": "<AggregateFunction name>",
    "REGION": "us-east-1",
    # Optional: set if more than one queue name starts with "cascading-failure-research"
    # "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/<account-id>/<queue-name>",
}
