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

import datetime
from typing import Any
from unittest import mock

import flask
import pytest
import requests

from .. import main

app = flask.Flask(__name__)
ORDER_ID = "order-2001"
NOW = datetime.datetime.now(datetime.timezone.utc)


class FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data or {})


class FakeDocument:
    def __init__(self, store: dict[str, dict[str, Any]], doc_id: str) -> None:
        self.store = store
        self.id = doc_id

    def get(self, transaction: object = None) -> FakeSnapshot:
        return FakeSnapshot(self.store.get(self.id))


class FakeDatabase:
    """An in-memory stand-in for the orders collection."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> mock.MagicMock:
        collection = mock.MagicMock()
        collection.document.side_effect = lambda doc_id: FakeDocument(
            self.store, doc_id
        )
        return collection

    def transaction(self) -> mock.MagicMock:
        # firestore.transactional begins, commits, and retries the transaction.
        transaction = mock.MagicMock(_max_attempts=1, _id=b"test", _read_only=False)
        transaction.update.side_effect = lambda ref, data: self.store[ref.id].update(
            data
        )
        return transaction


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    database = FakeDatabase()
    monkeypatch.setattr(main, "db", database)
    return database


@pytest.fixture
def refund(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    refund_mock = mock.MagicMock()
    monkeypatch.setattr(main, "refund", refund_mock)
    return refund_mock


def create_order(db: FakeDatabase, **fields: object) -> dict[str, Any]:
    db.store[ORDER_ID] = {"order_state": "paid", "amount_cents": 4999, **fields}
    return db.store[ORDER_ID]


def call(body: dict[str, Any] | None = None) -> tuple[dict, int]:
    body = {"order_id": ORDER_ID} if body is None else body
    with app.test_request_context("/", method="POST", json=body):
        return main.refund_order(flask.request)


def test_paid_order_is_refunded_once(db: FakeDatabase, refund: mock.MagicMock) -> None:
    order = create_order(db)

    body, status = call()

    assert (status, body["already_processed"]) == (200, False)
    refund.assert_called_once_with(ORDER_ID, 4999)
    assert order["order_state"] == "refunded"


def test_refunded_order_is_not_refunded_again(
    db: FakeDatabase, refund: mock.MagicMock
) -> None:
    create_order(db, order_state="refunded")

    body, status = call()

    assert (status, body["already_processed"]) == (200, True)
    refund.assert_not_called()


def test_order_with_active_claim_returns_503(
    db: FakeDatabase, refund: mock.MagicMock
) -> None:
    create_order(
        db,
        order_state="refund_in_progress",
        attempt_id="other-attempt",
        lease_expires=NOW + main.LEASE,
    )

    _, status = call()

    assert status == 503
    refund.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {
            "order_state": "refund_in_progress",
            "attempt_id": "crashed-attempt",
            "lease_expires": NOW - datetime.timedelta(seconds=1),
        },
        {"order_state": "refund_failed"},
    ],
    ids=["expired_claim", "failed_refund"],
)
def test_unfinished_refund_is_retried(
    db: FakeDatabase, refund: mock.MagicMock, fields: dict[str, Any]
) -> None:
    order = create_order(db, **fields)

    _, status = call()

    assert status == 200
    refund.assert_called_once()
    assert order["order_state"] == "refunded"


def test_unpaid_order_is_not_refunded(db: FakeDatabase, refund: mock.MagicMock) -> None:
    create_order(db, order_state="created")

    _, status = call()

    assert status == 409
    refund.assert_not_called()


def test_ambiguous_refund_failure_is_retryable(
    db: FakeDatabase, refund: mock.MagicMock
) -> None:
    order = create_order(db)
    refund.side_effect = requests.Timeout("timed out")

    _, status = call()

    assert status == 503
    assert order["order_state"] == "refund_failed"


def test_stale_attempt_does_not_overwrite_newer_claim(
    db: FakeDatabase, refund: mock.MagicMock
) -> None:
    order = create_order(db)
    refund.side_effect = lambda *args: order.update(attempt_id="newer-attempt")

    call()

    assert order["order_state"] == "refund_in_progress"


def test_refund_uses_its_own_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "PAYMENT_SERVICE_URL", "https://payments.example.com")
    monkeypatch.setattr(main.id_token, "fetch_id_token", lambda *args: "token")
    post = mock.MagicMock()
    monkeypatch.setattr(main.requests, "post", post)

    main.refund(ORDER_ID, 4999)

    kwargs = post.call_args.kwargs
    assert kwargs["headers"]["Idempotency-Key"] == f"refund-{ORDER_ID}"
    assert kwargs["json"] == {"order_id": ORDER_ID, "amount_cents": -4999}
