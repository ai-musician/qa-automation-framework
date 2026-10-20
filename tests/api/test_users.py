import pytest

from utils.assertions import assert_api_error, assert_status, assert_valid_user
from utils.factories import make_user

pytestmark = pytest.mark.api

def test_create_standard_user(user_api):
    payload = make_user(accountType="standard")
    res = user_api.post("/api/users", payload)
    
    assert_status(res, 201)
    standard_user = res.json()
    assert_valid_user(standard_user)
    assert {k: standard_user[k] for k in payload} == payload

def test_create_premium_user(user_api):
    payload = make_user(accountType="premium")
    res = user_api.post("/api/users", payload)
    
    assert_status(res, 201)
    premium_user = res.json()
    assert_valid_user(premium_user)
    assert {k: premium_user[k] for k in payload} == payload

def test_get_user_after_creation(user_api, fresh_user):
    res = user_api.get(f"/api/users/{fresh_user['id']}")
    assert_status(res, 200)
    assert res.json() == fresh_user

@pytest.mark.negative
def test_get_user_with_invalid_id_returns_404(user_api):
    res = user_api.get("/api/users/invalid-id")
    assert_api_error(res, "USER_NOT_FOUND", 404) 

@pytest.mark.negative
def test_create_user_with_existing_email_returns_409(user_api, fresh_user):
    payload = make_user(email=fresh_user["email"])
    res = user_api.post("/api/users", payload)
    assert_api_error(res, "EMAIL_EXISTS", 409)

@pytest.mark.negative
def test_create_user_without_name_returns_400(user_api):
    payload = make_user()
    del payload["name"]
    res = user_api.post("/api/users", payload)
    assert_api_error(res, "VALIDATION_ERROR", 400)

@pytest.mark.negative
def test_create_user_without_token_returns_401(client):
    res = client.post("/api/users", make_user(accountType="standard"))
    assert_api_error(res, "UNAUTHENTICATED", 401)

def test_admin_can_update_user_account_type(admin_api, fresh_user):
    path = f"/api/users/{fresh_user['id']}"
    new_type = "premium" if fresh_user["accountType"] == "standard" else "standard"

    res = admin_api.patch(path, {"accountType": new_type})

    assert_status(res, 200)
    assert res.json()["accountType"] == new_type      
    assert admin_api.get(path).json()["accountType"] == new_type    # really saved
