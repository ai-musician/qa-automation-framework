"""
Performance and concurrency tests. Not part of the normal run; start them with:

    pytest -m perf

Each scenario sends many requests from parallel threads and checks:
  - no request failed (error rate 0%)
  - TP99 latency is within its SLO (99% of requests were at least this fast)
and, for money and accounts, that nothing was lost or duplicated under load.

Targets are for the local Flask mock server on a laptop; against a real environment they'd come from the
team's SLOs. Results go to reports/performance.html, .json and .md (the .md is shown on the GitHub Actions run).

PERF_LOAD multiplies the request counts: PERF_LOAD=3 pytest -m perf sends 3x the requests (CI does this on main).
"""
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path

import allure
import pytest

import config
from utils.assertions import assert_status
from utils.factories import SENDER_ID, make_transaction, make_user
from utils.http import Session
from utils.load import run_load, run_stream_load, write_reports

pytestmark = pytest.mark.perf

API = config.env.api_base_url


def n(requests_total):
    """Request count for a scenario, scaled by PERF_LOAD."""
    return max(1, round(requests_total * config.env.perf_load))
REPORTS = Path(__file__).parents[2] / "reports"


@pytest.fixture(scope="module")
def perf_results():
    """Every scenario adds its Summary here; after the last test, the reports are written."""
    # Load tests measure latency: parallel workers (pytest -n) would compete for the same CPU and distort each
    # other's numbers, and each worker would write its own partial report. So they never run in an xdist worker.
    if os.getenv("PYTEST_XDIST_WORKER"):
        pytest.skip("load tests run in one process: use pytest -m perf without -n")
    summaries = []
    yield summaries
    if summaries:
        write_reports(summaries, REPORTS)


def check(summary, target_p99_ms, perf_results):
    """Records the scenario for the report, then fails the test if it missed its SLO."""
    summary.target_p99_ms = target_p99_ms
    summary.passed = summary.error_rate == 0 and summary.p99_ms <= target_p99_ms
    perf_results.append(summary)
    allure.attach(json.dumps(asdict(summary), indent=2), name=f"{summary.name}: load results",
                  attachment_type=allure.attachment_type.JSON)
    assert summary.error_rate == 0, f"{summary.errors} of {summary.requests} requests failed, e.g. {summary.sample_errors}"
    assert summary.p99_ms <= target_p99_ms, f"TP99 {summary.p99_ms:.1f} ms is over the {target_p99_ms} ms SLO"


@pytest.fixture
def auth(user_api):
    """The Authorization header of a signed-in customer (user 123)."""
    return {"Authorization": user_api.session.headers["Authorization"]}


# ---------- latency under load, one endpoint at a time ----------

def test_login_under_load(perf_results):
    send = lambda s, i: s.post(f"{API}/api/auth/token", json={"apiKey": config.env.api_key})
    check(run_load("login", send, requests_total=n(200), concurrency=10), 250, perf_results)


@pytest.fixture
def login_users(admin_api):
    """n(200) new users, each with their own API key. Setup, so not timed; deleted afterwards, also when the
    setup fails partway, so a shared QA server is never left with test accounts."""
    count = n(200)
    if count < 2:
        pytest.skip("needs at least 2 users: each token is also checked against another user's data")
    user_ids, created = [], []
    try:
        for _ in range(count):
            user = admin_api.post("/api/users", make_user(email=f"login-{uuid.uuid4().hex}@qa.test"))
            assert_status(user, 201)
            user_ids.append(user.json()["id"])          # recorded before its key, so cleanup finds it either way
            key = admin_api.post(f"/api/users/{user_ids[-1]}/api-keys")
            assert_status(key, 201)
            created.append(key.json())                  # {"apiKey": ..., "userId": ...}
        yield created
    finally:
        # 204 deleted, 404 already gone; anything else is a leftover account, so the cleanup fails loudly
        left = [uid for uid in user_ids if admin_api.delete(f"/api/users/{uid}").status_code not in (204, 404)]
        assert not left, f"{len(left)} of {len(user_ids)} test users could not be deleted, e.g. {left[:3]}"


def test_login_different_users_under_load(login_users, perf_results):
    """Every user logs in with their own key, and each token must belong to the user who got it."""
    tokens = {}                                         # request number -> token

    def send(s, i):
        res = s.post(f"{API}/api/auth/token", json={"apiKey": login_users[i % len(login_users)]["apiKey"]})
        if res.status_code == 200:
            tokens[i] = res.json()["token"]
        return res

    check(run_load("login, different users", send, requests_total=len(login_users), concurrency=10), 250, perf_results)

    # after the load: each token may read its own user's transactions (200) and not the next user's (403)
    api = Session()
    for i, token in tokens.items():
        own = login_users[i % len(login_users)]["userId"]
        other = login_users[(i + 1) % len(login_users)]["userId"]
        headers = {"Authorization": f"Bearer {token}"}
        assert_status(api.get(f"{API}/api/transactions/{own}", headers=headers), 200)
        assert_status(api.get(f"{API}/api/transactions/{other}", headers=headers), 403)


def test_get_user_under_load(auth, perf_results):
    send = lambda s, i: s.get(f"{API}/api/users/{SENDER_ID}", headers=auth)
    check(run_load("get user", send, requests_total=n(500), concurrency=20), 150, perf_results)


