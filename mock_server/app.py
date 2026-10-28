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
    POST   /v1/chat/completions         OpenAI-compatible chat, streamed or not -> 200 / 400 / 401 (mock inference)
    POST   /debug/crash                 end the process, as a crash would (admin; only with FAULT_INJECTION=on)
    GET    /ui                          mock frontend (registration + transactions)

Data lives in Python dicts (stand-ins for MongoDB / Redis), so it disappears when the server stops.

Run it by hand:  python -m mock_server.app
"""

from datetime import datetime, timezone
from functools import wraps
import json
import math
import os
import re
import signal
import sys
import threading
import time
import uuid

from flask import Flask, Response, g, jsonify, request, stream_with_context

app = Flask(__name__)

# local "databases"
users: dict[str, dict] = {}          # user id -> user
transactions: dict[str, dict] = {}   # transaction id -> transaction
sessions: dict[str, dict] = {}       # token -> {role, userId} (would be Redis in the real system)

# Each API key identifies a caller: a role, and (for normal users) which user they are.
# In the real system every customer would have their own key; the mock starts with one of each role,
# and an admin can issue more with POST /api/users/<id>/api-keys (the load tests log in as many users).
# The keys come from API_KEY / ADMIN_API_KEY when set (the AWS QA environment generates new ones for every run),
# otherwise the demo keys, the same defaults the tests use (config.py).
API_KEYS = {
    os.getenv("API_KEY", "user-api-key"): {"role": "user", "userId": "123"},     # seed user 123, John Doe: a premium customer, but role user, not admin
    os.getenv("ADMIN_API_KEY", "admin-api-key"): {"role": "admin", "userId": None},  # back office: may act for anyone
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


def inference_auth(fn):
    """Like request_auth, but also takes an API key itself as the bearer token, the way OpenAI-style clients send it.
    Then an inference pod needs no session from a login, so any replica can answer any request."""
    with_session = request_auth(fn)

    @wraps(fn)
    def wrapper(*args, **kwargs):
        caller = API_KEYS.get(request.headers.get("Authorization", "").removeprefix("Bearer "))
        if caller is None:
            return with_session(*args, **kwargs)
        g.role, g.user_id = caller["role"], caller["userId"]
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


# ---------- inference: an OpenAI-compatible chat endpoint ----------
# A stand-in for an LLM inference service, so the load tests can measure what inference teams measure: time to first
# token (TTFT), the time between tokens, and tokens per second, and how they change as more users stream at once.
# No model runs here: each token is a word, produced at a fixed pace. The reply starts with the first word of the
# last message, so a test can tell if two users' streams ever got mixed up.
CHAT_MODELS = ("mock-llm-small",)
MAX_COMPLETION_TOKENS = 512
DEFAULT_COMPLETION_TOKENS = 16
DECODE_SLOTS = 16              # requests generated at once, like a model server's batch; more wait in a queue
PREFILL_MS = 20                # reading the prompt before the first token ...
PREFILL_MS_PER_TOKEN = 0.1     # ... a little longer for a longer prompt
TOKEN_MS = 10                  # each further token: 100 tokens/s per stream
REPLY_WORDS = "the quick brown fox jumps over the lazy dog while the band plays on".split()
decode_slots = threading.BoundedSemaphore(DECODE_SLOTS)


def validate_chat(body):
    details = {}
    if body.get("model") not in CHAT_MODELS:
        details["model"] = f"model must be one of {', '.join(CHAT_MODELS)}"
    messages = body.get("messages")
    if (not isinstance(messages, list) or not messages
            or not all(isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
                       and isinstance(m.get("content"), str) for m in messages)):
        details["messages"] = "messages must be a non-empty list of {role, content}"
    max_tokens = body.get("max_tokens", DEFAULT_COMPLETION_TOKENS)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= MAX_COMPLETION_TOKENS:
        details["max_tokens"] = f"max_tokens must be a whole number from 1 to {MAX_COMPLETION_TOKENS}"
    if not isinstance(body.get("stream", False), bool):
        details["stream"] = "stream must be true or false"
    return details


@app.post("/v1/chat/completions")
@inference_auth
def chat_completions():
    body = json_object()
    if body is None:
        return fail(400, "VALIDATION_ERROR", "Request validation failed", NOT_AN_OBJECT)
    if details := validate_chat(body):
        return fail(400, "VALIDATION_ERROR", "Request validation failed", details)

    messages, max_tokens = body["messages"], body.get("max_tokens", DEFAULT_COMPLETION_TOKENS)
    prompt_tokens = sum(len(m["content"].split()) for m in messages)
    first = (messages[-1]["content"].split() or ["..."])[0]
    words = [first] + [REPLY_WORDS[i % len(REPLY_WORDS)] for i in range(max_tokens - 1)]
    reply = {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "created": int(time.time()), "model": body["model"]}
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": max_tokens, "total_tokens": prompt_tokens + max_tokens}

    def tokens():
        """The reply, one token at a time, at the model's pace; waits for a free decode slot first."""
        with decode_slots:
            time.sleep((PREFILL_MS + PREFILL_MS_PER_TOKEN * prompt_tokens) / 1000)
            for i, word in enumerate(words):
                if i:
                    time.sleep(TOKEN_MS / 1000)
                yield word if i == 0 else " " + word

    if not body.get("stream", False):
        content = "".join(tokens())
        return jsonify({**reply, "object": "chat.completion", "usage": usage, "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "length"}]}), 200

    def events():
        """Server-sent events, as OpenAI streams them: a role, the tokens, the finish reason, then [DONE]."""
        def chunk(delta, finish_reason=None, **extra):
            return "data: " + json.dumps({**reply, "object": "chat.completion.chunk", **extra, "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}]}) + "\n\n"
        yield chunk({"role": "assistant", "content": ""})
        for token in tokens():
            yield chunk({"content": token})
        yield chunk({}, "length", usage=usage)
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(events()), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------- fault injection, for the chaos tests ----------
# Only exists when FAULT_INJECTION=on, which only the chaos tests' inference Deployment sets (k8s/inference.yaml).
if os.getenv("FAULT_INJECTION") == "on":
    @app.post("/debug/crash")
    @inference_auth
    @admin_only
    def crash():
        """Ends the process at once, as a crash would: no cleanup, every open connection cut."""
        threading.Timer(0.05, os._exit, args=(1,)).start()        # just after this reply is sent
        return jsonify({"crashing": True}), 202


# ---------- mock frontend ----------
@app.get("/ui")
def ui():
    return app.send_static_file("index.html")


@app.get("/")
def home():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    # docker stop and Kubernetes ask a container to stop with SIGTERM. As a container's first process, Python would
    # ignore it and be killed only when the grace period runs out, so exit straight away instead.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # HOST=0.0.0.0 in Docker, so other containers can reach it; the default only accepts this machine
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5050")))