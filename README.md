# FinPay QA Automation Framework

[![tests](https://github.com/ai-musician/qa-automation-framework/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/ai-musician/qa-automation-framework/actions/workflows/tests.yml?query=branch%3Amain)

**Live results**, updated on every merge to main:

| | |
|---|---|
| **[QA dashboard](https://ai-musician.github.io/qa-automation-framework/)** | the run's status, quality KPIs against their goals, trends, latency against SLOs |
| **[Test report](https://ai-musician.github.io/qa-automation-framework/report/)** | every test with its steps and API calls (Allure) |
| **[CI runs](https://github.com/ai-musician/qa-automation-framework/actions/workflows/tests.yml?query=branch%3Amain)** | API + UI, performance, AWS QA environment, KPIs, publishing |

Test automation framework for the Senior Quality Engineer take-home assessment.
It tests a fintech system (users, transactions, auth) through its **REST API** and its **web UI**.

**Stack:** Python 3.12 · pytest · Playwright · requests · Faker · Flask (mock server)

> The assessment allows "the language you are most comfortable with", so this uses **Python + pytest + Playwright**
> instead of JavaScript. Playwright for Python is the same engine as Playwright for JS: same browsers,
> same auto-waiting, same locator API.

---

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

pytest                      # run everything (starts the mock server automatically)
```

| Command | What it runs |
|---|---|
| `pytest` | all tests |
| `pytest -m api` | API tests only |
| `pytest -m ui` | UI tests only |
| `pytest -m ui --headed` | UI tests in a visible browser |
| `pytest -m ui --browser firefox` | UI tests in another browser (chromium / firefox / webkit) |
| `pytest -m perf` | performance and concurrency tests (**not** included in a plain `pytest` run) |
| `pytest -k transaction` | tests with "transaction" in the name |
| `pytest -n auto` | all tests in parallel, one worker per CPU core (pytest-xdist; see below) |
| `ENV_NAME=staging pytest` | run against another environment (see below) |

After a run, open the report: `open reports/report.html`

### Or in Docker (nothing to install but Docker)

```bash
docker compose run --rm --build tests                  # API + UI tests in parallel: the API and the tests in separate containers
docker compose run --rm --build tests pytest -m perf   # performance tests (PERF_LOAD=3 docker compose run ... for 3x)
docker compose run --rm --build tests pytest -m api    # any pytest command works the same way
docker compose up --build api                          # just the API, at http://localhost:5050
docker compose down                                    # stop and remove the containers (the API keeps running until then)
```

Reports are written to `reports/` on your machine, as in a local run. CI runs these same commands.

Why `--build`: the code is copied into the images when they are built, and Compose reuses an existing image, so
without it a run after a code change tests the **old** code. With it, Docker rebuilds only what changed (cached
layers), so the browsers and `pip install` rerun only when `requirements.txt` changes. `--rm` removes the tests
container when it finishes; the API container keeps running until `docker compose down`.

### Run against a QA server

The tests image is built once and runs against any environment; only environment variables change.

**The API in Docker as the QA server.** On the QA machine, `docker compose up -d --build api` keeps the API running at
`http://<qa-machine>:5050`. Point tests at it from anywhere:

```bash
START_MOCK_SERVER=false API_BASE_URL=http://<qa-machine>:5050 BASE_URL=http://<qa-machine>:5050 pytest
```

**The tests container against a deployed QA server:**

```bash
docker compose run --rm --build --no-deps \
  -e API_BASE_URL=https://api.qa.finpay.example \
  -e BASE_URL=https://qa.finpay.example \
  -e API_KEY -e ADMIN_API_KEY \
  tests
```

- `--no-deps`: don't start the local API container; the tests only call the QA server.
- `-e API_KEY` with no value passes the key in from your shell or a CI secret. Keys are never in the image:
  `.dockerignore` keeps `.env` files out.
- The tests clean up the data they create, but on a shared QA server use dedicated test accounts, and run
  `pytest -m perf` only when the team agrees: 3× load on a shared server affects everyone testing on it.

**Where the API keys are stored:** never in the repository or the image.

| Where the tests run | Where the keys come from |
|---|---|
| Your laptop | Your shell (`export API_KEY=...`). Without Docker, also a git-ignored `.env.qa`, loaded with `ENV_NAME=qa pytest` |
| CI against a QA server | GitHub Actions secrets, masked in the logs |
| [AWS QA environment](#aws-qa-environment) | Generated fresh for every run, masked in the logs, and gone when the environment is deleted |

### Run on Kubernetes

The same two images, deployed to a local Kubernetes cluster ([kind](https://kind.sigs.k8s.io/): Kubernetes in
Docker). CI runs this on every pull request and merge (job "Tests on Kubernetes").

```bash
brew install kind kubectl          # once; also needs Docker running
scripts/k8s_test.sh                # cluster, images, Secret, API, tests: exits non-zero if any test fails
kind delete cluster --name qa      # remove the cluster
```

| File | What it defines |
|---|---|
| `k8s/api.yaml` | **Deployment** `api`: readiness and liveness probes, CPU/memory requests and limits, runs as non-root with a read-only filesystem. **Service** `api`: the tests reach it at `http://api:5050`, as in Docker Compose |
| `k8s/tests-job.yaml` | **Job** `tests`: an init container waits until the API answers, then `pytest -n auto` runs once (`backoffLimit: 0`: a failed run is a result, not something to retry); `/dev/shm` enlarged for Chromium |
| `scripts/k8s_test.sh` | creates the cluster, builds and loads the images, stores new API keys in the **Secret** `api-keys` (read by both the API and the tests), deploys, runs the Job, prints its output |

One API replica: the mock keeps its data in memory, so a second pod would hold different users. A real service
would scale out behind a shared database.

Looking around while it runs:

```bash
kubectl --context kind-qa -n qa get pods                   # the API pod and the tests pod
kubectl --context kind-qa -n qa logs -f job/tests          # the test output, live
kubectl --context kind-qa -n qa describe deployment api    # probes, limits, events
kubectl --context kind-qa -n qa port-forward svc/api 5050  # the API at http://localhost:5050
```

---

## Project layout

```
.github/workflows/
  tests.yml           CI: functional + performance tests (in Docker), then QA KPIs, on every pull request and merge to main
Dockerfile            Two images: api (the mock API) and tests (pytest + Playwright with browsers)
docker-compose.yml    Runs the API and the tests in separate containers
mock_server/
  app.py              The system under test: a Flask stand-in for the API gateway + services
  static/index.html   Mock frontend (sign in, register user, send transaction, history)
config.py             Environment configuration (which URLs, which API keys)
conftest.py           Starts the mock server once (also for parallel runs); fixtures: API clients, fresh_user
utils/
  factories.py        Test data factories (make_user, make_transaction) built on Faker
  test_data.py        Loads negative test cases from data/negative/*.json
  load.py             Load generator: parallel requests, latency percentiles, HTML/JSON report
  assertions.py       Custom assertions with readable failure messages
  kpi.py              QA metrics review: output / input / operational metrics with goals and trends
  redact.py           Masks API keys and tokens before anything is logged or attached to a report
tests/
  api/                API tests: auth, users, transactions
    test_negative_data.py   Data-driven negative tests, one run per case in data/negative/
  ui/
    pages.py          Page Object for the frontend (FinPayPage)
    test_ui_*.py      UI tests: registration, transactions, error messages
  perf/
    test_performance.py   Load scenarios with TP99 SLOs + concurrency correctness checks
data/
  negative/           Negative test data: 76 invalid requests across 5 files (users, user updates,
                      transactions, API keys, auth headers), each with the expected error
  bugs.json           Bugs found before release, with severity (feeds the KPI report)
  *.json              Seed data for the scripts
scripts/              Load seed data into a running server by hand
allure/
  plugin/             Allure plugin: this project's logo and styles, and a clear empty state for Categories
infra/
  aws-setup.yml       One-time AWS setup: budget alert, GitHub OIDC login, least-privilege role for CI
  qa-env.yml          The per-run QA environment: one instance running the api image, open to the CI runner only
docs/
  KPIs.md             QA KPI framework: every metric's definition, source, goal, and what happens when it's red
reports/              Generated: HTML report, JUnit XML, API log, failure screenshots (git-ignored)
```

---

## The system under test

The real system (Node/Express services behind an API gateway) isn't provided, so `mock_server/app.py`
implements the same contract in Flask, with in-memory dicts standing in for MongoDB and Redis.

| Endpoint | Purpose | Responses |
|---|---|---|
| `POST /api/auth/token` | exchange an API key for a bearer token | 200, 401 |
| `POST /api/users` | create user | 201, 400, 401, 409 |
| `GET /api/users/:id` | get user | 200, 401, 404 |
| `GET /api/users` | list users (**admin only**) | 200, 401, 403 |
| `PATCH /api/users/:id` | update user (own account only; admins any) | 200, 400, 401, 403, 404, 409 |
| `DELETE /api/users/:id` | delete user (**admin only**) | 204, 401, 403, 404 |
| `POST /api/users/:id/api-keys` | issue an API key for that user (**admin only**; used by the load tests to log in as many users) | 201, 401, 403, 404 |
| `POST /api/transactions` | create transaction (own account only) | 201, 400, 401, 403, 404 |
| `GET /api/transactions/:userId` | a user's transactions (own only; admins any) | 200, 401, 403, 404 |
| `POST /v1/chat/completions` | OpenAI-compatible chat, streamed (`"stream": true`) or not: the mock inference service (see [Inference performance](#inference-performance)) | 200, 400, 401 |
| `GET /ui` | mock frontend | 200 |

All errors use one shape, which the tests rely on:

```json
{"error": {"code": "VALIDATION_ERROR", "message": "Request validation failed",
           "details": {"email": "email must be a valid email address"}}}
```

Users `123` and `456` (from the assessment's example payloads) are always seeded.
`GET /api/transactions/:userId` returns an envelope: `{"count": 2, "transactions": [...]}`.

### Assumptions

The PDF requires authentication/authorization tests but doesn't define an auth scheme (no login endpoint,
keys or roles), so the mock API makes the smallest reasonable assumptions:

- Callers exchange an API key for a bearer token at `POST /api/auth/token`. No token or a bad token → **401**.
- Each key has a **role**: `user` (a customer) or `admin` (back-office staff). Two keys exist:
  - `user-api-key` → role `user`, acting as seed user `123` (John Doe).
  - `admin-api-key` → role `admin`, may act for anyone.
- Customers may only read and create **their own** transactions and update **their own** account; only admins may list or delete users. Otherwise → **403**.
- `accountType` (standard/premium) is the customer's product tier and **does not affect permissions**.
  John Doe is a premium customer with role `user`; permissions come only from the role on the API key.

The PDF also gives no validation rules, so these were chosen as reasonable for a bank:

- **name:** 2–100 characters after trimming; no line breaks or control characters.
- **email:** one `@`, no spaces, a domain ending in a dot plus 2+ letters; local part ≤ 64 and total ≤ 254 characters (RFC 5321).
- **amount:** a finite JSON number (not a string or boolean), greater than 0, at most 1,000,000.00 per transaction, at most 2 decimal places.
- **Request bodies** must be JSON objects; a list or a bare string is a 400, never a crash.

---

## How the requirements are covered

| Requirement | Where |
|---|---|
| **API: CRUD** | `tests/api/test_users.py` (create, read, update), `tests/api/test_transactions.py` (create, list), `test_only_admin_can_delete_user` (delete) |
| **API: error scenarios** | 404 unknown user/sender/recipient, 409 duplicate email, 400 missing fields, 401 no token, 403 wrong role or not owner |
| **API: data validation** | `assert_valid_user` / `assert_valid_transaction`, response == request checks, and **76 data-driven negative cases** in `data/negative/` (wrong types, wrong body shapes, boundaries, several errors at once) |
| **API: authentication** | `tests/api/test_auth.py`: valid / invalid / missing key, missing token (401 `UNAUTHENTICATED`), bogus token (401 `INVALID_TOKEN`) |
| **API: authorization** | `tests/api/test_auth.py`: role checks (user can't list or delete users) and ownership checks (user can't read or create another user's transactions or update another user; admin can) |
| **UI: user registration** | `tests/ui/test_ui_users.py::test_register_user` |
| **UI: transaction creation** | `tests/ui/test_ui_transactions.py::test_transfer` |
| **UI: error messages** | `test_register_user_with_invalid_email`, `test_transfer_with_negative_amount_shows_error` |
| **Test data factories** | `utils/factories.py` (valid data), `data/negative/*.json` + `utils/test_data.py` (invalid data) |
| **Helper functions** | `Client` + fixtures in `conftest.py`, `FinPayPage` in `tests/ui/pages.py` |
| **Environment configuration** | `config.py` + `.env.<name>` files |
| **Custom assertions** | `utils/assertions.py` |
| **Reporting: results (multiple formats)** | `reports/report.html` (people) + `reports/junit.xml` (CI) |
| **Reporting: screenshots on UI failure** | `reports/artifacts/<test>/test-failed-1.png` |
| **Reporting: API response logging** | `reports/api.log` + shown in the HTML report under each failed test |
| **Performance testing** (from the PDF overview) | `tests/perf/test_performance.py`, report in `reports/performance.html` |

---

## Design decisions

**Each test creates its own data.** Fixtures like `fresh_user` create what a test needs through the API,
and factories generate unique values (Faker `unique.email()`). Tests don't depend on each other or on run order,
so any single test can be run alone. Fixed IDs are used only for the two seeded users.

**Tests clean up after themselves.** `fresh_user` uses `yield`: setup creates the user, the test runs, then teardown
deletes it with `admin_api` — even when the test fails. Nothing is left behind on a shared staging environment.
Teardown ignores a 404, so a test that deletes the user itself doesn't make cleanup fail.

**Factories with overrides.** `make_user()` gives a valid user; `make_user(email="bad")` gives an invalid one.
One factory produces both happy-path and negative-test data, and each test states only the field it cares about.

**Negative cases are data, not code.** Each invalid request lives in `data/negative/*.json` with a short name,
an optional `why`, and the expected status, error code and blamed fields. One test per file runs every case:
adding a case is one JSON entry, and the report names each one (`test_create_user_rejects[email-double-at]`).
Cases describe a change to valid factory data (`override` / `remove`), or a whole body when the shape itself is
the problem (`body`, `raw_body`). The assertion checks that **exactly** the expected fields are blamed: a server
that rejects for the wrong reason, or only reports the first problem, fails the test.

**Three API clients as fixtures.** `client` (logged out → 401 tests), `user_api` (user role, bound to user `123`),
`admin_api` (admin role). A test asks for the identity it needs by name; login and the `Authorization` header are handled
once in `Client.login`. Having both a user and an admin client is what makes the ownership tests possible: the same
request is sent twice and must be 403 for one caller and 200 for the other.

**Custom assertions that explain failures.** `assert_api_error(res, "EMAIL_EXISTS", 409)` checks status, error code
and message, and on failure prints the full response body, so a failure can be diagnosed from the report alone.

**Page Object Model for UI.** `FinPayPage` holds every locator and user action (`open_and_sign_in`, `register`,
`send_transaction`). Tests read as user steps; a UI change means editing one file.

**`data-testid` locators.** Tests find elements by `data-testid`, not CSS classes or layout, so styling changes don't break them.

**Playwright `expect` for every UI check.** It retries until the condition holds (default 5 s), which avoids
the most common cause of flaky UI tests: reading the page before the server has answered.

**UI action, API verification.** `test_register_user` registers through the browser, then fetches the user through
the API to confirm what was actually stored. This checks that the UI and backend agree, not just what the screen shows.

**Parallel runs with pytest-xdist.** `pytest -n auto` starts one worker process per CPU core and hands each one a
share of the tests. Three things make that safe:
- *One server for all workers.* A session fixture would run once in every worker: four servers fighting over port
  5050. `conftest.py` starts the mock server in `pytest_configure` in the main pytest process instead, before any
  worker starts, and stops it in `pytest_unconfigure`. Workers are recognised by `config.workerinput`.
- *No shared data.* Every test creates its own users and transactions, so tests can run in any order and at the same
  time. Tests that read seed user 123's history only check what is inside one response.
- *One log file per worker.* Workers log to `reports/api-gw0.log`, `api-gw1.log`, ... instead of all writing over
  `reports/api.log`; the KPI coverage reads them all. JUnit and HTML reports are merged by xdist itself.

Load tests never run in parallel: workers would compete for the CPU and distort each other's latency. The perf
fixture skips them inside a worker, so `pytest -m perf` stays a single process. Measured on an 8-core laptop:
124 functional tests take 3.9 s in one process and 2.3 s with `-n auto`; CI and Docker use `-n auto`.

**Docker: one image for the API, one for the tests.** The tests image starts from Microsoft's Playwright image at
the same version as `requirements.txt`, so the browsers always match the library and nobody installs them by hand.
The API image holds only Flask and the mock server (about 50 MB). Keeping them apart mirrors production, where the
service and its test runner are deployed separately; pointing the tests image at staging is just a change of
`API_BASE_URL`. The API container has a health check, and the tests wait for it instead of sleeping.

---

## AWS QA environment

On every merge to main, a CI job creates a real QA environment on AWS, runs the API + UI tests against it, and
deletes it: the same test image as everywhere else, pointed at a different address.

| Stage | How |
|---|---|
| **Create** | `infra/qa-env.yml` (CloudFormation): one `t3.micro` that builds the `api` image from the run's exact commit, with the same Dockerfile as locally |
| **Test** | `docker compose run --no-deps -e API_BASE_URL=http://<instance>:5050 ... tests`, as in [Run against a QA server](#run-against-a-qa-server) |
| **Delete** | always, also when tests fail or the run is cancelled; then CI checks no instance tagged `project=qa-env` is left, and fails if one is |

**Cost controls**
- The environment exists only during a run (a few minutes); the run summary shows its lifetime and estimated cost.
- Safety net: the instance terminates itself after 60 minutes even if CI never deletes it (`shutdown` with
  `InstanceInitiatedShutdownBehavior: terminate`).
- A monthly **budget alert** is defined as code in `infra/aws-setup.yml` (emails at 85% and 100% of $5, and when the
  forecast passes it). The email address is a deploy-time parameter, so it isn't in the repository.

**Security controls**
- **No AWS keys in GitHub.** CI signs in with GitHub's OIDC token for short-lived credentials. The role
  (`infra/aws-setup.yml`) trusts only `main` of this repository, so pull requests and forks can't use it, and it
  matches GitHub's immutable subject (owner and repository IDs), so a recreated repository with the same name can't.
- **Least privilege.** The role can create and delete `qa-env-*` stacks, launch only `t2/t3.micro` in one region,
  and delete only resources tagged `project=qa-env`.
- **Not public.** Port 5050 is open to the CI runner's IP address only; the instance requires IMDSv2.
- **New API keys every run**, generated in CI, masked in the CI log and in every report, never stored.

One-time setup (an admin, in the AWS console): deploy `infra/aws-setup.yml` as a CloudFormation stack (set
`CreateGitHubOIDCProvider` to `false` if the account already has GitHub's identity provider), then save its outputs
as repository variables `AWS_ROLE_ARN` and `AWS_REGION`. Without them the job is skipped.

---

## Environment configuration

Settings come from environment variables, optionally loaded from `.env.<ENV_NAME>` (default `local`):

| Variable | Default | Meaning |
|---|---|---|
| `ENV_NAME` | `local` | which `.env.<name>` file to load |
| `BASE_URL` | `http://localhost:5050` | frontend URL |
| `API_BASE_URL` | `http://localhost:5050` | API gateway URL |
| `API_KEY` / `ADMIN_API_KEY` | demo keys | credentials for the user / admin role |
| `START_MOCK_SERVER` | `true` | `false` when testing a deployed environment (and in Docker, where the API has its own container) |

Example `.env.staging` (git-ignored, so real keys are never committed):

```
BASE_URL=https://staging.finpay.example
API_BASE_URL=https://api.staging.finpay.example
API_KEY=...
ADMIN_API_KEY=...
START_MOCK_SERVER=false
```

---

## Reporting

Every run writes to `reports/`:

| File | For |
|---|---|
| `report.html` | people: results, durations, error details and captured API logs per test |
| `junit.xml` | CI tools (Jenkins, GitHub Actions, GitLab) |
| `api.log` | every API request and response (method, URL, status, bodies), **credentials masked** (`api-gw*.log` per worker with `-n`) |
| `allure-results/` | raw results for the **Allure report**: each test with its API calls attached, grouped by layer and module |
| `artifacts/<test>/test-failed-1.png` | screenshot of the browser at the moment a UI test failed |
| `performance.html` / `performance.json` | after `pytest -m perf`: req/s, TP50/TP90/TP99/TP99.9, errors and pass/fail per scenario |
| `kpi.md` / `kpi.html` / `kpi.json` | after `python -m utils.kpi`: the QA metrics review, as the CI summary, a dashboard, and data (see below) |

**Allure report.** Every merge to main publishes it to GitHub Pages, so anyone can open it without downloading
anything: **https://ai-musician.github.io/qa-automation-framework/report/**. It shows each test's API calls (request and
response), groups tests by layer (API / UI / Performance) and module, sorts failures into product defects and test
defects, links back to the CI run, and keeps history, so the trend charts and each test's past results build up
run after run. Locally: `brew install allure`, then `allure serve reports/allure-results`.

**Credentials are masked everywhere** (`utils/redact.py`): `api.log`, the HTML report and the Allure report are CI
artifacts, the Allure report is public, and against staging the keys are real secrets. Two layers: known values
(the configured API keys and every token the tests receive, wherever they appear) and secret-looking fields
(`apiKey`, `api_key`, `token`, `password`...), including in Allure's record of test parameters. Masking works on the
text, so the log still shows exactly what was sent, broken JSON included. `tests/api/test_secrets_not_logged.py`
covers each shape a key arrives in, and CI refuses to publish the Allure report if a key is found in it anyway.
Masked test parameters are excluded from Allure's test identity (the pytest case id is used instead): otherwise
cases whose inputs mask to the same text would be merged as "retries". CI also refuses to publish a report that
shows a different number of tests than JUnit says ran.

---

## QA KPIs

Every CI run ends with a **QA metrics review** on its summary page (`utils/kpi.py`, standard library only), and
every merge to main publishes it as the site's landing page: **https://ai-musician.github.io/qa-automation-framework/**
(the run's status and suites, tests per merge to main from the report's history, the key signals, login TP99
against its recent median, coverage, a searchable table of every metric that opens to show where each number comes
from, TP99 against each API's SLO, the environment, and bugs by severity; light and dark mode with a switch, works on
a phone, and its data downloads as `kpi.json`).
The full definitions, goals and reasoning are in **[docs/KPIs.md](docs/KPIs.md)**; in short:

| Section | Metrics | Goals |
|---|---|---|
| **Output**: what customers feel | availability under load (+ error budget used); APIs within their TP99 SLO; login TP99 with its trend; lost or duplicated records under load | ≥ 99.9%; all; ≤ 250 ms; 0 |
| **Input**: what the team controls | documented status codes tested; functional tests passing; bugs caught before release by severity; open critical bugs | 100%; 100%; tracked; 0 |
| **Operational excellence**: health of main | change failure rate; time to restore main; time from push to result; merge frequency | ≤ 15%; ≤ 1 h; ≤ 10 min; tracked |
| **API latency** | TP50 / TP90 / TP99 / TP99.9 per scenario, with a TP99 sparkline over the last 8 merges to main | TP99 ≤ each API's SLO |

How it works:
- **Measured, not typed in.** Coverage compares the README's API table with `api.log` (every request the tests sent);
  latency comes from `performance.json`; the operational metrics and trends come from this workflow's history on
  main through the GitHub CLI. The one hand-kept input is bug severity in `data/bugs.json`.
- **Callouts first.** Every red metric, and every TP99 more than 40% (and 2 ms) worse than its median over the last
  8 merges beyond the whole run's drift (a slow CI machine slows every scenario together), is listed at the top, so a review starts with what needs attention.
- **It reports; it never fails the build.** The tests do that: a missed TP99 SLO, a single error under load or a lost
  record fails the performance job.

Status-code coverage earned its place on its first run: it listed 7 documented responses no test checked (401 on
five routes, two 404s). Tests for them were added, and it now reports 35 / 35.

Run it locally after the tests: `pytest`, then `python -m utils.kpi --perf <folder with performance.json>`
(`pytest -m perf` writes its own `junit.xml` and `api.log` to `reports/`, so keep its `performance.json` elsewhere or run it first).

---

## Performance testing

The PDF's overview lists performance testing as part of the assessment, so the framework includes a small,
dependency-free load generator (`utils/load.py`: threads + `requests`) and seven scenarios, plus the
[inference sweep](#inference-performance) below:

| Scenario | Load | Checks |
|---|---|---|
| login | 200 requests, 10 parallel, one user | TP99 ≤ 250 ms, 0 errors |
| login, different users | 200 users, each with their own API key, 10 parallel | TP99 ≤ 250 ms, 0 errors, and **each token belongs to its user**: it reads its own user's transactions (200) and is refused the next user's (403) |
| get user | 500 requests, 20 parallel | TP99 ≤ 150 ms, 0 errors |
| create user | 300 requests, 20 parallel | TP99 ≤ 250 ms, 0 errors |
| create transaction | 300 requests, 20 parallel | TP99 ≤ 250 ms, 0 errors, **no lost writes**: the account history grows by exactly the number of 201s |
| history read + write mix | 400 requests, 20 parallel, half reads, half writes | TP99 ≤ 300 ms, 0 errors (reading a list while it's being written to is a classic crash) |
| same email ×20 in parallel | 20 sign-ups at once with one email | **exactly one** 201, nineteen 409s, and the email stored once |

Why different users: 200 logins by one user mostly measure a cache, and in a real system would run into per-user
rate limits or account lockout. The `login_users` fixture uses the admin key to create the users and their keys
(`POST /api/users/:id/api-keys`) before timing starts, and deletes them afterwards. Against staging, that admin
credential would be the only secret; the test users are created fresh on every run, so no keys are stored anywhere.

How it measures: each run starts with 10 unmeasured warm-up requests, then reports requests per second and
TP50/TP90/TP99/TP99.9/max latency. A scenario passes when **TP99** is within its SLO **and** no request failed. The
SLO is on the tail, not the average or the median: at 1,000 requests a minute, a slow p99 is still 10 slow requests
a minute for real customers. TP99.9 is reported but only means something with 1,000+ requests per scenario.

Example run on a laptop against the mock server: TP99 between 5 and 30 ms, 1,900–3,400 requests/second, no errors.

What the numbers mean and don't mean:
- The targets are generous because the mock is a single Flask dev server on the same machine as the tests.
  Against a real environment they'd come from the team's SLOs, and the same scenarios run with
  `ENV_NAME=staging pytest -m perf`.
- The concurrency checks matter more than the milliseconds for a fintech: lost transactions or duplicate accounts
  under load are correctness bugs, not speed problems.
- The duplicate-email check passed in 15 repeated runs, but the mock still checks "is this email taken?" and
  saves in two steps. A real service needs a unique index on email (MongoDB) so the database rejects the
  second insert; the test is there to catch it if that ever regresses.

### Inference performance

LLM inference services are judged by different numbers than a REST API, so the mock also serves an
OpenAI-compatible chat endpoint (`POST /v1/chat/completions`, streamed as server-sent events with
`"stream": true`), and `run_stream_load` in `utils/load.py` times every token as it arrives:

| Metric | What it tells you | SLO here |
|---|---|---|
| **TTFT** (time to first token) | how long a user stares at a blank screen | TP99 ≤ 250 ms, up to capacity |
| **Time between tokens** | whether text streams smoothly once it starts | TP99 ≤ 50 ms, at any load |
| **Output tokens/s** | throughput: all streams together | measured |

`test_chat_streaming_under_load` sweeps **1, 8, 16 and 32 concurrent users**, and also checks that every user gets
their own, complete reply (each prompt carries a marker the reply must start with, so mixed-up streams fail),
that no stream is cut short, and that requests past capacity wait rather than fail.

The mock works like a model server with a fixed batch: it generates 16 replies at once, 20 ms to read the prompt
and 10 ms per token. A laptop run shows the knee capacity planning looks for:

| Users | TTFT TP50 | TTFT TP99 | Between tokens TP99 | Output tokens/s |
|---:|---:|---:|---:|---:|
| 1 | 30 ms | 32 ms | 13 ms | 81 |
| 8 | 37 ms | 44 ms | 18 ms | 631 |
| 16 | 32 ms | 51 ms | 18 ms | 1,238 |
| 32 | 417 ms | 821 ms | 19 ms | 1,267 (saturated) |

Up to 16 users, throughput grows almost linearly and TTFT stays flat. At 32, throughput stops growing and TTFT
jumps 16×: half the requests queue for a free slot. Once a reply starts, it streams just as smoothly, which is why
TTFT and time between tokens are measured separately. Against a real inference service, the same test finds the
concurrency where TTFT leaves its SLO: the point to scale out, or to queue and shed load.

No model runs here: each token is a word, at a fixed pace. The value is in the method, which carries over to a real
endpoint by changing `API_BASE_URL` and the model name.

## CI/CD (GitHub Actions)

`.github/workflows/tests.yml` runs on every **pull request** to `main` (again on each new push to it) and on every
**merge** to `main`. It can also be started by hand from the Actions tab.

Both test jobs run in **Docker** (`docker compose run --rm tests ...`), the same commands as on a laptop, so a
failure in CI can be reproduced locally with one command. The API runs in its own container, and the tests reach it
by its service name (`http://api:5050`), as they would reach a deployed service.

| Job | Runs | Pull request | Merge to `main` |
|---|---|---|---|
| **API + UI tests** | `docker compose run --rm tests` | ✅ | ✅ |
| **Performance tests** | `docker compose run --rm tests pytest -m perf`, only if the first job passed; on a pull request, also only if a performance-relevant file changed (not docs, the README, or the dashboard and report styling) | normal load (`PERF_LOAD=1`): a quick check before merging | **3× load** (`PERF_LOAD=3`), on the code that gets deployed |
| **QA KPIs** | `python -m utils.kpi` on the two jobs' reports, always | ✅ | ✅ |
| **Allure report** | builds the report from both jobs' results, with history, and publishes it to GitHub Pages | - | ✅ |
| **Tests on AWS QA environment** | creates the environment, runs API + UI tests against it, deletes it, checks nothing is left | - | ✅ |

- **Allure report** runs on merges to main only (the report is public), also when tests failed, since a red run is
  the one worth looking at. It refuses to publish if a credential made it into the report.
- A third job, **QA KPIs**, always runs after the other two (also when they failed or performance was skipped)
  and adds the [QA metrics review](#qa-kpis) to the summary page. It reports; it never fails the build.
- Reports are attached to every run under **Artifacts** (HTML report, JUnit XML, API log, failure screenshots,
  performance HTML/JSON), even when tests fail.
- Performance results appear as a table on the run's **summary page**, so a reviewer sees TP99 and errors per scenario
  without downloading anything.
- A new push to a PR cancels the previous, now outdated run.
- Performance tests are skipped when a change can't affect performance: they run only if the server code
  (`mock_server/*.py`), `tests/perf/`, `utils/`, test setup (`conftest.py`, `config.py`, `pytest.ini`), `requirements.txt`
  or the workflow changed. A README or `.gitignore` edit runs the API + UI tests only. Manual runs always include them.
- To block merging on failures, make both jobs **required status checks** in the repository's branch protection
  settings for `main`.

Run the same load locally: `PERF_LOAD=3 pytest -m perf`.

## Bugs found in the mock API (and fixed)

Writing tests against earlier versions of the mock API surfaced these defects. The bottom five were all found
by the data-driven negative tests on their first run (16 of 76 cases failed against the old server). The same list, with a severity for each, is in
`data/bugs.json` and feeds the [QA KPIs](#qa-kpis):

| Bug | Impact | Fix |
|---|---|---|
| `amount: "abc"` → **500 Internal Server Error** (`"abc" <= 0` raises in Python) | bad input crashes the server instead of a 400 | check the type before the value |
| Transactions accepted for **users that don't exist** | money "sent" from or to nobody | 404 `USER_NOT_FOUND` / `RECIPIENT_NOT_FOUND` |
| Transfer with no `recipientId`, or to yourself, accepted | invalid financial records | 400 with a field-level message |
| `GET /api/transactions/:userId` returned **404 for a user with no transactions** | clients can't tell "no data" from "no user" | `200` with an empty list for an existing user, 404 only for unknown users |
| Any logged-in user could list all users | no authorization | admin-only, 403 for other roles |
| Any logged-in user could **read or create any user's transactions** | one customer's key exposes everyone's financial data | tokens carry an identity; ownership is checked before the resource lookup (403, and no 404 that leaks who exists) |
| Any customer could **update any other user** (e.g. change their account type); found while writing the update test | one customer can change another customer's account | `PATCH` checks ownership too; `test_user_cannot_update_other_user` guards it |
| Validation errors returned a generic message | the UI can't show which field is wrong | `details` keyed by field |
| A JSON **list or string body crashed** user creation, transactions and login (**500**); so did `apiKey` sent as a list | any malformed client request takes down the handler | bodies must be JSON objects → 400 (login → 401) |
| **Invalid emails accepted**: `@qa.test`, `ann@@qa.test`, `ann lee@qa.test`, `ann@qa.`, a 65-character local part | accounts created with addresses that can never receive mail | stricter email rule with RFC length limits |
| Names with **line breaks** and names over 100 characters accepted | broken CSV exports, statements and logs | 2–100 characters, no control characters |
| An amount of **`1e309` (Infinity) was accepted** and stored | an infinite balance; the API then returns invalid JSON | amounts must be finite |
| **No upper limit** on a transaction amount | one request can move any amount | at most 1,000,000.00 per transaction |

---

## What I'd add with more time

- **More negative data:** Unicode edge cases (emoji, right-to-left text, look-alike letters in emails),
  very large request bodies, and duplicate JSON keys.
- **Fail on performance regressions:** the KPI review already calls out a TP99 that is 40% worse than its trend over
  the last 8 merges; on a real service with stable test hardware I'd make that block the merge, not just warn.
- **Contract tests:** validate responses against an OpenAPI schema so services and the gateway can't drift apart.
- **Bigger load tests:** k6 or Locust for longer runs (soak tests over hours, ramping to find the breaking point),
  run from CI against staging rather than a laptop.
- **Cross-browser UI runs:** chromium, firefox and webkit in CI.

---

## Try the API by hand

```bash
python -m mock_server.app                                   # terminal 1

# terminal 2: get a token, then use it
TOKEN=$(curl -s -X POST localhost:5050/api/auth/token -H "Content-Type: application/json" \
        -d '{"apiKey": "user-api-key"}' | python -c 'import sys,json; print(json.load(sys.stdin)["token"])')

curl -X POST localhost:5050/api/users -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"name": "Ann Lee", "email": "ann@qa.test", "accountType": "premium"}'
curl localhost:5050/api/users/123 -H "Authorization: Bearer $TOKEN"
```

Or open http://localhost:5050/ui and sign in with `user-api-key`.

To load the sample data in `data/`: `python scripts/create_users.py`
