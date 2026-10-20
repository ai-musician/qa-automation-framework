"""Domain-level assertion helpers with readable failure messages."""
import requests


def assert_status(res: requests.Response, expected: int):
    assert res.status_code == expected, f"Expected HTTP {expected}, got {res.status_code}\nBody: {res.text}"


def assert_api_error(res: requests.Response, code: str, status: int):
    assert_status(res, status)
    err = res.json().get("error") or {}
    assert err.get("code") == code, f"Expected error code {code}, got {err.get('code')}\nBody: {res.text}"
    assert err.get("message"), "error.message is missing"
    return err


def assert_expected_error(res: requests.Response, expect: dict):
    """Checks status and error code; for validation errors, also that exactly the expected fields are blamed."""
    err = assert_api_error(res, expect["code"], expect["status"])
    if "fields" in expect:
        blamed = set(err.get("details", {}))
        assert blamed == set(expect["fields"]), f"Expected errors for {sorted(expect['fields'])}, got {sorted(blamed)}\nBody: {res.text}"
    return err


def assert_valid_user(user: dict):
    assert user.get("id"), "missing id"
    assert user.get("name"), "missing name"
    assert "@" in (user.get("email") or ""), "email is not an email"
    assert user.get("accountType") in ("standard", "premium"), f"bad accountType {user.get('accountType')}"
    #assert user.get("createdAt", "").endswith("Z"), "createdAt is not an ISO-8601 UTC timestamp"


def assert_valid_transaction(tx: dict):
    assert tx.get("id"), "missing id"
    assert tx.get("userId"), "missing userId"
    assert isinstance(tx.get("amount"), (int, float)) and tx["amount"] > 0, "amount not positive"
    assert tx.get("type") in ("transfer", "deposit", "withdrawal"), f"bad type {tx.get('type')}"
    if tx["type"] == "transfer":
        assert tx.get("recipientId"), "transfer missing recipientId"
    assert tx.get("status") == "completed", f"status is {tx.get('status')}"
