"""
The fake fintech API (plus a tiny web UI) that the tests run against.

    POST   /api/auth/token              API key -> bearer token
    POST   /api/users                   create a user            -> 201
    GET    /api/users                   list users (admin only)  -> 200 / 403
    GET    /api/users/<id>              get a user               -> 200 / 404
    PATCH  /api/users/<id>              update own user (admin: any) -> 200 / 400 / 403 / 404
    DELETE /api/users/<id>              delete a user (admin)    -> 204 / 403 / 404
    POST   /api/users/<id>/api-keys     issue an API key for that user (admin) -> 201 / 403 / 404
    POST   /api/transactions            create a transaction     -> 201 / 400 / 403 / 404
    GET    /api/transactions/<userId>   a user's transactions    -> 200 / 403 / 404
    GET    /ui                          mock frontend (registration + transactions)

Data lives in Python dicts (stand-ins for MongoDB / Redis), so it disappears when the server stops.

Run it by hand:  python -m mock_server.app
"""

from datetime import datetime, timezone
from functools import wraps
import math
import os
import re
import uuid

from flask import Flask, g, jsonify, request

app = Flask(__name__)

# local "databases"
users: dict[str, dict] = {}          # user id -> user
transactions: dict[str, dict] = {}   # transaction id -> transaction
sessions: dict[str, dict] = {}       # token -> {role, userId} (would be Redis in the real system)

# Each API key identifies a caller: a role, and (for normal users) which user they are.
# In the real system every customer would have their own key; the mock starts with one of each role,
# and an admin can issue more with POST /api/users/<id>/api-keys (the load tests log in as many users).
API_KEYS = {
    "user-api-key": {"role": "user", "userId": "123"},    # seed user 123, John Doe: a premium customer, but role user, not admin
    "admin-api-key": {"role": "admin", "userId": None},   # back office: may act for anyone
}
ACCOUNT_TYPES = ("standard", "premium")
MAX_NAME_LENGTH = 100
MAX_AMOUNT = 1_000_000            # per transaction
# one @, no spaces, a non-empty local part, and a domain ending in a dot + 2 or more letters
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
TRANSACTION_TYPES = ("transfer", "deposit", "withdrawal")

# The two users from the assessment's example payloads
SEED_USERS = [
    {"id": "123", "name": "John Doe", "email": "john@example.com", "accountType": "premium", "createdAt": "2026-10-17T12:00:00.000Z"},
    {"id": "456", "name": "Swim Runner", "email": "runner@example.com", "accountType": "standard", "createdAt": "2026-10-16T12:00:00.000Z"},
]
users.update({user["id"]: dict(user) for user in SEED_USERS})


def now() -> str:
    """ISO-8601 UTC with milliseconds, e.g. 2026-10-19T08:15:00.123Z"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fail(status, code, message, details=None):
    body = {"code": code, "message": message}
    if details:
        body["details"] = details
    return jsonify({"error": body}), status


def json_object():
    """The request body as a dict. No body -> {}. Valid JSON that isn't an object (a list, a string) -> None."""
    body = request.get_json(silent=True)
    if body is None:
        return {}
    return body if isinstance(body, dict) else None


NOT_AN_OBJECT = {"body": "request body must be a JSON object"}


def request_auth(fn):
    """Every /api route except /api/auth/token needs 'Authorization: Bearer <token>'."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return fail(401, "UNAUTHENTICATED", "Missing bearer token")
        token = auth_header.removeprefix("Bearer ")
        session = sessions.get(token)
        if session is None:
            return fail(401, "INVALID_TOKEN", "Token is invalid or expired")
        g.role, g.user_id = session["role"], session["userId"]
        return fn(*args, **kwargs)
    return wrapper


def admin_only(fn):
    """Use below @request_auth: a valid token that is not an admin's gets 403."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if g.role != "admin":
            return fail(403, "FORBIDDEN", "Admin role required")
        return fn(*args, **kwargs)
    return wrapper


def owns(user_id) -> bool:
    """Admins act for anyone; a normal user only for themselves."""
    return g.role == "admin" or g.user_id == user_id


def email_taken(email, except_id=None):
    return any(u["email"].lower() == email.lower() and u["id"] != except_id for u in users.values())


# ---------- auth ----------
@app.post("/api/auth/token")
def issue_token():
    api_key = (json_object() or {}).get("apiKey")
    caller = API_KEYS.get(api_key) if isinstance(api_key, str) else None
    if not caller:
        return fail(401, "INVALID_API_KEY", "Invalid API key")
    token = str(uuid.uuid4())
    sessions[token] = caller
    return jsonify({"token": token, "role": caller["role"]}), 200


# ---------- users ----------
def validate_user(body, partial=False):
    """Returns {field: problem}. partial=True (PATCH) only checks the fields that were sent."""
    details = {}
    if not partial or "name" in body:
        name = body.get("name")
        if not isinstance(name, str) or len(name.strip()) < 2:
            details["name"] = "name must be at least 2 characters"
        elif len(name.strip()) > MAX_NAME_LENGTH:
            details["name"] = f"name must be at most {MAX_NAME_LENGTH} characters"
        elif CONTROL_CHARS.search(name):
            details["name"] = "name must not contain line breaks or control characters"
    if not partial or "email" in body:
        email = body.get("email")
        if (not isinstance(email, str) or not EMAIL_PATTERN.match(email)
                or len(email.split("@")[0]) > 64 or len(email) > 254):      # RFC 5321 length limits
            details["email"] = "email must be a valid email address"
    if not partial or "accountType" in body:
        if body.get("accountType") not in ACCOUNT_TYPES:
            details["accountType"] = 'accountType must be "standard" or "premium"'
    return details


