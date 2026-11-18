import os
import time
import random
import boto3

dynamodb = boto3.resource("dynamodb")
CONFIG_TABLE = os.environ["CONFIG_TABLE"]
table = dynamodb.Table(CONFIG_TABLE)


def get_chaos_config():
    try:
        resp = table.get_item(Key={"config_id": "current"}, ConsistentRead=True)
        item = resp.get("Item", {})
        return {
            "d2": float(item.get("d2", 0)),
            "p2": float(item.get("p2", 0)),
        }
    except Exception:
        return {"d2": 0, "p2": 0}


def handler(event, context):
    config = get_chaos_config()

    if config["d2"] > 0:
        time.sleep(config["d2"] / 1000.0)

    if random.random() < config["p2"]:
        raise Exception("Injected aggregation failure (chaos config p2)")

    return {"statusCode": 200}
