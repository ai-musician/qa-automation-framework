import pytest

from conftest import Client
from utils.factories import SENDER_ID, make_user, make_transaction
from utils.assertions import assert_api_error, assert_valid_transaction, assert_status
pytestmark = pytest.mark.api

def test_create_transaction(user_api: Client):
    # Arrange: the signed-in user (seed user 123) sends money to a brand-new recipient.
    # The sender must be 123: a normal user may only create their own transactions.
    recipient = user_api.post("/api/users", json=make_user()).json()
    payload = make_transaction(amount=100.50, recipientId=recipient["id"])

    res = user_api.post("/api/transactions", json=payload)

    assert_status(res, 201)
    tx = res.json()
    assert_valid_transaction(tx)
    assert {k: tx[k] for k in payload} == payload   # everything sent came back unchanged

def test_list_transactions_includes_created_one(user_api):
    created = user_api.post("/api/transactions", make_transaction())
    assert_status(created, 201)

    res = user_api.get(f"/api/transactions/{SENDER_ID}")

    assert_status(res, 200)
    body = res.json()
    assert body["count"] == len(body["transactions"])
    assert created.json() in body["transactions"]

def test_create_transaction_without_token_returns_401(client: Client):
    res = client.post("/api/transactions", make_transaction())
    assert_status(res, 401)

@pytest.mark.negative
@pytest.mark.parametrize("override, code, status", [
    ({"amount": 0}, "VALIDATION_ERROR", 400),
    ({"amount": -5}, "VALIDATION_ERROR", 400),
    ({"amount": "100"}, "VALIDATION_ERROR", 400),
    ({"type": "refund"}, "VALIDATION_ERROR", 400),
    ({"recipientId": None}, "VALIDATION_ERROR", 400),
    ({"userId": "nope"}, "USER_NOT_FOUND", 404),
    ({"recipientId": "nope"}, "RECIPIENT_NOT_FOUND", 404),
], ids=["zero-amount", "negative-amount", "string-amount", "bad-type",
        "transfer-without-recipient", "unknown-sender", "unknown-recipient"])
def test_invalid_transaction_is_rejected(admin_api, override, code, status):
    res = admin_api.post("/api/transactions", make_transaction(**override))
    assert_api_error(res, code, status)