@app.post("/api/users")
@request_auth
def create_user():
    body = json_object()
    if body is None:
        return fail(400, "VALIDATION_ERROR", "Request validation failed", NOT_AN_OBJECT)
    details = validate_user(body)
    if details:
        return fail(400, "VALIDATION_ERROR", "Request validation failed", details)
    if email_taken(body["email"]):                               # case-insensitive
        return fail(409, "EMAIL_EXISTS", "A user with this email already exists")

    user = {"id": str(uuid.uuid4()), "name": body["name"].strip(), "email": body["email"],
            "accountType": body["accountType"], "createdAt": now()}
    users[user["id"]] = user
    return jsonify(user), 201


@app.get("/api/users")
@request_auth
@admin_only
def list_users():
    return jsonify(list(users.values())), 200


@app.get("/api/users/<user_id>")
@request_auth
def get_user(user_id):
    user = users.get(user_id)
    if not user:
        return fail(404, "USER_NOT_FOUND", f"User {user_id} not found")
    return jsonify(user)


@app.patch("/api/users/<user_id>")
@request_auth
def update_user(user_id):
    if not owns(user_id):
        return fail(403, "FORBIDDEN", "You can only update your own account")
    user = users.get(user_id)
    if not user:
        return fail(404, "USER_NOT_FOUND", f"User {user_id} not found")
    body = json_object()
    if body is None:
        return fail(400, "VALIDATION_ERROR", "Request validation failed", NOT_AN_OBJECT)
    changes ={k: body[k] for k in ("name", "email", "accountType") if k in body}
    if not changes:
        return fail(400, "VALIDATION_ERROR", "Nothing to update", {"body": "send name, email or accountType"})
    details = validate_user(changes, partial=True)
    if details:
        return fail(400, "VALIDATION_ERROR", "Request validation failed", details)
    if "email" in changes and email_taken(changes["email"], except_id=user_id):
        return fail(409, "EMAIL_EXISTS", "A user with this email already exists")
    if "name" in changes:
        changes["name"] = changes["name"].strip()
    user.update(changes)
    return jsonify(user), 200


@app.delete("/api/users/<user_id>")
@request_auth
@admin_only
def delete_user(user_id):
    if users.pop(user_id, None) is None:
        return fail(404, "USER_NOT_FOUND", f"User {user_id} not found")
    # a deleted user's keys stop working
    for key in [k for k, caller in API_KEYS.items() if caller["userId"] == user_id]:
        del API_KEYS[key]
    return "", 204


@app.post("/api/users/<user_id>/api-keys")
@request_auth
@admin_only
def issue_api_key(user_id):
    if user_id not in users:
        return fail(404, "USER_NOT_FOUND", f"User {user_id} not found")
    api_key = f"key-{uuid.uuid4().hex}"
    API_KEYS[api_key] = {"role": "user", "userId": user_id}
    return jsonify({"apiKey": api_key, "userId": user_id}), 201


# ---------- transactions ----------
@app.post("/api/transactions")
@request_auth
def create_transaction():
    body = json_object()
    if body is None:
        return fail(400, "VALIDATION_ERROR", "Request validation failed", NOT_AN_OBJECT)
    user_id, amount, tx_type, recipient_id = body.get("userId"), body.get("amount"), body.get("type"), body.get("recipientId")

    details = {}
    if not isinstance(user_id, str) or not user_id:
        details["userId"] = "userId is required"
    # bool is a subclass of int in Python, so rule it out explicitly
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        details["amount"] = "amount must be a number"
    elif not math.isfinite(amount):                 # 1e309 in JSON parses as Infinity
        details["amount"] = "amount must be a finite number"
    elif amount <= 0:
        details["amount"] = "amount must be greater than 0"
    elif amount > MAX_AMOUNT:
        details["amount"] = f"amount must be at most {MAX_AMOUNT:,}"
    elif round(amount, 2) != amount:
        details["amount"] = "amount must have at most 2 decimal places"
    if tx_type not in TRANSACTION_TYPES:
        details["type"] = f"type must be one of {', '.join(TRANSACTION_TYPES)}"
    elif tx_type == "transfer":
        if not recipient_id:
            details["recipientId"] = "recipientId is required for transfers"
        elif recipient_id == user_id:
            details["recipientId"] = "cannot transfer to yourself"
    if details:
        return fail(400, "VALIDATION_ERROR", "Request validation failed", details)

    # authorization before resource lookup, so a 404 can't leak who exists
    if not owns(user_id):
        return fail(403, "FORBIDDEN", "You can only create transactions for your own account")

    if user_id not in users:
        return fail(404, "USER_NOT_FOUND", f"User {user_id} not found")
    if tx_type == "transfer" and recipient_id not in users:
        return fail(404, "RECIPIENT_NOT_FOUND", f"Recipient {recipient_id} not found")

    transaction = {
        "id": str(uuid.uuid4()),
        "userId": user_id,
        "amount": amount,
        "type": tx_type,
        "recipientId": recipient_id if tx_type == "transfer" else None,
        "status": "completed",
        "createdAt": now(),
    }
    transactions[transaction["id"]] = transaction
    return jsonify(transaction), 201


@app.get("/api/transactions/<user_id>")
@request_auth
def get_user_transactions(user_id):
    if not owns(user_id):
        return fail(403, "FORBIDDEN", "You can only view your own transactions")
    if user_id not in users:
        return fail(404, "USER_NOT_FOUND", f"User {user_id} not found")
    # a user with no transactions yet gets an empty list, not an error
    txs = [t for t in transactions.values() if t["userId"] == user_id]
    return jsonify({"count": len(txs), "transactions": txs}), 200


# ---------- mock frontend ----------
@app.get("/ui")
def ui():
    return app.send_static_file("index.html")


@app.get("/")
def home():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", "5050")))