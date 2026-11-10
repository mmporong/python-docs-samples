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

"""A stand-in payment service for testing, not a published sample.

It charges each Idempotency-Key once and records every request in Firestore,
so tests can count charges. A fault document for a key makes the next request
charge and then respond ambiguously (HTTP 504), or charge and respond slowly.
"""

import os
import time
import uuid

import flask
import functions_framework
from google.cloud import firestore

db = firestore.Client(database=os.environ.get("FIRESTORE_DATABASE", "(default)"))


@firestore.transactional
def record_charge(
    transaction: firestore.Transaction,
    charge_ref: firestore.DocumentReference,
    order_id: str,
    amount_cents: int,
) -> bool:
    """Records one charge per key. Returns True if this request charged."""
    snapshot = charge_ref.get(transaction=transaction)
    if snapshot.exists:
        transaction.update(charge_ref, {"requests": firestore.Increment(1)})
        return False
    transaction.set(
        charge_ref,
        {
            "charge_id": uuid.uuid4().hex,
            "order_id": order_id,
            "amount_cents": amount_cents,
            "charges": 1,
            "requests": 1,
        },
    )
    return True


@functions_framework.http
def charge(request: flask.Request) -> tuple[dict, int]:
    key = request.headers.get("Idempotency-Key")
    body = request.get_json(silent=True) or {}
    if not key:
        return {"error": "missing Idempotency-Key"}, 400

    charge_ref = db.collection("test_payment_charges").document(key)
    record_charge(
        db.transaction(), charge_ref, body.get("order_id"), body.get("amount_cents")
    )

    fault_ref = db.collection("test_payment_faults").document(key)
    fault = fault_ref.get()
    if fault.exists:
        fault_ref.delete()  # One-shot: the next request behaves normally.
        mode = fault.to_dict().get("mode")
        if mode == "fail_after_charge":
            return {"error": "gateway timeout"}, 504
        if mode == "slow_after_charge":
            time.sleep(fault.to_dict().get("seconds", 40))
    return {"charge_id": charge_ref.get().to_dict()["charge_id"]}, 200
