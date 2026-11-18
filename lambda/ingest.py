import os
import json
import uuid
import boto3

sqs = boto3.client("sqs")
QUEUE_URL = os.environ["QUEUE_URL"]


def handler(event, context):
    """Entry point (Section 3.4: 'ingestion Lambda function')."""
    message = {
        "request_id": str(uuid.uuid4()),
        "body": event.get("body", "{}"),
    }
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(message))
    return {
        "statusCode": 202,
        "body": json.dumps({"accepted": True, "request_id": message["request_id"]}),
    }
