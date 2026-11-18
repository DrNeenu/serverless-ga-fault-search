import os
import json
import time
import random
import boto3

dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")

CONFIG_TABLE = os.environ["CONFIG_TABLE"]
AGGREGATE_FN = os.environ.get("AGGREGATE_FUNCTION_NAME")
FANOUT_FACTOR = int(os.environ.get("FANOUT_FACTOR", "3"))

table = dynamodb.Table(CONFIG_TABLE)


def get_chaos_config():
    try:
        resp = table.get_item(Key={"config_id": "current"}, ConsistentRead=True)
        item = resp.get("Item", {})
        return {
            "d1": float(item.get("d1", 0)),
            "d2": float(item.get("d2", 0)),
            "p1": float(item.get("p1", 0)),
            "p2": float(item.get("p2", 0)),
        }
    except Exception:
        return {"d1": 0, "d2": 0, "p1": 0, "p2": 0}


def handler(event, context):
    config = get_chaos_config()

    if config["d1"] > 0:
        time.sleep(config["d1"] / 1000.0)

    if random.random() < config["p1"]:
        raise Exception("Injected fan-out failure (chaos config p1)")

    for record in event.get("Records", []):
        for _ in range(FANOUT_FACTOR):
            lambda_client.invoke(
                FunctionName=AGGREGATE_FN,
                InvocationType="Event",
                Payload=json.dumps({"source_body": record.get("body", "{}")}),
            )

    return {"statusCode": 200}
