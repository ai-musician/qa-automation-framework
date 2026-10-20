"""
Performance and concurrency tests. Not part of the normal run; start them with:

    pytest -m perf

Each scenario sends many requests from parallel threads and checks:
  - no request failed (error rate 0%)
  - p95 latency is within its target (95% of requests were at least this fast)
and, for money and accounts, that nothing was lost or duplicated under load.

Targets are for the local Flask mock server on a laptop; against a real environment they'd come from the
team's SLOs. Results go to reports/performance.html, .json and .md (the .md is shown on the GitHub Actions run).

PERF_LOAD multiplies the request counts: PERF_LOAD=3 pytest -m perf sends 3x the requests (CI does this on main).
"""
import uuid
from pathlib import Path

import pytest

import config
from utils.assertions import assert_status
from utils.factories import SENDER_ID, make_transaction, make_user
from utils.load import run_load, write_reports

pytestmark = pytest.mark.perf

API = config.env.api_base_url


def n(requests_total):
    """Request count for a scenario, scaled by PERF_LOAD."""
    return max(1, round(requests_total * config.env.perf_load))
REPORTS = Path(__file__).parents[2] / "reports"


@pytest.fixture(scope="module")
def perf_results():
    """Every scenario adds its Summary here; after the last test, the reports are written."""
    summaries = []
    yield summaries
    if summaries:
        write_reports(summaries, REPORTS)


def check(summary, target_p95_ms, perf_results):
    """Records the scenario for the report, then fails the test if it missed its target."""
    summary.target_p95_ms = target_p95_ms
    summary.passed = summary.error_rate == 0 and summary.p95_ms <= target_p95_ms
    perf_results.append(summary)
    assert summary.error_rate == 0, f"{summary.errors} of {summary.requests} requests failed, e.g. {summary.sample_errors}"
    assert summary.p95_ms <= target_p95_ms, f"p95 {summary.p95_ms:.1f} ms is over the {target_p95_ms} ms target"


@pytest.fixture
def auth(user_api):
    """The Authorization header of a signed-in customer (user 123)."""
    return {"Authorization": user_api.session.headers["Authorization"]}


# ---------- latency under load, one endpoint at a time ----------

def test_login_under_load(perf_results):
    send = lambda s, i: s.post(f"{API}/api/auth/token", json={"apiKey": config.env.api_key})
    check(run_load("login", send, requests_total=n(200), concurrency=10), 250, perf_results)


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
    summary.target_p95_ms = None
    summary.passed = summary.statuses.get("201", 0) == 1
    perf_results.append(summary)

    assert summary.statuses.get("201", 0) == 1, f"expected exactly one 201, got {summary.statuses}"
    users = admin_api.get("/api/users")
    assert_status(users, 200)
    assert sum(u["email"] == email for u in users.json()) == 1, "the email exists more than once"
