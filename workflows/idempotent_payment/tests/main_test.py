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
ORDER_ID = "order-1001"
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
def charge(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    charge_mock = mock.MagicMock()
    monkeypatch.setattr(main, "charge", charge_mock)
    return charge_mock


def create_order(db: FakeDatabase, **fields: object) -> dict[str, Any]:
    db.store[ORDER_ID] = {"order_state": "created", "amount_cents": 1999, **fields}
    return db.store[ORDER_ID]


def call(body: dict[str, Any] | None = None) -> tuple[dict, int]:
    body = {"order_id": ORDER_ID} if body is None else body
    with app.test_request_context("/", method="POST", json=body):
        return main.process_payment(flask.request)


def test_new_order_is_charged_once(db: FakeDatabase, charge: mock.MagicMock) -> None:
    order = create_order(db)

    body, status = call()

    assert status == 200
    assert body["already_processed"] is False
    charge.assert_called_once_with(ORDER_ID, 1999)
    assert order["order_state"] == "paid"


def test_paid_order_is_not_charged_again(
    db: FakeDatabase, charge: mock.MagicMock
) -> None:
    create_order(db, order_state="paid")

    body, status = call()

    assert (status, body["already_processed"]) == (200, True)
    charge.assert_not_called()


def test_refunded_order_is_not_charged_again(
    db: FakeDatabase, charge: mock.MagicMock
) -> None:
    order = create_order(db, order_state="refunded")

    _, status = call()

    assert status == 409
    charge.assert_not_called()
    assert order["order_state"] == "refunded"


def test_order_with_active_claim_returns_503(
    db: FakeDatabase, charge: mock.MagicMock
) -> None:
    create_order(
        db,
        order_state="payment_in_progress",
        attempt_id="other-attempt",
        lease_expires=NOW + main.LEASE,
    )

    _, status = call()

    assert status == 503
    charge.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {
            "order_state": "payment_in_progress",
            "attempt_id": "crashed-attempt",
            "lease_expires": NOW - datetime.timedelta(seconds=1),
        },
        {"order_state": "payment_failed"},
    ],
    ids=["expired_claim", "failed_payment"],
)
def test_unfinished_order_is_charged(
    db: FakeDatabase, charge: mock.MagicMock, fields: dict[str, Any]
) -> None:
    order = create_order(db, **fields)

    _, status = call()

    assert status == 200
    charge.assert_called_once()
    assert order["order_state"] == "paid"


def test_ambiguous_payment_failure_is_retryable(
    db: FakeDatabase, charge: mock.MagicMock
) -> None:
    order = create_order(db)
    charge.side_effect = requests.Timeout("timed out")

    _, status = call()

    assert status == 503
    assert order["order_state"] == "payment_failed"


def test_stale_attempt_does_not_overwrite_newer_claim(
    db: FakeDatabase, charge: mock.MagicMock
) -> None:
    order = create_order(db)
    # While this attempt charges, its lease expires and a retry claims the order.
    charge.side_effect = lambda *args: order.update(attempt_id="newer-attempt")

    call()

    assert order["order_state"] == "payment_in_progress"
    assert order["attempt_id"] == "newer-attempt"


@pytest.mark.parametrize(
    "body, expected_status",
    [({}, 400), ({"order_id": "missing-order"}, 404)],
)
def test_bad_requests(
    db: FakeDatabase,
    charge: mock.MagicMock,
    body: dict[str, Any],
    expected_status: int,
) -> None:
    _, status = call(body)

    assert status == expected_status
    charge.assert_not_called()


def test_charge_sends_order_id_as_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "PAYMENT_SERVICE_URL", "https://payments.example.com")
    monkeypatch.setattr(main.id_token, "fetch_id_token", lambda *args: "token")
    post = mock.MagicMock()
    monkeypatch.setattr(main.requests, "post", post)

    main.charge(ORDER_ID, 1999)

    kwargs = post.call_args.kwargs
    assert post.call_args.args == ("https://payments.example.com",)
    assert kwargs["headers"]["Idempotency-Key"] == ORDER_ID
    assert kwargs["headers"]["Authorization"] == "Bearer token"
    assert kwargs["json"] == {"order_id": ORDER_ID, "amount_cents": 1999}
