"""
QA metrics review for one CI run: output metrics (what customers feel), input metrics (what the team controls),
operational excellence, and API latency against TP99 SLOs, each with its goal and its trend over recent merges to
main. Every number is measured from what the pipeline produces; nothing is typed in by hand except data/bugs.json.
Metric definitions and goals: docs/KPIs.md. Standard library only.

    python -m utils.kpi                                    # reads reports/ after pytest (+ pytest -m perf)
    python -m utils.kpi --functional reports/functional --perf reports/performance   # how CI calls it

Sources:
  bugs             data/bugs.json: bugs found before release, with severity
  coverage         README API table (the documented endpoints + status codes) vs api.log (what the tests sent)
  test results     junit.xml from the functional run
  performance      performance.json from pytest -m perf
  CI history       this workflow's runs on main, and their performance reports for trends (GitHub CLI;
                   those sections are left out when it isn't available)

Writes reports/kpi.md (shown on the GitHub Actions run page) and reports/kpi.json, and prints the markdown.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).parents[1]

# goals (docs/KPIs.md explains each one)
GOAL_AVAILABILITY = 99.9         # % of requests under load that succeed
GOAL_COVERAGE = 100              # % of documented endpoints / status codes exercised
GOAL_PASS_RATE = 100             # % of functional tests passing
GOAL_FEEDBACK_MIN = 10           # median minutes from push to result
GOAL_CHANGE_FAILURE = 15         # % of merges to main that broke the build
GOAL_RESTORE_HOURS = 1           # median hours from a broken main to the next green run
INTEGRITY_SCENARIOS = ("create transaction", "same email x20 in parallel")   # money / accounts must stay correct
CRITICAL_PATH = "login, different users"     # the first call of every customer session

HISTORY_RUNS = 8                 # trend window: this many earlier merges to main
OPS_RUNS = 30                    # operational window: this many completed runs on main
REGRESSION = 1.25                # TP99 more than 25% over the trend's median ...
REGRESSION_MIN_MS = 2            # ... and at least 2 ms worse (CI runners are noisy at a few ms) is a callout

GREEN, YELLOW, RED, NONE = "🟢", "🟡", "🔴", "⚪"


# ---------- measurements ----------

def documented_endpoints(readme: Path) -> dict[tuple[str, str], set[int]]:
    """{(method, path regex): {status codes}} from rows like | `POST /api/users` | create user | 201, 400 |"""
    row = re.compile(r"^\| `(GET|POST|PUT|PATCH|DELETE) (/api/\S+)` \|.*\| ([\d, ]+) \|$")
    endpoints = {}
    for line in readme.read_text().splitlines():
        if m := row.match(line):
            method, path, codes = m.groups()
            pattern = "^" + re.sub(r":\w+", "[^/]+", path) + "$"       # /api/users/:id -> ^/api/users/[^/]+$
            endpoints[(method, pattern)] = {int(c) for c in codes.split(",")}
    return endpoints


def sent_requests(api_log: Path) -> set[tuple[str, str, int]]:
    """{(method, path, status)} from api.log lines like: POST http://localhost:5050/api/users -> 201"""
    line = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (\S+) -> (\d{3})$")
    sent = set()
    for text in api_log.read_text().splitlines() if api_log.exists() else []:
        if m := line.match(text):
            sent.add((m[1], urlsplit(m[2]).path, int(m[3])))
    return sent


def coverage(readme: Path, api_log: Path) -> dict:
    endpoints = documented_endpoints(readme)
    sent = sent_requests(api_log)
    hit_endpoints, hit_codes, missing = 0, 0, []
    for (method, pattern), codes in endpoints.items():
        seen = {status for m, path, status in sent if m == method and re.match(pattern, path)}
        hit_endpoints += bool(seen)
        hit_codes += len(codes & seen)
        name = f"{method} {pattern.strip('^$').replace('[^/]+', ':id')}"
        missing += [f"{name} {code}" for code in sorted(codes - seen)]
    total_codes = sum(len(c) for c in endpoints.values())
    return {"endpoints": len(endpoints), "endpointsTested": hit_endpoints,
            "statusCodes": total_codes, "statusCodesTested": hit_codes, "untested": missing}


def test_results(junit: Path) -> dict | None:
    if not junit.exists():
        return None
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    for suite in ET.parse(junit).getroot().iter("testsuite"):
        for key in totals:
            totals[key] += float(suite.get(key, 0))
    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    return {"tests": int(totals["tests"]), "passed": int(passed), "failed": int(totals["failures"] + totals["errors"]),
            "skipped": int(totals["skipped"]), "seconds": round(totals["time"], 1)}