def test_create_user_under_load(auth, perf_results):
    send = lambda s, i: s.post(f"{API}/api/users", json=make_user(email=f"load-{uuid.uuid4().hex}@qa.test"), headers=auth)
    check(run_load("create user", send, requests_total=n(300), concurrency=20), 250, perf_results)


def test_create_transaction_under_load(auth, user_api, perf_results):
    """Latency, and no lost writes: every 201 must show up in the account's history."""
    before = user_api.get(f"/api/transactions/{SENDER_ID}").json()["count"]
    send = lambda s, i: s.post(f"{API}/api/transactions", json=make_transaction(amount=1.00), headers=auth)

    summary = run_load("create transaction", send, requests_total=n(300), concurrency=20)

    after = user_api.get(f"/api/transactions/{SENDER_ID}").json()["count"]
    created = summary.statuses.get("201", 0)
    check(summary, 250, perf_results)
    assert after - before == created + 10, f"{created} created (+10 warm-up), but history grew by {after - before}"


def test_read_history_while_writing(auth, perf_results):
    """Half the requests add transactions while the other half read the same account's history."""
    def send(s, i):
        if i % 2:
            return s.post(f"{API}/api/transactions", json=make_transaction(amount=1.00), headers=auth)
        return s.get(f"{API}/api/transactions/{SENDER_ID}", headers=auth)

    check(run_load("history read + write mix", send, requests_total=n(400), concurrency=20), 300, perf_results)


# ---------- correctness under concurrency ----------

def test_same_email_registered_in_parallel_creates_one_user(auth, admin_api, perf_results):
    """20 sign-ups with one email at the same moment: exactly one may win, the rest get 409."""
    email = f"race-{uuid.uuid4().hex}@qa.test"
    send = lambda s, i: s.post(f"{API}/api/users", json=make_user(email=email), headers=auth)

    summary = run_load("same email x20 in parallel", send, requests_total=20, concurrency=20,
                       warmup=0, ok_statuses=(201, 409))
    summary.target_p99_ms = None
    summary.passed = summary.statuses.get("201", 0) == 1
    perf_results.append(summary)

    assert summary.statuses.get("201", 0) == 1, f"expected exactly one 201, got {summary.statuses}"
    users = admin_api.get("/api/users")
    assert_status(users, 200)
    assert sum(u["email"] == email for u in users.json()) == 1, "the email exists more than once"


# ---------- inference: streamed chat completions ----------
# The mock inference endpoint generates 16 replies at once (its decode slots, like a model server's batch), so the
# sweep goes past that: up to 16 users, time to first token (TTFT) stays low; at 32, half the requests wait for a
# free slot, and TTFT jumps. That knee is what capacity planning looks for, so 32 users is measured, not judged.

CHAT_TOKENS = 32              # tokens per reply
TTFT_SLO_MS = 250             # TP99 time to first token, at or below capacity
BETWEEN_TOKENS_SLO_MS = 50    # TP99 time between tokens, at any load: once a reply starts, it must flow smoothly


@pytest.mark.parametrize("users, ttft_slo_ms", [(1, TTFT_SLO_MS), (8, TTFT_SLO_MS), (16, TTFT_SLO_MS), (32, None)],
                         ids=["1 user", "8 users", "16 users", "32 users (past capacity)"])
def test_chat_streaming_under_load(auth, perf_results, users, ttft_slo_ms):
    """TTFT, time between tokens and tokens per second as more users stream at once; and every user gets their own,
    complete reply: it starts with the request's own marker, has every token, and ends properly."""
    def send(s, i):
        return s.post(f"{API}/v1/chat/completions", headers=auth, stream=True, json={
            "model": "mock-llm-small", "stream": True, "max_tokens": CHAT_TOKENS,
            "messages": [{"role": "user", "content": f"req-{i} summarize my account activity"}]})

    name = f"chat first token, {users} user{'s' if users > 1 else ''}"
    requests_total = n(max(12, 4 * users))                      # at least 4 rounds of every user
    summary, streams = run_stream_load(name, send, requests_total, concurrency=users)

    if ttft_slo_ms:
        check(summary, ttft_slo_ms, perf_results)               # no errors, and TTFT TP99 within its SLO
    else:
        summary.passed = summary.error_rate == 0
        perf_results.append(summary)
        assert summary.error_rate == 0, f"{summary.errors} of {summary.requests} streams failed past capacity, " \
                                        f"e.g. {summary.sample_errors}: requests should queue, not fail"
    assert summary.itl_p99_ms <= BETWEEN_TOKENS_SLO_MS, \
        f"TP99 between tokens {summary.itl_p99_ms:.1f} ms is over {BETWEEN_TOKENS_SLO_MS} ms"
    for i, stream in enumerate(streams, start=4):               # request numbers start after the 4 warm-up requests
        assert stream.content.startswith(f"req-{i} "), f"request {i} got another reply: {stream.content[:40]!r}"
        assert stream.tokens == CHAT_TOKENS and stream.finish_reason == "length" and stream.done, \
            f"request {i}: {stream.tokens} of {CHAT_TOKENS} tokens, finish_reason {stream.finish_reason}, done {stream.done}"
