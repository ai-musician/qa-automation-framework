# QA KPI framework

How this project measures quality: which metrics, exactly how each is calculated, the goal, and what happens when
one is red. The numbers are produced by `utils/kpi.py` at the end of every CI run and shown on the run's summary
page as the **QA metrics review**.

## Tenets

1. **Output metrics show results; input metrics are what we act on.** Availability and latency are outcomes nobody
   can change directly. Coverage, test results and feedback time are the inputs the team controls, and they drive
   the outputs. We review both, and plans are written against the inputs.
2. **Every metric is defined exactly and measured automatically.** Each one below has a formula and a source in the
   pipeline. The only hand-maintained input is bug severity (`data/bugs.json`), because severity is a triage decision.
3. **Trends over snapshots.** Each run is compared with the last 8 merges to main, so a regression is called out
   while it is still within its goal, not after it misses it.
4. **Tail latency, not averages.** The SLO is TP99 (99% of requests at least this fast). TP99.9 is reported only
   when a scenario has 1,000+ requests; below that it is effectively the slowest single request.
5. **A red metric needs an owner and an action.** The review lists every red or regressed metric as a callout at the
   top, so the review starts with the problems, not with the rows that are fine.

## Metric definitions

### Output metrics: what customers feel

| Metric | Definition | Source | Goal | Why this goal |
|---|---|---|---|---|
| Availability under load | successful requests ÷ all requests, across every load scenario | `performance.json` | ≥ 99.9% | 99.9% is a common SLO for customer-facing APIs (about 43 minutes of errors a month) |
| Error budget used | errors ÷ errors the 99.9% SLO allows for that many requests | `performance.json` | < 100% | shows how close a run came to the SLO, not just pass/fail |
| APIs within TP99 SLO | scenarios whose TP99 is within their SLO ÷ scenarios with an SLO | `performance.json` | all | each API has its own SLO; one miss is a miss |
| Login TP99 (critical path) | TP99 of 200 logins (600 on main), each by a different user with their own API key | `performance.json` | ≤ 250 ms | login is the first call of every session; it is tracked on its own with a trend |
| Lost or duplicated records under load | integrity checks that failed: transactions missing from history after a burst of writes; one email registered twice by 20 parallel sign-ups | `performance.json` | 0 | in a payments system this is a correctness incident, not a performance one |

### Input metrics: what the team controls

| Metric | Definition | Source | Goal | Why this goal |
|---|---|---|---|---|
| Documented status codes tested | (endpoint, status code) pairs from the README's API table that at least one test received ÷ all documented pairs | README vs `api.log` | 100% | every documented response, including 401/403/404, is part of the contract clients build on |
| Endpoints tested | documented endpoints called by at least one test ÷ all documented endpoints | README vs `api.log` | 100% | an untested endpoint is an unknown |
| Functional tests passing | passed ÷ (run − skipped), API + UI | `junit.xml` | 100% | main must always be releasable |
| Bugs caught before release | bugs found by the tests before any release, by severity | `data/bugs.json` | (tracked, no goal) | a count with no goal: it shows where testing pays off, and it should not be gamed |
| Open critical bugs | bugs with severity critical and status not fixed | `data/bugs.json` | 0 | nothing critical ships |

### Operational excellence: the health of main

Calculated from the last 30 completed runs of this workflow on main (cancelled runs were replaced by a newer push,
so they are left out). These are the DORA metrics, measured at the CI stage because this project has no deployment.

| Metric | Definition | Source | Goal | Why this goal |
|---|---|---|---|---|
| Change failure rate | merges to main whose run failed ÷ all merges | GitHub Actions history | ≤ 15% | the DORA "elite" band is 0–15% |
| Time to restore main | median time from the first red run on main to the next green one | GitHub Actions history | ≤ 1 h | DORA "elite" restores in under an hour |
| Time from push to result | median minutes from a push to its run finishing | GitHub Actions history | ≤ 10 min | past about 10 minutes, developers switch tasks while waiting |
| Merge frequency | merges to main per week | GitHub Actions history | (tracked, no goal) | context for the rates above: 1 failure in 4 merges is not 1 in 400 |

### API latency

Per load scenario: requests, TP50, TP90, TP99 (the SLO, bold), TP99.9 (1,000+ requests only), a sparkline of TP99
over the last 8 merges to main, and the change against their median.

**Regression callout:** for scenarios with a latency SLO, TP99 more than 25% above the median of the last 8
merges **and** at least 2 ms worse. (The parallel sign-up scenario has no SLO: it checks correctness, not speed.) The
2 ms floor exists because shared CI runners vary by a few milliseconds from run to run; without it, a 4 → 6 ms
change would be flagged as +50%. A regression is a yellow callout, not a failed build: the SLO check fails the build.

## What fails the build, and what doesn't

| Signal | Effect |
|---|---|
| A functional test fails | build fails |
| A load scenario misses its TP99 SLO, has any error, or loses / duplicates data | build fails |
| A metric is red, or TP99 regressed against its trend | callout at the top of the review; the build still passes |

The KPI job itself never fails the build. A report that can break the pipeline gets switched off the first time it
is wrong, and then nobody reads it.

## Review cadence

- **Every run:** the review is on the run's summary page. Reviewers read the callouts first.
- **Weekly:** walk the trends, not every row: anything yellow, any metric moving the wrong way for 3+ merges, and
  whether a goal is still the right goal.
- **After every escaped bug or incident:** find the root cause, add the test that would have caught it, and check
  which input metric should have warned us. In this project the status-code coverage metric came from exactly
  that: its first run listed 7 documented responses no test checked, and tests were added for all of them.

## Not measured here yet, and how it would be

These need production traffic or tooling this project doesn't have. Leaving them out is better than showing a
number that isn't measured.

| Metric | Definition | Would come from |
|---|---|---|
| Defect escape rate | bugs found after release ÷ all bugs, by severity | bug tracker, with a "found in" field |
| Sev-1 / Sev-2 incidents | count per week, with time to detect and time to mitigate | incident tracker and alarms |
| Post-incident actions closed on time | action items from incident reviews closed by their due date ÷ all due | incident tracker |
| Flaky test rate | tests that failed and then passed on a rerun of the same commit ÷ all tests | CI reruns (`pytest-rerunfailures` with its report) |
| Production TP99 and availability | the same SLOs, from real traffic and a synthetic canary calling login every minute | service metrics and dashboards |
| Deployment safety | rollbacks per deployment; changes stopped by a canary or one-box stage | deployment pipeline |
