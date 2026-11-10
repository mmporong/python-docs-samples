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

"""Integration tests that run the order saga end to end.

Skipped unless RUN_INTEGRATION_TESTS is "true". They need the saga workflow
(workflow.yaml), the refund function (main.py), the payment function from
../idempotent_payment, and the stand-in payment and fulfillment services
deployed, and:
    GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION (default 'us-central1'),
    WORKFLOW_ID (default 'order-saga-workflow'), and FIRESTORE_DATABASE.
"""

import json
import os
import time
from typing import Any, Iterator
import uuid

from google.cloud import firestore
from google.cloud.workflows import executions_v1
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "true",
    reason="Set RUN_INTEGRATION_TESTS=true to run integration tests.",
)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION_ID = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
WORKFLOW_ID = os.environ.get("WORKFLOW_ID", "order-saga-workflow")
DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")
AMOUNT_CENTS = 4999


@pytest.fixture(scope="module")
def db() -> firestore.Client:
    return firestore.Client(database=DATABASE)


@pytest.fixture
def order_id(db: firestore.Client) -> Iterator[str]:
    """Provides a unique order ID and deletes its test records afterward."""
    order_id = f"test-order-{uuid.uuid4().hex[:12]}"
    yield order_id
    db.collection("orders").document(order_id).delete()
    db.collection("test_fulfillment_faults").document(order_id).delete()
    for key in (order_id, f"refund-{order_id}"):
        db.collection("test_payment_charges").document(key).delete()
        db.collection("test_payment_faults").document(key).delete()


def run_saga(order_id: str) -> executions_v1.Execution:
    """Runs the saga for an order and waits for the execution to finish."""
    client = executions_v1.ExecutionsClient()
    parent = f"projects/{PROJECT_ID}/locations/{LOCATION_ID}/workflows/{WORKFLOW_ID}"
    argument = json.dumps({"order_id": order_id, "amount_cents": AMOUNT_CENTS})
    execution = client.create_execution(
        parent=parent, execution=executions_v1.Execution(argument=argument)
    )
    deadline = time.monotonic() + 600
    while execution.state == executions_v1.Execution.State.ACTIVE:
        assert time.monotonic() < deadline, "The execution didn't finish."
        time.sleep(3)
        execution = client.get_execution(name=execution.name)
    return execution


def ledger(db: firestore.Client, key: str) -> dict[str, Any] | None:
    """Returns the stand-in payment service's record for a key, if any."""
    snapshot = db.collection("test_payment_charges").document(key).get()
    return snapshot.to_dict() if snapshot.exists else None


def order_state(db: firestore.Client, order_id: str) -> str:
    return db.collection("orders").document(order_id).get().to_dict()["order_state"]


def test_fulfilled_order_is_not_refunded(db: firestore.Client, order_id: str) -> None:
    execution = run_saga(order_id)

    assert json.loads(execution.result)["status"] == "fulfilled"
    assert ledger(db, order_id)["charges"] == 1
    assert ledger(db, f"refund-{order_id}") is None


def test_failed_fulfillment_refunds_once(db: firestore.Client, order_id: str) -> None:
    db.collection("test_fulfillment_faults").document(order_id).set({"mode": "fail"})

    execution = run_saga(order_id)

    assert json.loads(execution.result)["status"] == "refunded"
    assert ledger(db, order_id)["charges"] == 1
    refund = ledger(db, f"refund-{order_id}")
    assert refund["charges"] == 1
    assert refund["amount_cents"] == -AMOUNT_CENTS
    assert order_state(db, order_id) == "refunded"


def test_retried_compensation_refunds_once(db: firestore.Client, order_id: str) -> None:
    db.collection("test_fulfillment_faults").document(order_id).set({"mode": "fail"})
    # The payment service refunds, then responds with a gateway timeout.
    db.collection("test_payment_faults").document(f"refund-{order_id}").set(
        {"mode": "fail_after_charge"}
    )

    execution = run_saga(order_id)

    assert json.loads(execution.result)["status"] == "refunded"
    refund = ledger(db, f"refund-{order_id}")
    assert refund["charges"] == 1
    assert refund["requests"] >= 2


def test_retrying_a_refunded_order_moves_no_money(
    db: firestore.Client, order_id: str
) -> None:
    db.collection("test_fulfillment_faults").document(order_id).set({"mode": "fail"})
    run_saga(order_id)

    # The client retries the same order after it was refunded. The payment
    # function refuses to charge an order in a final state.
    execution = run_saga(order_id)

    assert execution.state == executions_v1.Execution.State.FAILED
    assert ledger(db, order_id)["charges"] == 1
    assert ledger(db, f"refund-{order_id}")["charges"] == 1
    assert order_state(db, order_id) == "refunded"
