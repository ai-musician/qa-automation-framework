# Test plan: FinPay API, web UI and inference service

One page: what is tested, how, when testing starts and stops, and what must be true before a release.
Metric definitions and goals are in [KPIs.md](KPIs.md); how to run everything is in the [README](../README.md).

## Scope

| In scope | Where |
|---|---|
| REST API: users, transactions, auth (roles, ownership), validation, error shape | `tests/api` |
| Web UI: register a user, send a transaction, error messages | `tests/ui` |
| Database: what is stored, not only what is returned (exact amounts, cascades, unique email under a race) | `tests/db` |
| Performance: TP99 SLOs, lost writes, LLM streaming (TTFT, time between tokens) | `tests/perf` |
| Resilience on Kubernetes: losing an inference pod, releasing a good and a broken version under load | `tests/chaos`, `tests/upgrade` |
| Security habits: credentials never in logs or reports | `tests/api/test_secrets_not_logged.py` |

**Out of scope:** a real model (the inference service is a mock), real payment rails, browsers other than Chromium in CI,
VM-image and operating-system testing (planned, see the README).

## Scenarios

| Area | Main scenarios | Pass means |
|---|---|---|
| Auth | valid, invalid and missing key or token; user cannot reach admin routes or another user's data | 401 or 403 with the documented error code |
| Users and transactions | create, read, update, delete, list; 76 data-driven invalid requests | documented status code, exactly the blamed fields |
| UI | register, transfer, invalid email, negative amount | the UI matches what the API stored |
| Database | amounts `0.1` and `999999.99`, delete cascade, rejected request stores nothing, 10 parallel sign-ups with one email | exact values, no orphans, one 201 and nine 409 |
| Load | login, get user, create user, create transaction, read and write mix, duplicate email x20, chat streaming at 1 to 32 users | TP99 within SLO, zero errors, no lost or duplicated records |
| Chaos | graceful pod stop, pod crash, under 8 streaming users | graceful: 0 failed replies; crash: recovery within 60 s |
| Upgrade | good release, broken release (rolled back) under 8 streaming users | 0 failed replies, no replies from the broken version |

## Entry criteria (testing can start)

- The build and the images (`api`, `tests`) build from the commit under test.
- The API answers its health check; the database is healthy when the database tests run.
- API keys are supplied by the environment (never from the repository).
- Test data is created by each test and cleaned up by it, so a shared environment needs only dedicated test accounts.

## Exit criteria (testing is done)

- All functional tests (API, UI, database) pass: 100% of those run, none skipped without a reason.
- Every documented endpoint and status code is called by at least one test (KPI: 35 of 35).
- Performance tests are within every TP99 SLO with zero errors and zero lost records, at 3x load on main.
- Chaos and upgrade tests pass: no failed reply on graceful shutdown or a good release.
- No open critical bug (`data/bugs.json`).

## Release sign-off checklist

To be ticked by QA and reviewed with the developers and the product owner before a release:

- [ ] The CI run on the release commit is green: API + UI + database, performance, Kubernetes (chaos and upgrade)
- [ ] The run on the AWS QA environment is green (the same tests against a deployed service)
- [ ] The [QA metrics review](KPIs.md) has no red metric, or each red one has an owner and an agreed action
- [ ] No open critical or high bug; any open medium or low bug is listed in the release notes
- [ ] No performance regression flagged against the last 8 merges
- [ ] Known gaps are written down (what was not tested, and why)
- [ ] QA sign-off: name and date. Product owner sign-off: name and date

## Roles and review

QA writes and maintains this plan and the tests. Developers review the scenarios for their area and own fixes for
failures. The product owner reviews scope and the sign-off. The plan is reviewed when the scope changes, and the KPI
review is the standing check of whether it still works.

## Risks

| Risk | Mitigation |
|---|---|
| The mock differs from the real system | the same tests run against any environment through `API_BASE_URL` |
| Load results vary on a shared CI machine | SLOs are on TP99 with generous limits, and the KPI review compares against the run's own drift |
| A version passes its readiness probe, then fails real requests | not caught by these tests; needs a canary or an automated post-release check |
