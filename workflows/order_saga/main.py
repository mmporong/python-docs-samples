# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# [START workflows_order_saga_refund]
import datetime
import os
import uuid

import flask
import functions_framework
from google.auth.transport.requests import Request
from google.cloud import firestore
from google.oauth2 import id_token
import requests

# Longer than a refund takes, so only a crashed attempt's claim expires.
LEASE = datetime.timedelta(minutes=2)
PAYMENT_SERVICE_URL = os.environ.get("PAYMENT_SERVICE_URL", "")
REFUNDABLE_STATES = ("paid", "refund_failed")

db = firestore.Client(database=os.environ.get("FIRESTORE_DATABASE", "(default)"))


@firestore.transactional
def claim_refund(
    transaction: firestore.Transaction,
    order_ref: firestore.DocumentReference,
    attempt_id: str,
    now: datetime.datetime,
) -> tuple[str, int]:
    """Claims the order's refund unless it's refunded or another attempt holds it.

    Returns the claim status and the order's amount.
    """
    snapshot = order_ref.get(transaction=transaction)
    if not snapshot.exists:
        return "missing", 0
    order = snapshot.to_dict()
    state = order["order_state"]
    if state == "refunded":
        return "refunded", order["amount_cents"]
    if state == "refund_in_progress" and order["lease_expires"] > now:
        return "in_progress", order["amount_cents"]
    if state not in REFUNDABLE_STATES and state != "refund_in_progress":
        return "not_paid", order["amount_cents"]
    transaction.update(
        order_ref,
        {
            "order_state": "refund_in_progress",
            "attempt_id": attempt_id,
            "lease_expires": now + LEASE,
        },
    )
    return "claimed", order["amount_cents"]


@firestore.transactional
def finish_refund(
    transaction: firestore.Transaction,
    order_ref: firestore.DocumentReference,
    attempt_id: str,
    order_state: str,
) -> None:
    """Records the result, if this attempt still holds the claim."""
    snapshot = order_ref.get(transaction=transaction)
    if snapshot.exists and snapshot.to_dict().get("attempt_id") == attempt_id:
        transaction.update(order_ref, {"order_state": order_state})


def refund(order_id: str, amount_cents: int) -> None:
    """Refunds the order through the payment service, at most once per order ID."""
    token = id_token.fetch_id_token(Request(), PAYMENT_SERVICE_URL)
    response = requests.post(
        PAYMENT_SERVICE_URL,
        json={"order_id": order_id, "amount_cents": -amount_cents},
        # A key that's different from the charge's key, so the payment service
        # treats the refund as its own operation, also done only once.
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": f"refund-{order_id}",
        },
        timeout=30,
    )
    response.raise_for_status()


@functions_framework.http
def refund_order(request: flask.Request) -> tuple[dict, int]:
    """Refunds a paid order once, even when the workflow retries the call."""
    order_id = (request.get_json(silent=True) or {}).get("order_id")
    if not order_id:
        return {"error": "missing order_id"}, 400

    order_ref = db.collection("orders").document(str(order_id))
    attempt_id = uuid.uuid4().hex
    now = datetime.datetime.now(datetime.timezone.utc)

    status, amount_cents = claim_refund(db.transaction(), order_ref, attempt_id, now)
    if status == "missing":
        return {"error": f"order {order_id} not found"}, 404
    if status == "not_paid":
        return {"error": f"order {order_id} wasn't paid"}, 409
    if status == "refunded":
        return {
            "order_id": order_id,
            "status": "refunded",
            "already_processed": True,
        }, 200
    if status == "in_progress":
        # The workflow's retry policy retries 503 responses after a backoff.
        return {"error": "refund in progress"}, 503

    # Refund outside the transaction: Firestore can rerun a transaction, but it
    # can't undo a call to another service.
    try:
        refund(order_id, amount_cents)
    except requests.RequestException as err:
        finish_refund(db.transaction(), order_ref, attempt_id, "refund_failed")
        return {"error": f"refund failed: {err}"}, 503
    finish_refund(db.transaction(), order_ref, attempt_id, "refunded")
    return {"order_id": order_id, "status": "refunded", "already_processed": False}, 200


# [END workflows_order_saga_refund]
