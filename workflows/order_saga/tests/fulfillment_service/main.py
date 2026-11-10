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

"""A stand-in fulfillment service for testing, not a published sample.

It fulfills every order unless a fault document for the order ID says to fail,
in which case it responds with HTTP 422, a permanent failure.
"""

import os

import flask
import functions_framework
from google.cloud import firestore

db = firestore.Client(database=os.environ.get("FIRESTORE_DATABASE", "(default)"))


@functions_framework.http
def fulfill(request: flask.Request) -> tuple[dict, int]:
    order_id = str((request.get_json(silent=True) or {}).get("order_id", ""))
    if not order_id:
        return {"error": "missing order_id"}, 400
    if db.collection("test_fulfillment_faults").document(order_id).get().exists:
        return {"error": "out of stock"}, 422
    return {"order_id": order_id, "status": "fulfilled"}, 200
