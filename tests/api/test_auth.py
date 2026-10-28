import pytest
from utils.assertions import assert_api_error, assert_status
from utils.factories import SENDER_ID, make_deposit
import config

API_KEY = config.env.api_key
ADMIN_API_KEY = config.env.admin_api_key

pytestmark = [pytest.mark.api, pytest.mark.auth]

# ---------- authentication ----------
def test_valid_api_key_return_token(client):
    res = client.post("/api/auth/token", json={"apiKey": API_KEY})
    assert_status(res, 200)
    assert res.json()["token"]
    assert res.json()["role"] == "user"

@pytest.mark.negative
def test_invalid_api_key_return_401(client):
    res = client.post("/api/auth/token", {"apiKey": "wrong"})
    assert_api_error(res, "INVALID_API_KEY", 401)

@pytest.mark.negative
def test_missing_api_key_return_401(client): 
    res = client.post("/api/auth/token", json={})
    assert "token" not in res.json()
    assert_api_error(res, "INVALID_API_KEY", 401)

# every route except POST /api/auth/token needs a token
PROTECTED_ROUTES = [
    ("post", "/api/users"),
    ("get", "/api/users"),
    ("get", f"/api/users/{SENDER_ID}"),
    ("patch", f"/api/users/{SENDER_ID}"),
    ("delete", f"/api/users/{SENDER_ID}"),
    ("post", f"/api/users/{SENDER_ID}/api-keys"),
    ("post", "/api/transactions"),
    ("get", f"/api/transactions/{SENDER_ID}"),
    ("post", "/v1/chat/completions"),
]

@pytest.mark.negative
@pytest.mark.parametrize("method, path", PROTECTED_ROUTES, ids=[f"{m.upper()} {p}" for m, p in PROTECTED_ROUTES])
def test_protected_routes_reject_missing_token(client, method, path):
    res = getattr(client, method)(path)
    assert_api_error(res, "UNAUTHENTICATED", 401)

@pytest.mark.negative
def test_bogus_token_returns_401(client):
    res = client.get(f"/api/users/{SENDER_ID}", headers={"Authorization": "Bearer nope"})
    assert_api_error(res, "INVALID_TOKEN", 401)

# ---------- authorization (roles) ----------
def test_user_can_read_own_transaction(user_api):
    assert_status(user_api.get(f"/api/transactions/{SENDER_ID}"), 200)

@pytest.mark.negative
def test_user_cannot_read_other_users_transactions(user_api, fresh_user):
    assert_api_error(user_api.get(f"/api/transactions/{fresh_user['id']}"), "FORBIDDEN", 403)

@pytest.mark.negative
def test_user_cannot_create_transaction_for_other_user(user_api, fresh_user):
    res = user_api.post("/api/transactions", make_deposit(userId=fresh_user["id"]))
    assert_api_error(res, "FORBIDDEN", 403)

def test_admin_can_read_any_users_transactions(admin_api, fresh_user):
    created = admin_api.post("/api/transactions", make_deposit(userId=fresh_user["id"]))
    assert_status(created, 201)

    res = admin_api.get(f"/api/transactions/{fresh_user['id']}")
    assert_status(res, 200)
    body = res.json()
    assert body["count"] == len(body["transactions"]) == 1
    assert created.json() in body["transactions"]
    assert all(tx["userId"] == fresh_user["id"] for tx in body["transactions"])   # nobody else's

@pytest.mark.negative
def test_normal_user_cannot_list_users(user_api, admin_api):
    assert_api_error(user_api.get("/api/users"), "FORBIDDEN", 403)
    assert_status(admin_api.get("/api/users"), 200)

def test_only_admin_can_delete_user(user_api, admin_api, fresh_user):
    path = f"/api/users/{fresh_user['id']}"
    assert_api_error(user_api.delete(path), "FORBIDDEN", 403)
    assert_status(admin_api.delete(path), 204)
    assert_api_error(admin_api.get(path), "USER_NOT_FOUND", 404)   # really gone

@pytest.mark.negative
def test_user_cannot_update_other_user(user_api, fresh_user):
    res = user_api.patch(f"/api/users/{fresh_user['id']}", {"accountType": "premium"})
    assert_api_error(res, "FORBIDDEN", 403)