def bugs(bugs_json: Path) -> dict:
    found = json.loads(bugs_json.read_text())
    order = ["critical", "high", "medium", "low"]
    return {"found": len(found),
            "bySeverity": {s: n for s in order if (n := sum(b["severity"] == s for b in found))},
            "openCritical": sum(b["status"] != "fixed" and b["severity"] == "critical" for b in found),
            "open": sum(b["status"] != "fixed" for b in found)}


def gh(*args) -> str | None:
    """Output of a GitHub CLI command, or None when gh is missing, not logged in, or offline."""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def main_runs(limit) -> list[dict]:
    """Completed runs of this workflow on main, newest first; cancelled runs were superseded, so they're dropped."""
    out = gh("run", "list", "--workflow", "tests.yml", "--branch", "main", "--event", "push", "--limit", str(limit),
             "--json", "databaseId,status,conclusion,createdAt,updatedAt")
    runs = json.loads(out) if out else []
    return [r for r in runs if r["status"] == "completed" and r["conclusion"] in ("success", "failure")]


def hours_between(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600


def operational(runs: list[dict]) -> dict | None:
    """DORA-style metrics for main: how often a merge breaks it, and how fast it's green again."""
    if not runs:
        return None
    oldest_first = list(reversed(runs))
    restores = []                                       # hours from the first red run to the next green one
    broken_since = None
    for r in oldest_first:
        if r["conclusion"] == "failure" and broken_since is None:
            broken_since = r["updatedAt"]
        elif r["conclusion"] == "success" and broken_since is not None:
            restores.append(hours_between(broken_since, r["updatedAt"]))
            broken_since = None
    days = max(hours_between(oldest_first[0]["createdAt"], oldest_first[-1]["updatedAt"]) / 24, 1)
    return {"runs": len(runs), "failed": sum(r["conclusion"] == "failure" for r in runs),
            "days": round(days), "mergesPerWeek": round(len(runs) / days * 7, 1),
            "medianRestoreHours": round(statistics.median(restores), 1) if restores else None,
            "stillBroken": broken_since is not None,
            "medianFeedbackMin": round(statistics.median(hours_between(r["createdAt"], r["updatedAt"]) * 60
                                                         for r in runs), 1)}


def perf_history(runs: list[dict], current_run: str | None) -> list[list[dict]]:
    """performance.json of the last HISTORY_RUNS green merges to main (oldest first), from their CI artifacts."""
    history = []
    with tempfile.TemporaryDirectory() as tmp:
        for r in [r for r in runs if r["conclusion"] == "success" and str(r["databaseId"]) != current_run][:HISTORY_RUNS]:
            folder = Path(tmp) / str(r["databaseId"])
            if gh("run", "download", str(r["databaseId"]), "-n", "performance-reports", "-D", str(folder)) is None:
                continue                                # that run skipped performance tests, or the artifact expired
            if (folder / "performance.json").exists():
                history.append(json.loads((folder / "performance.json").read_text()))
    return list(reversed(history))


# ---------- the review ----------

def pct(part, whole, digits=0):
    return round(100 * part / whole, digits) if whole else 0.0


def sparkline(values: list[float]) -> str:
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    bars = "▁▂▃▄▅▆▇█"
    return "".join(bars[round((v - low) / (high - low) * 7) if high > low else 3] for v in values)


def metric(name, value, goal, status, trend="", note=""):
    return {"name": name, "value": value, "goal": goal, "status": status, "trend": trend, "note": note}


def tp99_trend(name: str, now_ms: float, history: list[list[dict]]):
    """(sparkline of TP99 over the history + now, change vs the history's median, regressed?)"""
    past = [s["p99_ms"] for run in history for s in run if s["name"] == name]
    if not past:
        return "", "", False
    median = statistics.median(past)
    change = pct(now_ms - median, median) if median else 0
    regressed = len(past) >= 3 and now_ms > median * REGRESSION and now_ms - median >= REGRESSION_MIN_MS
    return sparkline(past + [now_ms]), f"{change:+.0f}%", regressed


def review(k: dict, history: list[list[dict]]) -> tuple[dict[str, list], list[dict], list[str]]:
    """Builds the metric rows per section, the latency table, and the callouts."""
    sections = {"output": [], "input": [], "ops": []}
    callouts = []
    perf, tests, cov, b, ops = k["performance"], k["tests"], k["coverage"], k["bugs"], k["operational"]

    # output metrics: what a customer would notice
    latency = []
    if perf:
        total = sum(s["requests"] for s in perf)
        errors = sum(s["errors"] for s in perf)
        availability = pct(total - errors, total, 2)
        budget = total * (100 - GOAL_AVAILABILITY) / 100            # errors the SLO allows in this many requests
        status = GREEN if availability >= GOAL_AVAILABILITY else RED
        sections["output"].append(metric("Availability under load", f"{availability}% of {total:,} requests",
                                         f"≥ {GOAL_AVAILABILITY}%", status,
                                         note=f"error budget used: {pct(errors, budget)}% ({errors} of {budget:.1f})"))
        timed = [s for s in perf if s.get("target_p99_ms")]
        within = sum(s["p99_ms"] <= s["target_p99_ms"] for s in timed)
        sections["output"].append(metric("APIs within TP99 SLO", f"{within} / {len(timed)}", "all",
                                         GREEN if within == len(timed) else RED))
        if critical := next((s for s in perf if s["name"] == CRITICAL_PATH), None):
            spark, change, regressed = tp99_trend(CRITICAL_PATH, critical["p99_ms"], history)
            status = RED if critical["p99_ms"] > critical["target_p99_ms"] else YELLOW if regressed else GREEN
            sections["output"].append(metric("Login TP99 (critical path, every user their own key)",
                                             f"{critical['p99_ms']:.1f} ms", f"≤ {critical['target_p99_ms']} ms",
                                             status, f"{spark} {change}".strip()))
        integrity = [s for s in perf if s["name"] in INTEGRITY_SCENARIOS]
        broken = sum(not s["passed"] for s in integrity)
        sections["output"].append(metric("Lost or duplicated records under load", str(broken), "0",
                                         GREEN if broken == 0 else RED,
                                         note=f"{len(integrity)} checks: no lost transactions, no duplicate accounts"))
        for s in perf:
            spark, change, regressed = tp99_trend(s["name"], s["p99_ms"], history)
            target = s.get("target_p99_ms")
            status = NONE if not target else RED if s["p99_ms"] > target else YELLOW if regressed else GREEN
            latency.append({"name": s["name"], "requests": s["requests"], "p50": s["p50_ms"],
                            "p90": s.get("p90_ms"), "p99": s["p99_ms"], "p999": s.get("p999_ms"),
                            "target": target, "spark": spark, "change": change, "status": status})
            if regressed:
                callouts.append(f"{YELLOW} **{s['name']}** TP99 {s['p99_ms']:.1f} ms is {change} vs the median of the "
                                f"last {len(history)} merges: still within its {target} ms SLO, but worth a look")
    else:
        sections["output"].append(metric("Load tests", "not run in this build", "-", NONE,
                                         note="no performance-relevant file changed"))

    # input metrics: what the team controls, and what drives the outputs above
    sections["input"].append(metric("Documented status codes tested", f"{cov['statusCodesTested']} / {cov['statusCodes']}",
                                    f"{GOAL_COVERAGE}%", GREEN if cov["statusCodesTested"] == cov["statusCodes"] else RED,
                                    note=f"{cov['endpointsTested']} / {cov['endpoints']} endpoints"))
    if cov["untested"]:
        callouts.append(f"{RED} **Untested documented responses:** {', '.join(cov['untested'])}")
    if tests:
        run = tests["tests"] - tests["skipped"]
        status = GREEN if pct(tests["passed"], run) >= GOAL_PASS_RATE else RED
        sections["input"].append(metric("Functional tests passing", f"{tests['passed']} / {run}", f"{GOAL_PASS_RATE}%",
                                        status, note=f"API + UI, {tests['seconds']} s"))
        if tests["failed"]:
            callouts.append(f"{RED} **{tests['failed']} functional test(s) failed:** see the API + UI tests job")
    else:
        sections["input"].append(metric("Functional tests passing", "no results", f"{GOAL_PASS_RATE}%", RED))
        callouts.append(f"{RED} **No functional test results:** the API + UI tests job didn't finish")
    severity = ", ".join(f"{n} {s}" for s, n in b["bySeverity"].items())
    sections["input"].append(metric("Bugs caught before release", str(b["found"]), "-", NONE, note=severity))
    sections["input"].append(metric("Open critical bugs", str(b["openCritical"]), "0",
                                    GREEN if b["openCritical"] == 0 else RED))
    if b["openCritical"]:
        callouts.append(f"{RED} **{b['openCritical']} critical bug(s) open** in data/bugs.json")

    # operational excellence: the health of main, from CI history
    if ops:
        rate = pct(ops["failed"], ops["runs"])
        sections["ops"].append(metric("Change failure rate (merges that broke main)", f"{rate:.0f}%",
                                      f"≤ {GOAL_CHANGE_FAILURE}%", GREEN if rate <= GOAL_CHANGE_FAILURE else RED,
                                      note=f"{ops['failed']} of {ops['runs']} merges"))
        restore = ops["medianRestoreHours"]
        status = RED if ops["stillBroken"] else NONE if restore is None else GREEN if restore <= GOAL_RESTORE_HOURS else RED
        sections["ops"].append(metric("Time to restore main (median)", "main is red" if ops["stillBroken"]
                                      else "no failures to restore" if restore is None else f"{restore} h",
                                      f"≤ {GOAL_RESTORE_HOURS} h", status))
        sections["ops"].append(metric("Time from push to result (median)", f"{ops['medianFeedbackMin']} min",
                                      f"≤ {GOAL_FEEDBACK_MIN} min",
                                      GREEN if ops["medianFeedbackMin"] <= GOAL_FEEDBACK_MIN else RED))
        sections["ops"].append(metric("Merge frequency", f"{ops['mergesPerWeek']} / week", "-", NONE,
                                      note=f"{ops['runs']} merges in {ops['days']} days"))
        if ops["stillBroken"]:
            callouts.append(f"{RED} **main is broken:** the latest run on main failed")
        for m in sections["ops"]:
            if m["status"] == RED and not (ops["stillBroken"] and m["name"].startswith("Time to restore")):
                callouts.append(f"{RED} **{m['name']}** is {m['value']} (goal {m['goal']})")

    for m in sections["output"]:
        if m["status"] == RED:
            callouts.append(f"{RED} **{m['name']}** is {m['value']} (goal {m['goal']})")
    return sections, latency, callouts


def to_markdown(k: dict, sections: dict, latency: list, callouts: list, history: list) -> str:
    rows = [m for ms in sections.values() for m in ms if m["status"] != NONE]
    on_goal = sum(m["status"] == GREEN for m in rows)
    run = os.getenv("GITHUB_RUN_ID")
    context = [f"run {run}" if run else "local run", f"commit {os.getenv('GITHUB_SHA', '')[:7]}" if run else "",
               datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")]
    lines = ["## QA metrics review", "",
             f"_{' · '.join(c for c in context if c)} · trends: last {len(history)} merges to main · "
             f"definitions: docs/KPIs.md_", "",
             f"**{on_goal} of {len(rows)} metrics on goal.**"]
    if os.getenv("GITHUB_EVENT_NAME") == "pull_request":
        lines.append("Pull request runs use a third of main's load, so compare latency trends with care.")
    lines += [""] + ([f"- {c}" for c in callouts] or ["- No callouts: every metric is on goal and within its trend."])

    titles = {"output": "Output metrics: what customers feel",
              "input": "Input metrics: what the team controls",
              "ops": f"Operational excellence: main, last {k['operational']['runs'] if k['operational'] else 0} merges"}
    for key, title in titles.items():
        if not sections[key]:
            continue
        lines += ["", f"### {title}", "", "| | Metric | Now | Goal | Trend | Notes |", "|:-:|---|---|---|---|---|"]
        lines += [f"| {m['status']} | {m['name']} | {m['value']} | {m['goal']} | {m['trend']} | {m['note']} |"
                  for m in sections[key]]

    if latency:
        ms = lambda v: f"{v:.1f}" if v is not None else "-"
        lines += ["", "### API latency under load (ms): TP99 is the SLO", "",
                  "| | Scenario | Requests | TP50 | TP90 | TP99 | SLO | TP99.9 | TP99 trend | vs median |",
                  "|:-:|---|---:|---:|---:|---:|---:|---:|---|---:|"]
        for s in latency:
            p999 = ms(s["p999"]) if s["requests"] >= 1000 else "n < 1,000"
            lines.append(f"| {s['status']} | {s['name']} | {s['requests']:,} | {ms(s['p50'])} | {ms(s['p90'])} | "
                         f"**{ms(s['p99'])}** | {s['target'] or '-'} | {p999} | {s['spark']} | {s['change']} |")

    lines += ["", "**Not measured here, and why:** escaped defects, Sev-2 incidents and time to detect need production "
              "traffic and an incident tracker; flaky-test rate needs reruns of the same commit. "
              "docs/KPIs.md has how each would be measured."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--functional", type=Path, default=ROOT / "reports", help="folder with junit.xml + api.log")
    parser.add_argument("--perf", type=Path, default=ROOT / "reports", help="folder with performance.json")
    parser.add_argument("--out", type=Path, default=ROOT / "reports")
    args = parser.parse_args()

    runs = main_runs(OPS_RUNS)
    perf_json = args.perf / "performance.json"
    kpis = {
        "bugs": bugs(ROOT / "data" / "bugs.json"),
        "coverage": coverage(ROOT / "README.md", args.functional / "api.log"),
        "tests": test_results(args.functional / "junit.xml"),
        "performance": json.loads(perf_json.read_text()) if perf_json.exists() else None,
        "operational": operational(runs),
    }
    history = perf_history(runs, os.getenv("GITHUB_RUN_ID")) if kpis["performance"] else []
    sections, latency, callouts = review(kpis, history)
    markdown = to_markdown(kpis, sections, latency, callouts, history)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "kpi.json").write_text(json.dumps({"sections": sections, "latency": latency, "callouts": callouts,
                                                   "raw": kpis}, indent=2, ensure_ascii=False) + "\n")
    (args.out / "kpi.md").write_text(markdown)
    print(markdown)


if __name__ == "__main__":
    main()
