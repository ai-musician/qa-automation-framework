"""
Data-driven negative tests. The cases live in data/negative/*.json; each test below runs once per case.
To add a case, add an entry to the JSON file. No code change needed.

    pytest -m negative                       all negative tests
    pytest -k "email-double-at"              one case by name
"""
import pytest

from utils.assertions import assert_api_error, assert_expected_error
from utils.factories import make_transaction, make_user
from utils.test_data import build_body, load_cases

pytestmark = [pytest.mark.api, pytest.mark.negative]


@pytest.mark.parametrize("case", load_cases("users.json"))
def test_create_user_rejects(user_api, case):
    res = user_api.post("/api/users", build_body(case, make_user()))
    assert_expected_error(res, case["expect"])


@pytest.mark.parametrize("case", load_cases("user_updates.json"))
def test_update_user_rejects(admin_api, fresh_user, case):
    path = f"/api/users/{fresh_user['id']}"

    res = admin_api.patch(path, build_body(case, {}))

    assert_expected_error(res, case["expect"])
    assert admin_api.get(path).json() == fresh_user        # a rejected update changes nothing


@pytest.mark.parametrize("case", load_cases("transactions.json"))
def test_create_transaction_rejects(user_api, case):
    if "raw_body" in case:
        res = user_api.post_raw("/api/transactions", case["raw_body"])
    else:
        res = user_api.post("/api/transactions", build_body(case, make_transaction()))
    assert_expected_error(res, case["expect"])


@pytest.mark.parametrize("case", load_cases("api_keys.json"))
def test_token_request_rejects(client, case):
    res = client.post("/api/auth/token", case["body"])
    assert_api_error(res, "INVALID_API_KEY", 401)
    assert "token" not in res.json()


@pytest.mark.parametrize("case", load_cases("auth_headers.json"))
def test_protected_route_rejects_auth_header(client, user_api, case):
    token = user_api.session.headers["Authorization"].removeprefix("Bearer ")   # a real, valid token
    header = case["authorization"]
    if header is not None:
        header = header.replace("{token_truncated}", token[:-1]).replace("{token}", token)

    res = client.get("/api/users/123", headers={"Authorization": header} if header else None)

    assert_expected_error(res, case["expect"])
