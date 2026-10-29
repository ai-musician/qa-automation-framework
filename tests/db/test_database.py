"""
Checks below the API: what it actually stored in its database.

An API test sees only the response, and a response can look right while the data behind it is wrong: an amount
rounded on the way in, a delete that leaves rows behind, a duplicate that slipped past a check. These tests call the
API, then read the tables directly (the `db` fixture, conftest.py).

They run where the API uses Postgres (docker-compose.yml), and skip themselves everywhere else.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
import threading
import uuid

import pytest

from conftest import API_KEY, Client
from utils.assertions import assert_api_error, assert_status
from utils.factories import make_deposit, make_user

pytestmark = pytest.mark.db


def test_created_user_is_stored(db, fresh_user):
    row = db.execute("SELECT * FROM users WHERE id = %s", (fresh_user["id"],)).fetchone()

    assert row is not None, f"user {fresh_user['id']} got a 201 but isn't in the users table"
    assert (row["name"], row["email"], row["account_type"]) == \
        (fresh_user["name"], fresh_user["email"], fresh_user["accountType"])
    assert row["created_at"] == datetime.fromisoformat(fresh_user["createdAt"])


@pytest.mark.parametrize("amount", [0.01, 0.1, 19.99, 1234.56, 999_999.99])
def test_amount_is_stored_exactly(db, user_api, amount):
    # Money must not drift: 0.1 has no exact float, so a float column would store 0.1000000000000000055...
    res = user_api.post("/api/transactions", make_deposit() | {"amount": amount})
    assert_status(res, 201)

    row = db.execute("SELECT amount FROM transactions WHERE id = %s", (res.json()["id"],)).fetchone()
    assert row["amount"] == Decimal(str(amount)), f"sent {amount}, stored {row['amount']}"


def test_deleting_a_user_deletes_their_transactions(db, admin_api, fresh_user):
    for _ in range(3):
        assert_status(admin_api.post("/api/transactions", make_deposit(userId=fresh_user["id"])), 201)

    assert_status(admin_api.delete(f"/api/users/{fresh_user['id']}"), 204)

    left = db.execute("SELECT count(*) AS n FROM transactions WHERE user_id = %s", (fresh_user["id"],)).fetchone()
    assert left["n"] == 0, f"{left['n']} transactions of the deleted user are still in the database"


@pytest.mark.negative
def test_rejected_request_stores_nothing(db, user_api):
    payload = make_user(accountType="gold")             # invalid: 400
    assert_api_error(user_api.post("/api/users", payload), "VALIDATION_ERROR", 400)

    row = db.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (payload["email"],)).fetchone()
    assert row is None, "a request that got a 400 still created a user"


def test_user_added_in_the_database_is_served_by_the_api(db, user_api, admin_api):
    # Test data set up straight in the database: quicker than through the API, and it can create states the API
    # wouldn't (here: a user with no API calls behind it at all)
    user_id, email = str(uuid.uuid4()), f"seeded-{uuid.uuid4().hex[:8]}@example.com"
    db.execute("INSERT INTO users (id, name, email, account_type, created_at)"
               " VALUES (%s, 'Seeded In Db', %s, 'standard', '2026-10-01T09:00:00Z')", (user_id, email))
    try:
        res = user_api.get(f"/api/users/{user_id}")
        assert_status(res, 200)
        assert res.json() == {"id": user_id, "name": "Seeded In Db", "email": email, "accountType": "standard",
                              "createdAt": "2026-10-01T09:00:00.000Z"}
    finally:
        admin_api.delete(f"/api/users/{user_id}")


@pytest.mark.negative
def test_same_email_at_the_same_moment_creates_one_user(db, admin_api):
    # A check-then-insert in the code ("is this email taken? no -> insert") lets two requests both pass the check.
    # Only the database's unique index stops that, so send 10 sign-ups with one email at the same moment.
    payload, attempts = make_user(), 10
    clients = [Client().login(API_KEY) for _ in range(attempts)]     # one connection each: really in parallel
    start = threading.Barrier(attempts)

    def sign_up(client):
        start.wait()                                    # all threads send together
        return client.post("/api/users", payload)

    with ThreadPoolExecutor(attempts) as pool:
        responses = list(pool.map(sign_up, clients))

    rows = db.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (payload["email"],)).fetchall()
    for row in rows:
        admin_api.delete(f"/api/users/{row['id']}")

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201] + [409] * (attempts - 1), f"expected one 201 and {attempts - 1} 409s, got {statuses}"
    assert len(rows) == 1, f"{len(rows)} users were stored with the same email"
