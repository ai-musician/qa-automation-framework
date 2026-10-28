"""
QA metrics review for one CI run: output metrics (what customers feel), input metrics (what the team controls),
operational excellence, and API latency against TP99 SLOs, each with its goal and its trend over recent merges to
main. Every number is measured from what the pipeline produces; nothing is typed in by hand except data/bugs.json.
Metric definitions and goals: docs/KPIs.md. Standard library only.

    python -m utils.kpi                                    # reads reports/ after pytest (+ pytest -m perf)
    python -m utils.kpi --functional reports/functional --perf reports/performance   # how CI calls it

Sources:
  bugs             data/bugs.json: bugs found before release, with severity
  coverage         README API table (the documented endpoints + status codes) vs api*.log (what the tests sent)
  test results     junit.xml from the functional run
  performance      performance.json from pytest -m perf
  CI history       this workflow's runs on main, and their performance reports for trends (GitHub CLI;
                   those sections are left out when it isn't available)

Writes reports/kpi.md (shown on the GitHub Actions run page) and reports/kpi.json, and prints the markdown.
"""
import argparse
import html
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import urllib.request
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

TREND_RUNS = 9                   # the run-history chart: this run and the 8 before it
HISTORY_RUNS = 8                 # trend window: this many earlier merges to main
OPS_RUNS = 30                    # operational window: this many completed runs on main
REGRESSION = 1.25                # TP99 more than 25% over the trend's median ...
REGRESSION_MIN_MS = 2            # ... and at least 2 ms worse (CI runners are noisy at a few ms) is a callout

GOOD, WARNING, CRITICAL, NONE = "good", "warning", "critical", "none"     # status of a metric
DEFAULT_REPOSITORY = "ai-musician/qa-automation-framework"


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


def sent_requests(folder: Path) -> set[tuple[str, str, int]]:
    """{(method, path, status)} from lines like: POST http://localhost:5050/api/users -> 201
    in api.log, or in api-gw0.log, api-gw1.log, ... when the tests ran in parallel (pytest -n)."""
    line = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (\S+) -> (\d{3})$")
    sent = set()
    for api_log in folder.glob("api*.log"):
        for text in api_log.read_text().splitlines():
            if m := line.match(text):
                sent.add((m[1], urlsplit(m[2]).path, int(m[3])))
    return sent


def coverage(readme: Path, reports: Path) -> dict:
    endpoints = documented_endpoints(readme)
    sent = sent_requests(reports)
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


def suites(junit_files: list[Path]) -> list[dict]:
    """Per test suite (tests/api, tests/ui, tests/perf): tests, failed, skipped, seconds, from JUnit."""
    per = {}
    for junit in junit_files:
        for case in ET.parse(junit).getroot().iter("testcase"):
            parts = case.get("classname", "").split(".")
            key = parts[1] if len(parts) > 1 and parts[0] == "tests" else "other"
            suite = per.setdefault(key, {"key": key, "tests": 0, "failed": 0, "skipped": 0, "seconds": 0.0})
            suite["tests"] += 1
            suite["seconds"] += float(case.get("time") or 0)
            if case.find("failure") is not None or case.find("error") is not None:
                suite["failed"] += 1
            elif case.find("skipped") is not None:
                suite["skipped"] += 1
    order = ["api", "ui", "perf"]
    return sorted(per.values(), key=lambda s: order.index(s["key"]) if s["key"] in order else len(order))


def run_trend(current_run, passed: int, total: int) -> list[dict]:
    """Tests per merge to main: the published Allure report's history (real counts, one per run) plus this run."""
    try:
        with urllib.request.urlopen(links()["allure"] + "history/history-trend.json", timeout=10) as res:
            history = json.loads(res.read())
    except (OSError, ValueError):
        history = []                                    # offline, or no report published yet
    points = sorted(({"run": p["buildOrder"], "passed": p["data"].get("passed", 0), "total": p["data"].get("total", 0)}
                     for p in history if p.get("buildOrder") and str(p["buildOrder"]) != str(current_run)),
                    key=lambda p: p["run"])
    return points[-(TREND_RUNS - 1):] + [{"run": current_run or "now", "passed": passed, "total": total, "current": True}]


def environment(perf: list | None) -> dict:
    """What this run was built and tested with, from the CI context and requirements.txt."""
    versions = dict(line.strip().split("==") for line in (ROOT / "requirements.txt").read_text().splitlines() if "==" in line)
    login = next((s for s in perf or [] if s["name"] == "login"), None)
    return {"commit": os.getenv("GITHUB_SHA", "")[:7], "branch": os.getenv("GITHUB_REF_NAME", ""),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "pytest": versions.get("pytest"), "playwright": versions.get("playwright"),
            "loadFactor": round(login["requests"] / 200, 1) if login else None}


# ---------- the review ----------

def pct(part, whole, digits=0):
    return round(100 * part / whole, digits) if whole else 0.0


def metric(key, name, value, goal, status, note="", series=None, ratio=None):
    """One row of the review. series: values over time (sparkline); ratio: 0..1 for a meter."""
    return {"key": key, "name": name, "value": value, "goal": goal, "status": status, "note": note,
            "series": series or [], "ratio": ratio}


def callout(status, title, text):
    return {"status": status, "title": title, "text": text}


def tp99_trend(name: str, now_ms: float, history: list[list[dict]]):
    """(TP99 over the history + now, change vs the history's median in %, regressed?)"""
    past = [s["p99_ms"] for run in history for s in run if s["name"] == name]
    if not past:
        return [now_ms], None, False
    median = statistics.median(past)
    change = pct(now_ms - median, median) if median else 0
    regressed = len(past) >= 3 and now_ms > median * REGRESSION and now_ms - median >= REGRESSION_MIN_MS
    return past + [now_ms], change, regressed


def review(k: dict, history: list[list[dict]]) -> tuple[dict[str, list], list[dict], list[dict]]:
    """Builds the metric rows per section, the latency rows, and the callouts."""
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
        sections["output"].append(metric(
            "availability", "Availability under load", f"{availability:.2f}%", f"≥ {GOAL_AVAILABILITY}%",
            GOOD if availability >= GOAL_AVAILABILITY else CRITICAL,
            note=f"{total:,} requests · {pct(errors, budget):.0f}% of error budget used", ratio=availability / 100))
        timed = [s for s in perf if s.get("target_p99_ms")]
        within = sum(s["p99_ms"] <= s["target_p99_ms"] for s in timed)
        sections["output"].append(metric(
            "slo", "APIs within their TP99 SLO", f"{within} of {len(timed)}", "all",
            GOOD if within == len(timed) else CRITICAL, ratio=within / len(timed) if timed else None))
        if critical := next((s for s in perf if s["name"] == CRITICAL_PATH), None):
            series, change, regressed = tp99_trend(CRITICAL_PATH, critical["p99_ms"], history)
            status = CRITICAL if critical["p99_ms"] > critical["target_p99_ms"] else WARNING if regressed else GOOD
            note = "every user with their own key" + ("" if change is None else " · in line with recent median"
                                                      if round(change) == 0 else f" · {change:+.0f}% vs recent median")
            sections["output"].append(metric(
                "login", "Login TP99", f"{critical['p99_ms']:.1f} ms", f"≤ {critical['target_p99_ms']} ms",
                status, note=note, series=series))
        integrity = [s for s in perf if s["name"] in INTEGRITY_SCENARIOS]
        broken = sum(not s["passed"] for s in integrity)
        sections["output"].append(metric(
            "integrity", "Lost or duplicated records under load", str(broken), "0", GOOD if broken == 0 else CRITICAL,
            note="no lost transactions · no duplicate accounts"))
        for s in perf:
            series, change, regressed = tp99_trend(s["name"], s["p99_ms"], history)
            target = s.get("target_p99_ms")
            status = NONE if not target else CRITICAL if s["p99_ms"] > target else WARNING if regressed else GOOD
            latency.append({"name": s["name"], "requests": s["requests"], "p50": s["p50_ms"],
                            "p90": s.get("p90_ms"), "p99": s["p99_ms"], "p999": s.get("p999_ms"),
                            "target": target, "series": series, "change": change, "status": status})
            if regressed:
                callouts.append(callout(WARNING, f"{s['name']}: TP99 {change:+.0f}% vs its recent median",
                                        f"{s['p99_ms']:.1f} ms, still within its {target} ms SLO, but worth a look."))
    else:
        sections["output"].append(metric("load", "Load tests", "not run", "-", NONE,
                                         note="no performance-relevant file changed in this build"))

    # input metrics: what the team controls, and what drives the outputs above
    sections["input"].append(metric(
        "coverage", "Documented status codes tested", f"{cov['statusCodesTested']} of {cov['statusCodes']}",
        f"{GOAL_COVERAGE}%", GOOD if cov["statusCodesTested"] == cov["statusCodes"] else CRITICAL,
        note=f"{cov['endpointsTested']} of {cov['endpoints']} endpoints",
        ratio=cov["statusCodesTested"] / cov["statusCodes"] if cov["statusCodes"] else None))
    if cov["untested"]:
        callouts.append(callout(CRITICAL, "Untested documented responses", ", ".join(cov["untested"])))
    if tests:
        run = tests["tests"] - tests["skipped"]
        sections["input"].append(metric(
            "tests", "Functional tests passing", f"{tests['passed']} of {run}", f"{GOAL_PASS_RATE}%",
            GOOD if pct(tests["passed"], run) >= GOAL_PASS_RATE else CRITICAL,
            note=f"API + UI in parallel · {tests['seconds']} s", ratio=tests["passed"] / run if run else None))
        if tests["failed"]:
            callouts.append(callout(CRITICAL, f"{tests['failed']} functional test(s) failed", "See the API + UI tests job."))
    else:
        sections["input"].append(metric("tests", "Functional tests passing", "no results", f"{GOAL_PASS_RATE}%", CRITICAL))
        callouts.append(callout(CRITICAL, "No functional test results", "The API + UI tests job didn't finish."))
    severity = " · ".join(f"{n} {s}" for s, n in b["bySeverity"].items())
    sections["input"].append(metric("bugs", "Bugs caught before release", str(b["found"]), "tracked", NONE, note=severity))
    sections["input"].append(metric("open-critical", "Open critical bugs", str(b["openCritical"]), "0",
                                    GOOD if b["openCritical"] == 0 else CRITICAL))
    if b["openCritical"]:
        callouts.append(callout(CRITICAL, f"{b['openCritical']} critical bug(s) open", "See data/bugs.json."))

    # operational excellence: the health of main, from CI history
    if ops:
        rate = pct(ops["failed"], ops["runs"])
        sections["ops"].append(metric(
            "cfr", "Change failure rate", f"{rate:.0f}%", f"≤ {GOAL_CHANGE_FAILURE}%",
            GOOD if rate <= GOAL_CHANGE_FAILURE else CRITICAL, note=f"{ops['failed']} of {ops['runs']} merges broke main"))
        restore = ops["medianRestoreHours"]
        status = CRITICAL if ops["stillBroken"] else NONE if restore is None else GOOD if restore <= GOAL_RESTORE_HOURS else CRITICAL
        value = "main is red" if ops["stillBroken"] else "no failures" if restore is None else f"{restore} h"
        sections["ops"].append(metric("restore", "Time to restore main", value, f"≤ {GOAL_RESTORE_HOURS} h", status,
                                      note="median, first red run to next green"))
        sections["ops"].append(metric(
            "feedback", "Time from push to result", f"{ops['medianFeedbackMin']} min", f"≤ {GOAL_FEEDBACK_MIN} min",
            GOOD if ops["medianFeedbackMin"] <= GOAL_FEEDBACK_MIN else CRITICAL, note="median"))
        sections["ops"].append(metric("merges", "Merge frequency", f"{ops['mergesPerWeek']} / week", "tracked", NONE,
                                      note=f"{ops['runs']} merges in {ops['days']} days"))
        if ops["stillBroken"]:
            callouts.append(callout(CRITICAL, "main is broken", "The latest run on main failed."))

    for m in sections["output"] + sections["ops"]:
        if m["status"] == CRITICAL and m["key"] != "restore":
            callouts.append(callout(CRITICAL, f"{m['name']}: {m['value']}", f"Goal {m['goal']}."))
    return sections, latency, callouts


def links() -> dict:
    repo = os.getenv("GITHUB_REPOSITORY", DEFAULT_REPOSITORY)
    owner, name = repo.split("/")
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    run = os.getenv("GITHUB_RUN_ID")
    return {"repo": f"{server}/{repo}", "run": f"{server}/{repo}/actions/runs/{run}" if run else None,
            "dashboard": f"https://{owner}.github.io/{name}/kpi/", "allure": f"https://{owner}.github.io/{name}/",
            "definitions": f"{server}/{repo}/blob/main/docs/KPIs.md"}


def context() -> dict:
    return {"run": os.getenv("GITHUB_RUN_NUMBER"), "commit": os.getenv("GITHUB_SHA", "")[:7],
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "pr": os.getenv("GITHUB_EVENT_NAME") == "pull_request"}


SECTION_TITLES = {"output": ("Output metrics", "what customers feel"),
                  "input": ("Input metrics", "what the team controls"),
                  "ops": ("Operational excellence", "the health of main")}


# ---------- markdown: the CI run's summary page ----------

MD_STATUS = {GOOD: "✅", WARNING: "⚠️", CRITICAL: "❌", NONE: "➖"}


def to_markdown(sections: dict, latency: list, callouts: list, history: list) -> str:
    scored = [m for ms in sections.values() for m in ms if m["status"] != NONE]
    on_goal = sum(m["status"] == GOOD for m in scored)
    ctx, url = context(), links()
    verdict = MD_STATUS[GOOD if on_goal == len(scored) else CRITICAL]
    meta = " · ".join(x for x in [f"run #{ctx['run']}" if ctx["run"] else "local run",
                                  f"commit `{ctx['commit']}`" if ctx["commit"] else "", ctx["date"]] if x)
    lines = [f"## {verdict} QA metrics review: {on_goal} of {len(scored)} on goal", "",
             f"{meta} · [**Dashboard**]({url['dashboard']}) · [Definitions]({url['definitions']})", ""]
    if ctx["pr"]:
        lines += ["> Pull request runs use a third of main's load: compare latency with care.", ""]
    if callouts:
        lines += [f"> {MD_STATUS[c['status']]} **{c['title']}**. {c['text']}" for c in callouts] + [""]
    else:
        lines += ["> ✅ **No callouts.** Every metric is on goal and within its trend.", ""]

    for key, (title, subtitle) in SECTION_TITLES.items():
        if not sections[key]:
            continue
        lines += [f"| | **{title}** · {subtitle} | Now | Goal |", "|:-:|---|---|---|"]
        for m in sections[key]:
            now = f"**{m['value']}**"
            if len(m["series"]) > 1:
                now += f" {sparkline(m['series'])}"
            if m["note"]:
                now += f"<br><sub>{m['note']}</sub>"
            lines.append(f"| {MD_STATUS[m['status']]} | {m['name']} | {now} | {m['goal']} |")
        lines.append("")

    if latency:
        lines += [f"| | **API latency under load** · TP99 against its SLO | TP99 | SLO | Budget used | Trend (last {len(history) + 1}) |",
                  "|:-:|---|--:|--:|--:|---|"]
        for s in latency:
            used = f"{pct(s['p99'], s['target']):.0f}%" if s["target"] else "-"
            trend = sparkline(s["series"]) + (f" {s['change']:+.0f}%" if s["change"] is not None else "")
            lines.append(f"| {MD_STATUS[s['status']]} | {s['name']} | {s['p99']:.1f} ms | "
                         f"{s['target'] or '-'}{' ms' if s['target'] else ''} | {used} | {trend} |")
        lines.append("")
    lines.append("<sub>Not measured here: escaped defects, Sev-2 incidents and time to detect need production traffic; "
                 "flaky-test rate needs reruns of the same commit. See the definitions for how each would be measured.</sub>")
    return "\n".join(lines) + "\n"


def sparkline(values: list[float]) -> str:
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    bars = "▁▂▃▄▅▆▇█"
    return "".join(bars[round((v - low) / (high - low) * 7) if high > low else 3] for v in values)


# ---------- HTML: the dashboard published to GitHub Pages ----------

# 18px line icons (stroke = currentColor), drawn for this page
ICON = {
    "pulse": '<path d="M3 12h4l2.5-6 4 12 2.5-6H21"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "flask": '<path d="M9.5 3.5h5M10.5 3.5v5.2L5.6 17.6A2 2 0 0 0 7.4 20.5h9.2a2 2 0 0 0 1.8-2.9l-4.9-8.9V3.5"/><path d="M7.8 14.5h8.4"/>',
    "branch": '<circle cx="6.5" cy="5.5" r="2"/><circle cx="6.5" cy="18.5" r="2"/><circle cx="17.5" cy="7.5" r="2"/><path d="M6.5 7.5v9M17.5 9.5c0 4-5 4-9.5 7"/>',
    "shield": '<path d="M12 3.5l7 2.6v5.4c0 4.4-3 7.6-7 9-4-1.4-7-4.6-7-9V6.1z"/><path d="M8.8 12.2l2.2 2.2 4.2-4.4"/>',
    "code": '<path d="M9 8l-4 4 4 4M15 8l4 4-4 4"/>',
    "file": '<path d="M6.5 3.5h7l4 4v12a1 1 0 0 1-1 1h-10a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1z"/><path d="M13.5 3.5v4h4M8.5 12.5h7M8.5 16h7"/>',
    "grid": '<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>',
    "chart": '<path d="M4 4v16h16"/><path d="M7.5 15l3.5-4.5 3 2.5 5-6.5"/>',
    "report": '<path d="M6.5 3.5h7l4 4v12a1 1 0 0 1-1 1h-10a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1z"/><path d="M9 13.5l2 2 4-4"/>',
    "book": '<path d="M4 5.5c2.5-1.3 5.5-1.3 8 0 2.5-1.3 5.5-1.3 8 0v13c-2.5-1.3-5.5-1.3-8 0-2.5-1.3-5.5-1.3-8 0z"/><path d="M12 5.5v13"/>',
    "users": '<circle cx="9" cy="8.5" r="3"/><path d="M3.5 19c.6-3 2.8-4.5 5.5-4.5s4.9 1.5 5.5 4.5"/><circle cx="17" cy="9.5" r="2.4"/><path d="M16.5 14.6c2.2.1 3.6 1.4 4 3.9"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M15.5 15.5L20 20"/>',
    "chevron": '<path d="M9.5 6.5L15 12l-5.5 5.5"/>',
    "arrow": '<path d="M5 12h14M13.5 6.5L19 12l-5.5 5.5"/>',
    "down": '<path d="M12 5v14M6.5 13.5L12 19l5.5-5.5"/>',
    "up": '<path d="M12 19V5M6.5 10.5L12 5l5.5 5.5"/>',
}
STATUS_ICON = {
    GOOD: '<circle cx="12" cy="12" r="8.5"/><path d="M8.3 12.3l2.5 2.5 4.9-5.1"/>',
    WARNING: '<path d="M12 4l9 15.5H3z"/><path d="M12 10v4.2M12 17.2v.1"/>',
    CRITICAL: '<circle cx="12" cy="12" r="8.5"/><path d="M9 9l6 6M15 9l-6 6"/>',
    NONE: '<circle cx="12" cy="12" r="8.5"/><path d="M8.5 12h7"/>',
}
LABELS = {GOOD: "On goal", WARNING: "Watch", CRITICAL: "Missed", NONE: "Tracked"}
SOURCES = {   # where each number comes from: shown when a row in the table is opened
    "availability": "performance.json: successful requests ÷ all requests, across every load scenario.",
    "slo": "performance.json: load scenarios whose TP99 is within their SLO ÷ scenarios with an SLO.",
    "login": "performance.json: TP99 of the login scenario in which every user has their own API key; the trend "
             "compares it with the median of recent merges to main.",
    "integrity": "performance.json: transactions missing from history after a burst of writes, and accounts created "
                 "twice by 20 parallel sign-ups with one email.",
    "load": "Performance tests run only when a performance-relevant file changes.",
    "coverage": "The README's API table (documented endpoints and status codes) against api*.log (every request "
                "the tests sent).",
    "tests": "junit.xml from the API + UI test job.",
    "bugs": "data/bugs.json: bugs the tests found before release, with severity.",
    "open-critical": "data/bugs.json: critical bugs whose status isn't fixed.",
    "cfr": "GitHub Actions: merges to main whose run failed ÷ all merges (DORA).",
    "restore": "GitHub Actions: median time from the first red run on main to the next green one (DORA).",
    "feedback": "GitHub Actions: median minutes from a push to its run finishing.",
    "merges": "GitHub Actions: merges to main per week.",
}
GROUPS = {"output": "customer", "input": "engineering", "ops": "engineering"}

CSS = """
:root {
  color-scheme: light;
  --page: #f6f7f6; --side: #fbfbfa; --surface: #ffffff; --sunk: #f4f6f5;
  --ink: #111816; --ink-2: #4f5a56; --muted: #7d8783; --line: #e6e9e7; --line-2: #eef0ef;
  --accent: #0f8a5f; --accent-text: #0b6b49; --accent-wash: #e6f3ee; --baseline: #a7b6b1; --track: #edf0ef;
  --good: #0b6b49; --warning: #8f5600; --critical: #c0392b; --neutral: #7d8783;
  --shadow: 0 1px 2px rgba(17,24,22,0.04);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0f1211; --side: #121615; --surface: #161a19; --sunk: #1b201f;
    --ink: #eef2f0; --ink-2: #b4bdb9; --muted: #8b9692; --line: #262d2b; --line-2: #212725;
    --accent: #21a072; --accent-text: #4cc497; --accent-wash: #15302a; --baseline: #56645f; --track: #232a28;
    --good: #4cc497; --warning: #e8b34a; --critical: #f07a6b; --neutral: #8b9692;
    --shadow: none;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0f1211; --side: #121615; --surface: #161a19; --sunk: #1b201f;
  --ink: #eef2f0; --ink-2: #b4bdb9; --muted: #8b9692; --line: #262d2b; --line-2: #212725;
  --accent: #21a072; --accent-text: #4cc497; --accent-wash: #15302a; --baseline: #56645f; --track: #232a28;
  --good: #4cc497; --warning: #e8b34a; --critical: #f07a6b; --neutral: #8b9692;
  --shadow: none;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; -webkit-font-smoothing: antialiased; }
a { color: inherit; text-decoration: none; }
svg.i { width: 18px; height: 18px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.7;
        stroke-linecap: round; stroke-linejoin: round; }
.app { display: grid; grid-template-columns: 248px minmax(0, 1fr); min-height: 100vh;
        background: linear-gradient(to right, var(--side) 248px, var(--line) 248px 249px, var(--page) 249px); }

/* sidebar */
.side { background: var(--side); padding: 28px 18px 20px;
        display: flex; flex-direction: column; gap: 6px; position: sticky; top: 0; height: 100vh; }
.brand { display: flex; align-items: center; gap: 10px; padding: 0 8px 26px; font-size: 24px; font-weight: 700;
         letter-spacing: -0.02em; }
.brand .mark { width: 36px; height: 36px; border-radius: 9px; background: #12382c; color: #fff; display: grid;
               place-items: center; }
.brand .mark svg.i { width: 20px; height: 20px; stroke-width: 2; }
.brand b { color: var(--accent); }
.eyebrow { font-size: 11.5px; font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted);
           padding: 0 10px 8px; }
.nav a { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 10px; color: var(--ink-2);
         font-weight: 500; }
.nav a:hover { background: var(--sunk); color: var(--ink); }
.nav a.on { background: var(--accent-wash); color: var(--accent-text); }
.nav .count { margin-left: auto; font: 600 12px/1 ui-monospace, SFMono-Regular, Menlo, monospace; padding: 4px 7px;
              border-radius: 6px; background: var(--accent-wash); color: var(--accent-text); }
.side .spacer { flex: 1; }
.health { border: 1px solid var(--line); background: var(--surface); border-radius: 12px; padding: 14px 16px;
          font-size: 13.5px; color: var(--ink-2); }
.health strong { display: flex; align-items: center; gap: 8px; color: var(--ink); font-size: 14px; margin-bottom: 4px; }
.health .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--good); }
.health.bad .dot { background: var(--critical); }
.repo { display: flex; align-items: center; gap: 10px; padding: 14px 8px 0; font-size: 13px; color: var(--ink-2); }
.repo .avatar { width: 34px; height: 34px; border-radius: 50%; background: var(--sunk); border: 1px solid var(--line);
                display: grid; place-items: center; font-weight: 600; font-size: 12px; color: var(--ink-2); flex: none; }
.repo strong { display: block; color: var(--ink); font-size: 13.5px; }

/* main */
.main { padding: 28px 40px 48px; max-width: 1340px; width: 100%; }
.head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px 24px; flex-wrap: wrap;
        margin-bottom: 14px; }
h2 { font-size: 18px; font-weight: 650; margin: 0; letter-spacing: -0.01em; }
.head .meta, .meta { color: var(--muted); font-size: 13.5px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; box-shadow: var(--shadow); }
.callouts { display: grid; gap: 8px; margin-bottom: 18px; }
.callout { display: flex; gap: 10px; align-items: flex-start; padding: 12px 16px; font-size: 14px; }
.callout.warning svg.i { color: var(--warning); } .callout.critical svg.i { color: var(--critical); }

.signals { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-bottom: 20px; }
.signal { padding: 20px 22px 16px; display: flex; flex-direction: column; min-height: 196px; }
.signal .top { display: flex; justify-content: space-between; color: var(--ink-2); font-weight: 500; }
.signal .top svg.i { color: var(--muted); }
.big { font-size: 44px; font-weight: 700; letter-spacing: -0.03em; line-height: 1.1; margin-top: 14px; }
.big small { font-size: 0.5em; font-weight: 500; color: var(--muted); letter-spacing: 0; margin-left: 2px; }
.signal .sub { color: var(--ink-2); font-size: 14px; margin-top: 4px; display: flex; align-items: center; gap: 4px; }
.signal .sub svg.i { width: 15px; height: 15px; }
.signal .foot { margin-top: auto; padding-top: 14px; border-top: 1px solid var(--line-2); display: flex;
                justify-content: space-between; align-items: center; font-size: 13px; color: var(--muted); }
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 14px; font-weight: 500; white-space: nowrap; }
.status svg.i { width: 16px; height: 16px; }
.status.good { color: var(--good); } .status.warning { color: var(--warning); }
.status.critical { color: var(--critical); } .status.none { color: var(--neutral); }

.pair { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); gap: 20px; margin-bottom: 20px; }
.feature { padding: 26px 30px 26px; }
.feature .title { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px 16px;
                  flex-wrap: wrap; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 8px; background: var(--sunk);
        border: 1px solid var(--line); color: var(--ink-2); font-size: 13.5px; white-space: nowrap; }
.chip svg.i { width: 15px; height: 15px; }
.h3 { font-size: 18px; font-weight: 650; margin: 0; letter-spacing: -0.01em; }
.subtitle { color: var(--ink-2); font-size: 14px; margin-top: 2px; }
.feature .big { font-size: 42px; margin-top: 22px; }
.delta { display: flex; align-items: center; gap: 6px; font-size: 14px; font-weight: 500; margin-top: 2px; }
.delta svg.i { width: 15px; height: 15px; }
.delta.good { color: var(--good); } .delta.flat { color: var(--ink-2); } .delta.warning { color: var(--warning); }
.legend { display: flex; justify-content: flex-end; gap: 18px; font-size: 13px; color: var(--ink-2); margin: 6px 0 10px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.bars { display: grid; grid-template-columns: 120px minmax(0, 1fr) 82px; gap: 16px 18px; align-items: center; }
.bars .name { color: var(--ink-2); font-size: 15px; }
.bars .val { text-align: right; font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
.track { height: 12px; border-radius: 6px; background: var(--track); position: relative; outline: none; }
.track span { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 6px; }
.track:hover span, .track:focus span { filter: brightness(1.1); }
.ticks { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; grid-column: 2;
         font-variant-numeric: tabular-nums; margin-top: -6px; }
.note { display: flex; align-items: center; gap: 10px; margin-top: 22px; padding: 12px 16px; border-radius: 10px;
        background: var(--sunk); border: 1px solid var(--line); font-size: 14px; color: var(--ink-2); }
.note svg.i { color: var(--good); width: 17px; height: 17px; }
.note.critical svg.i { color: var(--critical); }
.note strong { color: var(--ink); font-weight: 600; }

.coverage { padding: 26px 30px 0; display: flex; flex-direction: column; }
.coverage .title { display: flex; justify-content: space-between; }
.coverage .title svg.i { color: var(--muted); }
.cov-list { margin-top: 18px; }
.cov { display: grid; grid-template-columns: 36px minmax(0, 1fr) auto; gap: 14px; align-items: center; padding: 16px 0;
       border-bottom: 1px solid var(--line-2); }
.cov:last-child { border-bottom: 0; }
.cov .ic { width: 36px; height: 36px; border-radius: 9px; background: var(--sunk); border: 1px solid var(--line);
           display: grid; place-items: center; color: var(--ink-2); }
.cov .ic svg.i { width: 17px; height: 17px; }
.cov strong { display: block; font-weight: 600; }
.cov .small { color: var(--ink-2); font-size: 13.5px; }
.cov .num { font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
.cov .num small { color: var(--muted); font-weight: 500; }
.cov-foot { margin: auto -30px 0; padding: 14px 30px; border-top: 1px solid var(--line); background: var(--sunk);
            border-radius: 0 0 14px 14px; display: flex; justify-content: space-between; font-size: 14px;
            color: var(--muted); }
.cov-foot a { color: var(--ink); font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }
.cov-foot a svg.i { width: 15px; height: 15px; }

/* the metrics table */
.detail-card { margin-bottom: 20px; }
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap;
               padding: 26px 30px 0; }
.filters { display: flex; gap: 10px; flex-wrap: wrap; }
.search { display: flex; align-items: center; gap: 8px; border: 1px solid var(--line); border-radius: 9px; padding: 0 12px;
          background: var(--surface); color: var(--muted); }
.search input { border: 0; outline: 0; background: transparent; color: var(--ink); font: inherit; font-size: 14px;
                padding: 9px 0; width: 190px; }
select { border: 1px solid var(--line); border-radius: 9px; background: var(--surface); color: var(--ink); font: inherit;
         font-size: 14px; padding: 9px 12px; }
.tabs { display: flex; gap: 28px; padding: 18px 30px 0; border-bottom: 1px solid var(--line); overflow-x: auto; }
.tabs button { all: unset; cursor: pointer; padding: 6px 0 12px; color: var(--ink-2); font-weight: 500; white-space: nowrap;
               border-bottom: 2px solid transparent; display: inline-flex; gap: 8px; align-items: center; }
.tabs button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tabs button.on { color: var(--ink); border-color: var(--accent); }
.tabs .count { font-size: 12px; padding: 1px 7px; border-radius: 6px; background: var(--sunk); color: var(--muted); }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-size: 12px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
     color: var(--muted); padding: 14px 30px; border-bottom: 1px solid var(--line); }
td { padding: 16px 30px; border-bottom: 1px solid var(--line-2); vertical-align: middle; }
tr.row { cursor: pointer; }
tr.row:hover td { background: var(--sunk); }
tr.row td.name strong { font-weight: 600; display: block; }
tr.row td.name span { color: var(--ink-2); font-size: 13.5px; }
td.cur { font-weight: 650; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.goal { color: var(--ink-2); white-space: nowrap; }
td.chev { width: 40px; color: var(--muted); text-align: right; }
td.chev svg.i { transition: transform .15s; }
tr.row.open td.chev svg.i { transform: rotate(90deg); }
tr.more td { background: var(--sunk); color: var(--ink-2); font-size: 14px; padding-top: 10px; padding-bottom: 14px; }
tr.more[hidden], tr.row[hidden] { display: none; }
.empty { padding: 22px 30px; color: var(--muted); }

/* per-API latency */
.latency { padding: 26px 30px 22px; }
.lat { display: grid; grid-template-columns: minmax(150px, 1fr) minmax(0, 3fr) 140px; gap: 16px; align-items: center;
       padding: 11px 0; border-bottom: 1px solid var(--line-2); }
.lat:last-of-type { border-bottom: 0; }
.lat .val { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; font-weight: 600; }
.lat .val span { color: var(--muted); font-weight: 400; }
.lat .track { height: 8px; }
.lat-axis { display: grid; grid-template-columns: minmax(150px, 1fr) minmax(0, 3fr) 140px; gap: 16px; color: var(--muted);
            font-size: 12px; margin-top: 6px; }
.lat-axis div:nth-child(2) { display: flex; justify-content: space-between; }
footer { color: var(--muted); font-size: 13px; margin-top: 18px; }
footer a { color: var(--accent-text); }

#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--line);
       border-radius: 10px; padding: 9px 12px; font-size: 13px; box-shadow: 0 6px 20px rgba(0,0,0,0.12); opacity: 0;
       transition: opacity .08s; z-index: 10; }
#tip strong { display: block; font-size: 15px; }
#tip span { display: block; color: var(--ink-2); }

@media (max-width: 1180px) { .signals { grid-template-columns: repeat(2, minmax(0, 1fr)); } .pair { grid-template-columns: 1fr; } }
@media (max-width: 860px) {
  .app { grid-template-columns: minmax(0, 1fr); background: var(--page); }
  .side { position: static; height: auto; flex-direction: row; flex-wrap: wrap; align-items: center; gap: 4px 6px;
          padding: 16px; border-right: 0; border-bottom: 1px solid var(--line); }
  .brand { padding: 0 12px 0 4px; font-size: 20px; }
  .eyebrow, .side .spacer, .repo { display: none; }
  .nav { display: flex; gap: 2px; flex-wrap: wrap; min-width: 0; }
  .nav a { padding: 8px 10px; }
  .health { width: 100%; margin-top: 8px; }
  .main { padding: 20px 16px 40px; }
}
@media (max-width: 560px) {
  .signals { grid-template-columns: 1fr; }
  .feature, .coverage, .latency { padding-left: 18px; padding-right: 18px; }
  .cov-foot { margin: auto -18px 0; padding: 14px 18px; }
  .detail-head, .tabs { padding-left: 18px; padding-right: 18px; }
  th, td { padding-left: 18px; padding-right: 18px; }
  th.goal, td.goal, th.chev, td.chev { display: none; }
  .search input { width: 140px; }
  .bars { grid-template-columns: 1fr auto; }
  .bars .track { grid-column: 1 / -1; }
  .ticks { grid-column: 1 / -1; }
  .lat, .lat-axis { grid-template-columns: 1fr auto; }
  .lat .track { grid-column: 1 / -1; grid-row: 2; }
  .lat-axis div:first-child, .lat-axis div:last-child { display: none; }
  .lat-axis { grid-template-columns: 1fr; }
}
"""

JS = """
const tip = document.getElementById('tip');
function show(el, x, y) {
  const [value, ...rest] = el.dataset.tip.split('|');
  tip.replaceChildren();
  const strong = document.createElement('strong'); strong.textContent = value; tip.append(strong);
  for (const line of rest) { const s = document.createElement('span'); s.textContent = line; tip.append(s); }
  tip.style.opacity = 1;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(x + 14, innerWidth - r.width - 8) + 'px';
  tip.style.top = Math.max(8, y - r.height - 10) + 'px';
}
for (const el of document.querySelectorAll('[data-tip]')) {
  el.addEventListener('pointermove', e => show(el, e.clientX, e.clientY));
  el.addEventListener('pointerleave', () => tip.style.opacity = 0);
  el.addEventListener('focus', () => { const r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top); });
  el.addEventListener('blur', () => tip.style.opacity = 0);
}

// the metrics table: tabs, search and status filter work together; a row opens to show where its number comes from
let group = 'all';
const search = document.getElementById('q'), statusSel = document.getElementById('status');
function filter() {
  const q = search.value.trim().toLowerCase(), st = statusSel.value;
  let shown = 0;
  for (const row of document.querySelectorAll('tr.row')) {
    const ok = (group === 'all' || row.dataset.group === group) && (st === 'all' || row.dataset.status === st)
               && row.dataset.name.includes(q);
    row.hidden = !ok; shown += ok;
    const more = row.nextElementSibling;
    more.hidden = !ok || !row.classList.contains('open');
  }
  document.getElementById('none').hidden = shown > 0;
}
for (const b of document.querySelectorAll('.tabs button')) {
  b.addEventListener('click', () => {
    document.querySelectorAll('.tabs button').forEach(x => { x.classList.remove('on'); x.setAttribute('aria-selected', 'false'); });
    b.classList.add('on'); b.setAttribute('aria-selected', 'true'); group = b.dataset.group; filter();
  });
}
search.addEventListener('input', filter);
statusSel.addEventListener('change', filter);
for (const row of document.querySelectorAll('tr.row')) {
  const toggle = () => { row.classList.toggle('open'); row.setAttribute('aria-expanded', row.classList.contains('open')); filter(); };
  row.addEventListener('click', toggle);
  row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
}
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def icon(name: str) -> str:
    return f'<svg class="i" viewBox="0 0 24 24" aria-hidden="true">{ICON[name]}</svg>'


def status(s: str) -> str:
    return (f'<span class="status {s}"><svg class="i" viewBox="0 0 24 24" aria-hidden="true">{STATUS_ICON[s]}</svg>'
            f'{LABELS[s]}</span>')


def nice_max(value: float) -> float:
    """The smallest round axis end at least 15% above value: ... 10, 20, 25, 50, 100, 200, 250 ..."""
    candidates = sorted(step * 10 ** power for step in (1, 2, 2.5, 5) for power in range(-1, 6))
    return next((top for top in candidates if top >= value * 1.15), value)


ICON.update({
    "suitcase": '<rect x="3.5" y="7" width="17" height="12.5" rx="2"/><path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7M3.5 12h17M10.5 12v2h3v-2"/>',
    "history": '<path d="M4 19h16"/><path d="M5 15l4-4 3 2.5 6-6.5"/>',
    "server": '<rect x="4" y="4.5" width="16" height="6" rx="1.5"/><rect x="4" y="13.5" width="16" height="6" rx="1.5"/><path d="M7.5 7.5h.01M7.5 16.5h.01"/>',
    "gauge": '<path d="M4.5 16a7.5 7.5 0 1 1 15 0"/><path d="M12 16l3.5-4"/>',
    "monitor": '<rect x="3.5" y="4.5" width="17" height="11.5" rx="1.5"/><path d="M9 20h6M12 16v4"/>',
    "download": '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
    "github": '<path d="M9 19c-4.3 1.4-4.3-2.5-6-3m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 0 0-1.3-3.2 4.2 4.2 0 0 0-.1-3.2s-1.1-.3-3.5 1.3a12.3 12.3 0 0 0-6.2 0C6.5 2.8 5.4 3.1 5.4 3.1a4.2 4.2 0 0 0-.1 3.2A4.6 4.6 0 0 0 4 9.5c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21"/>',
    "moon": '<path d="M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10z"/>',
    "bug": '<rect x="7.5" y="7.5" width="9" height="12" rx="4.5"/><path d="M12 11v8.5M7.5 13H4M20 13h-3.5M5 8.5l2.8 1.6M19 8.5l-2.8 1.6M5 18.5l2.8-1.8M19 18.5l-2.8-1.8M9.5 7.5a2.5 2.5 0 0 1 5 0"/>',
})
SUITE_INFO = {"api": ("API tests", "code"), "ui": ("UI tests", "monitor"), "perf": ("Performance tests", "gauge")}

CSS += """
/* overview: top bar, page header, run status */
.main { padding-top: 0; }
.topbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin: 0 -40px 30px;
          padding: 18px 40px; border-bottom: 1px solid var(--line); }
.crumbs { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 15px; }
.crumbs b { color: var(--ink); font-weight: 500; }
.crumbs svg.i { width: 14px; height: 14px; }
.toplinks { display: flex; align-items: center; gap: 18px; font-size: 14.5px; color: var(--ink-2); }
.toplinks a:hover { color: var(--ink); }
.runchip { display: inline-flex; align-items: center; gap: 8px; padding: 7px 12px; border: 1px solid var(--line);
           border-radius: 9px; font: 500 13.5px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--ink); }
.runchip svg.i { width: 16px; height: 16px; }
.hero { display: flex; justify-content: space-between; align-items: flex-end; gap: 18px 28px; flex-wrap: wrap;
        margin-bottom: 26px; }
.kicker { font-size: 12.5px; font-weight: 600; letter-spacing: 0.22em; text-transform: uppercase; color: var(--muted); }
h1 { font-size: 44px; line-height: 1.1; font-weight: 750; letter-spacing: -0.035em; margin: 10px 0 12px; }
.when { color: var(--ink-2); font-size: 15px; display: flex; gap: 10px; flex-wrap: wrap; }
.when .sep { color: var(--muted); }
.when code { font: 500 14px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--ink); }
.actions { display: flex; gap: 12px; flex-wrap: wrap; }
.btn { display: inline-flex; align-items: center; gap: 9px; padding: 11px 18px; border-radius: 11px; font-weight: 600;
       border: 1px solid var(--line); background: var(--surface); color: var(--ink); }
.btn:hover { background: var(--sunk); }
.btn.primary { background: var(--btn); color: var(--btn-ink); border-color: transparent; }
.btn.primary:hover { filter: brightness(1.05); background: var(--btn); }
.banner { display: flex; align-items: center; gap: 18px; padding: 22px 26px; margin-bottom: 22px;
          background: var(--accent-wash); border: 1px solid var(--banner-line); border-radius: 14px; }
.banner.bad { background: var(--bad-wash); border-color: var(--bad-line); }
.banner .badge { width: 46px; height: 46px; border-radius: 50%; display: grid; place-items: center; flex: none;
                 background: var(--surface); color: var(--good); border: 1px solid var(--banner-line); }
.banner.bad .badge { color: var(--critical); border-color: var(--bad-line); }
.banner .badge svg.i { width: 22px; height: 22px; stroke-width: 2; }
.banner .what { flex: 1; }
.banner strong { display: block; font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
.banner span.sub { color: var(--ink-2); font-size: 15px; }
.state { display: inline-flex; align-items: center; gap: 8px; font: 600 13px ui-monospace, SFMono-Regular, Menlo, monospace;
         letter-spacing: 0.06em; color: var(--good); }
.state i { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
.banner.bad .state { color: var(--critical); }
.run-tiles .signal:first-child { background: var(--accent-wash); border-color: var(--banner-line); }
.run-tiles .signal:first-child .big { color: var(--accent-text); }
.mini { height: 4px; border-radius: 2px; background: var(--track); margin-top: 12px; }
.mini span { display: block; height: 100%; border-radius: 2px; background: var(--accent); }
.run-tiles .signal { min-height: 176px; }

/* run history chart */
.section-title { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.chart { width: 100%; height: auto; display: block; margin-top: 18px; overflow: visible; }
.chart .grid { stroke: var(--line); stroke-width: 1; }
.chart .ytick, .chart .xtick { fill: var(--muted); font-size: 12px; font-family: system-ui, sans-serif; }
.chart .xtick.now { fill: var(--accent-text); font-weight: 650; }
.chart .nowpill { fill: var(--accent-wash); stroke: var(--banner-line); }
.chart .area { fill: var(--accent); opacity: 0.1; }
.chart .line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.chart .pt { fill: var(--accent); stroke: var(--surface); stroke-width: 2; pointer-events: none; }
.chart .hit { fill: transparent; cursor: default; outline: none; }
.chart .hit:hover + .pt, .chart .hit:focus + .pt { r: 6; }
.chart .endlabel { fill: var(--ink); font-size: 12.5px; font-weight: 650; font-family: system-ui, sans-serif; }
.card-foot { margin: 18px -30px 0; padding: 14px 30px; border-top: 1px solid var(--line); background: var(--sunk);
             border-radius: 0 0 14px 14px; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
             font-size: 14px; color: var(--ink-2); }
.card-foot strong { color: var(--ink); }
.card-foot a { color: var(--accent-text); font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }
.card-foot a svg.i { width: 15px; height: 15px; }
.panel { padding: 26px 30px 0; display: flex; flex-direction: column; }
.panel > .card-foot { margin-top: auto; }

/* suites */
.suite { display: grid; grid-template-columns: 38px minmax(0, 1fr) auto; gap: 6px 14px; align-items: center;
         padding: 16px 0; border-bottom: 1px solid var(--line-2); }
.suite:last-of-type { border-bottom: 0; }
.suite .ic { width: 38px; height: 38px; border-radius: 10px; background: var(--sunk); border: 1px solid var(--line);
             display: grid; place-items: center; color: var(--ink-2); grid-row: span 2; }
.suite strong { display: block; font-weight: 600; }
.suite code { font: 13px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted); }
.suite .num { font-weight: 700; font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
.suite .num small { color: var(--muted); font-weight: 500; }
.suite .bar { grid-column: 2 / -1; height: 6px; border-radius: 3px; background: var(--track); }
.suite .bar span { display: block; height: 100%; border-radius: 3px; background: var(--accent); }
.suite .meta { grid-column: 2 / -1; font-size: 13px; color: var(--ink-2); }
.pill { font-size: 13px; padding: 4px 10px; border: 1px solid var(--line); border-radius: 8px; color: var(--ink-2);
        white-space: nowrap; }

/* environment and bugs */
.env { display: grid; grid-template-columns: minmax(120px, auto) 1fr; margin: 18px 0 26px; }
.env dt, .env dd { padding: 11px 0; border-bottom: 1px solid var(--line-2); margin: 0; font-size: 14.5px; }
.env dt { color: var(--ink-2); padding-right: 16px; }
.env dd { font-weight: 500; }
.env dd code { font: 13.5px ui-monospace, SFMono-Regular, Menlo, monospace; }
.env dt:nth-last-of-type(1), .env dd:last-of-type { border-bottom: 0; }
.sev { display: grid; grid-template-columns: 80px minmax(0, 1fr) 28px; gap: 12px; align-items: center; padding: 9px 0; }
.sev .name { color: var(--ink-2); text-transform: capitalize; }
.sev .track { height: 8px; }
.sev .n { text-align: right; font-weight: 650; font-variant-numeric: tabular-nums; }
.bugs-big { display: flex; align-items: baseline; gap: 14px; margin: 16px 0 8px; }
.bugs-big .big { margin: 0; font-size: 40px; }

/* theme switch */
.theme { display: flex; align-items: center; gap: 12px; padding: 14px 10px 4px; color: var(--ink-2); font-size: 14px; }
.theme span { flex: 1; }
.switch { all: unset; cursor: pointer; width: 42px; height: 24px; border-radius: 12px; background: var(--track);
          border: 1px solid var(--line); position: relative; flex: none; }
.switch::after { content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%;
                 background: var(--muted); transition: transform .15s; }
.switch[aria-checked="true"] { background: var(--accent-wash); border-color: var(--banner-line); }
.switch[aria-checked="true"]::after { transform: translateX(18px); background: var(--accent); }
.switch:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.nav .sep { height: 1px; background: var(--line); margin: 10px 10px; }
section[id], .pair[id] { scroll-margin-top: 16px; }

:root { --btn: #0f8a5f; --btn-ink: #ffffff; --banner-line: #bfe0d2; --bad-wash: #fbeceb; --bad-line: #f0c4bf; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --btn: #7fd8aa; --btn-ink: #0d2a1f; --banner-line: #24503f; --bad-wash: #2a1716; --bad-line: #5a2925; }
}
:root[data-theme="dark"] { --btn: #7fd8aa; --btn-ink: #0d2a1f; --banner-line: #24503f; --bad-wash: #2a1716; --bad-line: #5a2925; }

@media (max-width: 860px) {
  .topbar { margin: 0 -16px 22px; padding: 14px 16px; }
  .toplinks a.text { display: none; }
  h1 { font-size: 34px; }
  .theme { display: none; }
}
@media (max-width: 560px) {
  .panel { padding: 22px 18px 0; }
  .card-foot { margin: 18px -18px 0; padding: 14px 18px; }
  .banner { padding: 18px; gap: 14px; flex-wrap: wrap; }
}
"""

JS += """
// dark / light switch: follows the system until chosen, then remembers the choice (when storage is available)
const root = document.documentElement, sw = document.getElementById('theme');
const isDark = () => root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
const sync = () => sw && sw.setAttribute('aria-checked', String(isDark()));
sync();
if (sw) sw.addEventListener('click', () => {
  root.dataset.theme = isDark() ? 'light' : 'dark';
  try { localStorage.setItem('qa-theme', root.dataset.theme); } catch (e) {}
  sync();
});
"""
THEME_BOOT = "try{const t=localStorage.getItem('qa-theme');if(t)document.documentElement.dataset.theme=t}catch(e){}"


def duration(seconds: float) -> tuple[str, str]:
    """(number, unit) for a tile: 21.6 s, or 1m 05s."""
    if seconds < 60:
        return f"{seconds:.1f}", " s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}", "s"


def suite_status(suite: dict) -> str:
    """A suite passed or it didn't: the same icon + label as a status, with its own words."""
    return status(GOOD if not suite["failed"] else CRITICAL).replace(
        LABELS[GOOD if not suite["failed"] else CRITICAL], "Passed" if not suite["failed"] else f'{suite["failed"]} failed')


def history_chart(points: list[dict]) -> str:
    """Tests passed per merge to main: area + line, a point per run with a hover/focus tooltip, this run labeled."""
    w, h, left, right, top, bottom = 640, 270, 44, 26, 30, 46
    peak = max(p["total"] for p in points) or 1
    step = 50 if peak > 60 else 10
    ymax = -(-peak // step) * step
    x = lambda i: left + (i * (w - left - right) / (len(points) - 1) if len(points) > 1 else (w - left - right) / 2)
    y = lambda v: top + (1 - v / ymax) * (h - top - bottom)
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Tests passed per merge to main, '
           f'{esc(", ".join(f"run {p['run']}: {p['passed']} passed" for p in points))}">']
    for v in range(0, ymax + 1, step):
        out.append(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                   f'<text class="ytick" x="{left - 10}" y="{y(v) + 4:.1f}" text-anchor="end">{v}</text>')
    coords = [(x(i), y(p["passed"])) for i, p in enumerate(points)]
    line = " ".join(f"{a:.1f},{b:.1f}" for a, b in coords)
    if len(coords) > 1:
        out.append(f'<path class="area" d="M{coords[0][0]:.1f},{y(0):.1f} L{line.replace(" ", " L")} L{coords[-1][0]:.1f},{y(0):.1f} Z"/>')
        out.append(f'<polyline class="line" points="{line}"/>')
    for (cx, cy), p in zip(coords, points):
        failed = p["total"] - p["passed"]
        tip = f'{p["passed"]} passed|run #{p["run"]}{" · this run" if p.get("current") else ""}|' + \
              (f"{failed} failed or skipped" if failed else "no failures")
        out.append(f'<circle class="hit" cx="{cx:.1f}" cy="{cy:.1f}" r="14" tabindex="0" data-tip="{esc(tip)}"/>'
                   f'<circle class="pt" cx="{cx:.1f}" cy="{cy:.1f}" r="{5 if p.get("current") else 4}"/>')
        label = f'#{p["run"]}'
        if p.get("current"):
            out.append(f'<rect class="nowpill" x="{cx - 22:.1f}" y="{h - 30}" width="44" height="24" rx="6"/>')
        out.append(f'<text class="xtick{" now" if p.get("current") else ""}" x="{cx:.1f}" y="{h - 13}" text-anchor="middle">{esc(label)}</text>')
    lx, ly = coords[-1]
    out.append(f'<text class="endlabel" x="{lx:.1f}" y="{ly - 12:.1f}" text-anchor="middle">{points[-1]["passed"]}</text>')
    out.append('</svg>')
    return "".join(out)


def to_html(k: dict, sections: dict, latency: list, callouts: list, history: list) -> str:
    ctx, url = context(), links()
    rows = [m for ms in sections.values() for m in ms]
    by_key = {m["key"]: m for m in rows}
    scored = [m for m in rows if m["status"] != NONE]
    on_goal = sum(m["status"] == GOOD for m in scored)
    perf, ops, cov, bugs_, env = k["performance"], k["operational"], k["coverage"], k["bugs"], k["environment"]
    suite_list, trend = k["suites"], k["trend"]
    total = sum(s["tests"] for s in suite_list)
    failed = sum(s["failed"] for s in suite_list)
    skipped = sum(s["skipped"] for s in suite_list)
    passed = total - failed - skipped
    green_suites = sum(not s["failed"] for s in suite_list)
    healthy = failed == 0 and on_goal == len(scored) and not (ops or {}).get("stillBroken")
    run_label = f"run #{ctx['run']}" if ctx["run"] else "this local run"
    login = next((s for s in latency if s["name"] == CRITICAL_PATH), None)
    names = [{"api": "API", "ui": "UI", "perf": "performance"}.get(s["key"], s["key"]) for s in suite_list]
    suite_names = ", ".join(names[:-1]) + (f" and {names[-1]}" if len(names) > 1 else names[0] if names else "")
    out = []

    # ---- sidebar ----
    out.append(f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>QA Metrics Review</title>
<script>{THEME_BOOT}</script><style>{CSS}</style></head><body><div class="app">
<aside class="side">
  <div class="brand"><span class="mark">{icon("pulse")}</span>quality<b>.</b></div>
  <div class="eyebrow">This project</div>
  <nav class="nav">
    <a class="on" href="#overview">{icon("grid")}Overview</a>
    <a href="#suites">{icon("suitcase")}Test suites<span class="count">{len(suite_list)}</span></a>
    <a href="#history">{icon("history")}Run history</a>
    <a href="#metrics">{icon("chart")}Metrics<span class="count">{len(rows)}</span></a>
    <a href="#latency">{icon("gauge")}API latency</a>
    <a href="#environment">{icon("server")}Environment</a>
    <div class="sep"></div>
    <a href="{esc(url["allure"])}">{icon("report")}Test report</a>
    <a href="{esc(url["definitions"])}">{icon("book")}Definitions</a>
  </nav>
  <div class="spacer"></div>
  <div class="health{"" if healthy else " bad"}"><strong><span class="dot"></span>
    {"All checks passed" if healthy else "Needs attention"}</strong>
    {f"{run_label.capitalize()}: {total} tests, {'zero failures' if not failed else f'{failed} failed'}, all {len(scored)} goals met."
     if healthy else f"{run_label.capitalize()}: {failed} failed tests, {len(scored) - on_goal} of {len(scored)} goals not met."}</div>
  <div class="theme">{icon("moon")}<span>Dark appearance</span>
    <button class="switch" id="theme" role="switch" aria-checked="false" aria-label="Dark appearance"></button></div>
  <a class="repo" href="{esc(url["repo"])}"><span class="avatar">QA</span>
    <span><strong>qa-automation-framework</strong>ai-musician on GitHub</span></a>
</aside>
<main class="main" id="overview">''')

    # ---- top bar and page header ----
    run_chip = (f'<a class="runchip" href="{esc(url["run"])}">{icon("github")}run #{esc(ctx["run"])}</a>'
                if url["run"] else "")
    out.append(f'''<div class="topbar"><div class="crumbs">QA automation {icon("chevron")}<b>Overview</b></div>
  <div class="toplinks"><a class="text" href="{esc(url["allure"])}">Allure test report</a>{run_chip}</div></div>
<div class="hero"><div><div class="kicker">A clearer picture of quality</div><h1>Quality overview</h1>
  <div class="when"><span>{esc(ctx["date"])}</span>{f'<span class="sep">·</span><code>{esc(ctx["commit"])}</code>' if ctx["commit"] else ""}
  {f'<span class="sep">·</span><span>{esc(run_label)}</span>' if ctx["run"] else ""}
  {'<span class="sep">·</span><span>pull request: a third of main&#39;s load</span>' if ctx["pr"] else ""}</div></div>
  <div class="actions">{f'<a class="btn" href="{esc(url["run"])}">{icon("file")}Run details</a>' if url["run"] else ""}
    <a class="btn primary" href="kpi.json" download>{icon("download")}Export data</a></div></div>''')

    # ---- run status ----
    if not suite_list:
        banner = ("bad", CRITICAL, "No test results in this run.", "The test jobs didn't produce JUnit results.", "NO RESULTS")
    elif failed:
        bad = [SUITE_INFO.get(s["key"], (s["key"],))[0] for s in suite_list if s["failed"]]
        banner = ("bad", CRITICAL, f"{failed} of {total} tests failed.", f"Failures in: {', '.join(bad)}.", "FAILURES")
    else:
        perf_note = "" if perf else " Load tests didn't run in this build."
        banner = ("", GOOD, f"All {total} tests passed. Every suite is green.",
                  f"{suite_names} suites completed successfully.{perf_note}", "COMPLETED")
    cls, st, title, sub, state = banner
    out.append(f'''<div class="banner {cls}"><span class="badge"><svg class="i" viewBox="0 0 24 24" aria-hidden="true">{STATUS_ICON[st]}</svg></span>
  <div class="what"><strong>{esc(title)}</strong><span class="sub">{esc(sub)}</span></div>
  <span class="state"><i></i>{state}</span></div>''')

    if callouts:
        out.append('<div class="callouts">' + "".join(
            f'<div class="card callout {c["status"]}"><svg class="i" viewBox="0 0 24 24" aria-hidden="true">'
            f'{STATUS_ICON[c["status"]]}</svg><div><strong>{esc(c["title"])}.</strong> {esc(c["text"])}</div></div>'
            for c in callouts) + '</div>')

    # ---- run tiles ----
    def tile(title, ico, big, unit, sub, extra=""):
        return (f'<div class="card signal"><div class="top">{esc(title)}{icon(ico)}</div>'
                f'<div class="big">{big}<small>{unit}</small></div><div class="sub">{sub}</div>{extra}</div>')

    run_count = total - skipped
    rate = pct(passed, run_count, 1) if run_count else 0
    functional_s = sum(s["seconds"] for s in suite_list if s["key"] != "perf")
    perf_s = sum(s["seconds"] for s in suite_list if s["key"] == "perf")
    num, unit = duration(functional_s + perf_s)
    out.append('<section class="signals run-tiles" aria-label="This run">')
    out.append(tile("Pass rate", "report", f"{rate:g}", "%", "No failed tests" if not failed else f"{failed} failed",
                    f'<div class="mini"><span style="width:{rate:.1f}%"></span></div>'))
    out.append(tile("Tests passed", "flask", str(passed), f" / {run_count}", "Across all test suites"))
    out.append(tile("Time in tests", "clock", num, unit,
                    f"API + UI {functional_s:.1f} s" + (f" · load {perf_s:.1f} s" if perf_s else "")))
    out.append(tile("Test suites", "suitcase", str(green_suites), f" / {len(suite_list)}",
                    "Every suite passed" if green_suites == len(suite_list) else f"{len(suite_list) - green_suites} with failures"))
    out.append('</section>')

    # ---- run history + suite breakdown ----
    out.append('<div class="pair">')
    current = trend[-1] if trend else None
    out.append(f'''<section class="card panel" id="history">
  <div class="section-title"><div><div class="h3">Consistency across runs</div>
    <div class="subtitle">Tests passed per merge to main, last {len(trend)} runs · from the published report's history</div></div>
    <div class="legend" style="margin:0"><span><i style="background:var(--accent)"></i>Passed</span></div></div>
  {history_chart(trend) if trend else ""}
  <div class="card-foot"><span>Latest result <strong>{current["passed"] if current else "-"} passed</strong></span>
    <a href="{esc(url["allure"])}#graph">Open the trend in Allure{icon("arrow")}</a></div>
</section>''')
    biggest = max((s["tests"] for s in suite_list), default=1)
    suite_rows = "".join(
        f'<div class="suite"><span class="ic">{icon(SUITE_INFO.get(s["key"], (s["key"], "code"))[1])}</span>'
        f'<div><strong>{esc(SUITE_INFO.get(s["key"], (s["key"],))[0])}</strong><code>tests.{esc(s["key"])}</code></div>'
        f'<div class="num">{s["tests"]}<small> tests</small></div>'
        f'<div class="bar" role="img" aria-label="{s["tests"]} of {total} tests"><span style="width:{s["tests"] / biggest * 100:.1f}%"></span></div>'
        f'<div class="meta">{suite_status(s)} · {s["seconds"]:.1f} s'
        f'{f" · {s['skipped']} skipped" if s["skipped"] else ""}</div></div>'
        for s in suite_list)
    out.append(f'''<section class="card panel" id="suites">
  <div class="section-title"><div><div class="h3">Test suite breakdown</div>
    <div class="subtitle">{total} checks, {len(suite_list)} layers of confidence</div></div><span class="pill">{len(suite_list)} suites</span></div>
  <div style="margin-top:8px">{suite_rows}</div>
  <div class="card-foot">{status(GOOD if green_suites == len(suite_list) else CRITICAL).replace("On goal", "All suites passed" if green_suites == len(suite_list) else "Failures")}
    <a href="{esc(url["allure"])}#suites">Explore suites{icon("arrow")}</a></div>
</section></div>''')

    # ---- key signals ----
    out.append(f'<div class="head" style="margin-top:6px"><h2>The key signals</h2><div class="meta">compared with your goals</div></div>'
               '<section class="signals" aria-label="Key signals">')

    def signal(title, ico, big, unit, sub, m):
        return (f'<div class="card signal"><div class="top">{esc(title)}{icon(ico)}</div>'
                f'<div class="big">{big}<small>{unit}</small></div><div class="sub">{sub}</div>'
                f'<div class="foot"><span>Goal {esc(m["goal"])}</span>{status(m["status"])}</div></div>')

    def missing(title, ico, goal):
        return (f'<div class="card signal"><div class="top">{title}{icon(ico)}</div><div class="big">–</div>'
                f'<div class="sub">Load tests didn\'t run in this build</div>'
                f'<div class="foot"><span>Goal {goal}</span>{status(NONE)}</div></div>')

    if perf and (m := by_key.get("availability")):
        errors = sum(s["errors"] for s in perf)
        out.append(signal("Availability under load", "pulse", m["value"].rstrip("%"), "%",
                          f"{sum(s['requests'] for s in perf):,} requests · {'no errors' if not errors else f'{errors:,} errors'}", m))
    else:
        out.append(missing("Availability under load", "pulse", f"≥ {GOAL_AVAILABILITY}%"))
    if login and (m := by_key.get("login")):
        sub = "first measurement" if login["change"] is None else "in line with recent median" if round(login["change"]) == 0 else \
            f'{icon("down" if login["change"] < 0 else "up")}{abs(login["change"]):.0f}% {"below" if login["change"] < 0 else "above"} recent median'
        out.append(signal("Login TP99 latency", "clock", f'{login["p99"]:.1f}', " ms", sub, m))
    else:
        out.append(missing("Login TP99 latency", "clock", "≤ 250 ms"))
    if (m := by_key.get("coverage")):
        tested, of = m["value"].split(" of ")
        out.append(signal("Status codes tested", "file", tested, f" / {of}", f"across all {cov['endpoints']} endpoints", m))
    if ops and (m := by_key.get("cfr")):
        out.append(signal("Change failure rate", "branch", m["value"].rstrip("%"), "%",
                          f'{ops["failed"]} of {ops["runs"]} merges broke main', m))
    out.append('</section>')
    return to_html_rest(out, k, sections, latency, history, by_key, rows, login, cov, bugs_, env, run_label, url)


def to_html_rest(out, k, sections, latency, history, by_key, rows, login, cov, bugs_, env, run_label, url) -> str:
    perf = k["performance"]
    # ---- featured: login latency, this run against the recent median ----
    out.append('<div class="pair">')
    if login:
        median = statistics.median(login["series"][:-1]) if len(login["series"]) > 1 else None
        top = nice_max(max(login["p99"], median or 0))
        login_req = next((s for s in perf if s["name"] == CRITICAL_PATH), {})
        users, parallel = login_req.get("requests", 0), login_req.get("concurrency", 0)
        if login["change"] is None:
            delta = '<div class="delta flat">first measurement: no trend yet</div>'
        elif round(login["change"]) == 0:
            delta = '<div class="delta flat">in line with the recent median</div>'
        elif login["change"] < 0:
            delta = f'<div class="delta good">{icon("down")}{abs(login["change"]):.0f}% below recent median</div>'
        else:
            cls = "warning" if login["status"] == WARNING else "flat"
            delta = f'<div class="delta {cls}">{icon("up")}{login["change"]:.0f}% above recent median</div>'
        headroom = 100 - pct(login["p99"], login["target"])
        within = login["p99"] <= login["target"]
        bars = [("Current run", login["p99"], "var(--accent)",
                 f'{login["p99"]:.1f} ms TP99|this run|TP50 {login["p50"]:.1f} · TP90 {login["p90"]:.1f} ms')]
        if median is not None:
            bars.append(("Recent median", median, "var(--baseline)",
                         f'{median:.1f} ms TP99|median of the last {len(login["series"]) - 1} merges to main'))
        bar_html = "".join(
            f'<div class="name">{name}</div><div class="track" tabindex="0" data-tip="{esc(tipt)}">'
            f'<span style="width:{min(100, value / top * 100):.1f}%;background:{color}"></span></div>'
            f'<div class="val">{"≈ " if name != "Current run" else ""}{value:.1f} ms</div>'
            for name, value, color, tipt in bars)
        ticks = "".join(f"<span>{t:g}{' ms' if i in (0, 5) else ''}</span>" for i, t in
                        enumerate([top * f for f in (0, 0.2, 0.4, 0.6, 0.8, 1)]))
        legend = ('<div class="legend"><span><i style="background:var(--accent)"></i>Current</span>'
                  + ('<span><i style="background:var(--baseline)"></i>Recent median</span>' if median is not None else "")
                  + '</div>')
        headline = ("Login is faster than usual" if round(login["change"] or 0) < 0 else
                    "Login is slower than usual" if login["status"] == WARNING else "Login latency")
        out.append(f'''<section class="card feature">
  <div class="title"><div><div class="h3">{headline}</div><div class="subtitle">TP99 response time · lower is better</div></div>
    <span class="chip">{icon("users")}{users:,} users · {parallel} at a time</span></div>
  <div class="big">{login["p99"]:.1f}<small> ms</small></div>{delta}
  {legend}
  <div class="bars">{bar_html}<div class="ticks">{ticks}</div></div>
  <div class="note{"" if within else " critical"}"><svg class="i" viewBox="0 0 24 24" aria-hidden="true">{STATUS_ICON[GOOD if within else CRITICAL]}</svg>
    <span><strong>{f"{headroom:.1f}% below the latency limit." if within else "Over the latency limit."}</strong>
    Your goal is ≤ {login["target"]} ms.</span></div>
</section>''')

    items = []
    if (m := by_key.get("slo")):
        within, total = m["value"].split(" of ")
        items.append(("code", "API latency SLOs", "All APIs within their TP99 limit" if within == total
                      else f"{int(total) - int(within)} over their TP99 limit", within, f" / {total}"))
    items.append(("file", "Documented status codes", f"Across all {cov['endpoints']} endpoints",
                  str(cov["statusCodesTested"]), f" / {cov['statusCodes']}"))
    if (m := by_key.get("integrity")):
        items.append(("shield", "Data integrity", "No lost or duplicated records" if m["value"] == "0"
                      else "Lost or duplicated records found", m["value"], " issues"))
    cov_html = "".join(f'<div class="cov"><span class="ic">{icon(ic)}</span><div><strong>{esc(t)}</strong>'
                       f'<span class="small">{esc(sub)}</span></div><div class="num">{esc(n)}<small>{esc(of)}</small></div></div>'
                       for ic, t, sub, n, of in items)
    out.append(f'''<section class="card coverage">
  <div class="title"><div><div class="h3">Coverage, without the gaps</div>
    <div class="subtitle">Every documented check accounted for</div></div>{icon("shield")}</div>
  <div class="cov-list">{cov_html}</div>
  <div class="cov-foot"><span>Validated in {esc(run_label)}</span><a href="#metrics">Explore metrics{icon("arrow")}</a></div>
</section></div>''')

    # ---- every metric, in detail ----
    counts = {"all": len(rows), "customer": len(sections["output"]), "engineering": len(sections["input"]) + len(sections["ops"])}
    body = []
    for key, ms in sections.items():
        for m in ms:
            body.append(
                f'<tr class="row" tabindex="0" aria-expanded="false" data-group="{GROUPS[key]}" data-status="{m["status"]}" '
                f'data-name="{esc((m["name"] + " " + m["note"]).lower())}">'
                f'<td class="name"><strong>{esc(m["name"])}</strong><span>{esc(m["note"])}</span></td>'
                f'<td class="cur">{esc(m["value"])}</td><td class="goal">{esc(m["goal"])}</td>'
                f'<td>{status(m["status"])}</td><td class="chev">{icon("chevron")}</td></tr>'
                f'<tr class="more" hidden><td colspan="5"><strong>Measured from</strong> {esc(SOURCES.get(m["key"], "-"))}</td></tr>')
    options = "".join(f'<option value="{s}">{LABELS[s]}</option>' for s in (GOOD, WARNING, CRITICAL, NONE))
    out.append(f'''<section class="card detail-card" id="metrics">
  <div class="detail-head"><div><div class="h3">Every metric, in detail</div>
    <div class="subtitle">The evidence behind the overview · open a row to see where its number comes from</div></div>
    <div class="filters"><label class="search">{icon("search")}<input id="q" type="search" placeholder="Search metrics…" aria-label="Search metrics"></label>
      <select id="status" aria-label="Filter by status"><option value="all">All statuses</option>{options}</select></div></div>
  <div class="tabs" role="tablist">
    <button class="on" role="tab" aria-selected="true" data-group="all">All metrics<span class="count">{counts["all"]}</span></button>
    <button role="tab" aria-selected="false" data-group="customer">Customer experience<span class="count">{counts["customer"]}</span></button>
    <button role="tab" aria-selected="false" data-group="engineering">Engineering quality<span class="count">{counts["engineering"]}</span></button>
  </div>
  <table><thead><tr><th>Metric</th><th>Current</th><th class="goal">Goal</th><th>Status</th><th class="chev"></th></tr></thead>
  <tbody>{"".join(body)}</tbody></table>
  <div class="empty" id="none" hidden>No metric matches this filter.</div>
</section>''')

    # ---- every API against its limit ----
    timed = [s for s in latency if s["target"]]
    if timed:
        lat_rows = "".join(
            f'<div class="lat"><div>{esc(s["name"])}</div>'
            f'<div class="track" tabindex="0" data-tip="{esc(f"{s["p99"]:.1f} ms TP99|{pct(s["p99"], s["target"]):.0f}% of the {s["target"]} ms SLO|TP50 {s["p50"]:.1f} · TP90 {s["p90"]:.1f} ms · {s["requests"]:,} requests")}">'
            f'<span style="width:{min(100, pct(s["p99"], s["target"], 1)):.1f}%;background:var(--accent)"></span></div>'
            f'<div class="val">{s["p99"]:.1f} ms <span>/ {s["target"]} ms</span></div></div>' for s in timed)
        out.append(f'''<section class="card latency" id="latency">
  <div class="h3">Every API, against its limit</div>
  <div class="subtitle">TP99 as a share of each API's SLO: a full bar is right at the limit · hover for TP50 and TP90</div>
  <div style="margin-top:14px">{lat_rows}</div>
  <div class="lat-axis"><div></div><div><span>0%</span><span>50%</span><span>100% of SLO</span></div><div></div></div>
</section>''')
    else:
        out.append('<section class="card latency" id="latency"><div class="h3">Every API, against its limit</div>'
                   '<div class="subtitle">Load tests didn\'t run in this build: no latency to show.</div></section>')

    # ---- environment + bugs ----
    load = f'PERF_LOAD={env["loadFactor"]:g} · one process' if env["loadFactor"] else "not run in this build"
    env_rows = [("Commit", f'<code>{esc(env["commit"] or "local")}</code>'),
                ("Branch", esc(env["branch"] or "local")),
                ("API under test", "mock fintech API in Docker"),
                ("Functional tests", "<code>pytest -n auto</code> · parallel workers"),
                ("Load tests", esc(load)),
                ("Tools", f'Python {esc(env["python"])} · pytest {esc(env["pytest"])} · Playwright {esc(env["playwright"])}'),
                ("Also on merges", "the same tests against an AWS QA environment, created and deleted per run")]
    out.append(f'''<div class="pair"><section class="card panel" id="environment">
  <div class="section-title"><div><div class="h3">Built on a known environment</div>
    <div class="subtitle">What this run was built and tested with</div></div></div>
  <dl class="env">{"".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in env_rows)}</dl>
</section>''')
    sev_max = max(bugs_["bySeverity"].values(), default=1)
    sev_rows = "".join(f'<div class="sev"><span class="name">{esc(s)}</span><div class="track"><span style="width:{n / sev_max * 100:.1f}%;'
                       f'background:var(--accent)"></span></div><span class="n">{n}</span></div>'
                       for s, n in bugs_["bySeverity"].items())
    out.append(f'''<section class="card panel">
  <div class="section-title"><div><div class="h3">Bugs caught before release</div>
    <div class="subtitle">Found by the tests, by severity</div></div>{icon("bug")}</div>
  <div class="bugs-big"><div class="big">{bugs_["found"]}</div><span class="meta">found · {bugs_["open"]} open</span></div>
  <div>{sev_rows}</div>
  <div class="card-foot"><span>{"All fixed · the tests that caught them still run" if not bugs_["open"] else f"{bugs_['open']} still open"}</span>
    <a href="{esc(url["repo"])}#bugs-found-in-the-mock-api-and-fixed">See the list{icon("arrow")}</a></div>
</section></div>''')

    out.append(f'''<footer>Not measured here, and why: escaped defects, Sev-2 incidents and time to detect need production
traffic and an incident tracker; flaky-test rate needs reruns of the same commit.
<a href="{esc(url["definitions"])}">The definitions</a> say how each would be measured.</footer>
</main></div><div id="tip" role="tooltip"></div><script>{JS}</script></body></html>''')
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--functional", type=Path, default=ROOT / "reports", help="folder with junit.xml + api*.log")
    parser.add_argument("--perf", type=Path, default=ROOT / "reports", help="folder with performance.json")
    parser.add_argument("--out", type=Path, default=ROOT / "reports")
    args = parser.parse_args()

    runs = main_runs(OPS_RUNS)
    perf_json = args.perf / "performance.json"
    kpis = {
        "bugs": bugs(ROOT / "data" / "bugs.json"),
        "coverage": coverage(ROOT / "README.md", args.functional),
        "tests": test_results(args.functional / "junit.xml"),
        "performance": json.loads(perf_json.read_text()) if perf_json.exists() else None,
        "operational": operational(runs),
    }
    history = perf_history(runs, os.getenv("GITHUB_RUN_ID")) if kpis["performance"] else []
    junit_files = sorted({f.resolve() for f in (args.functional / "junit.xml", args.perf / "junit.xml") if f.exists()})
    kpis["suites"] = suites(junit_files)
    run_total = sum(x["tests"] for x in kpis["suites"])
    run_passed = run_total - sum(x["failed"] + x["skipped"] for x in kpis["suites"])
    kpis["trend"] = run_trend(os.getenv("GITHUB_RUN_NUMBER"), run_passed, run_total)
    kpis["environment"] = environment(kpis["performance"])
    sections, latency, callouts = review(kpis, history)
    markdown = to_markdown(sections, latency, callouts, history)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "kpi.json").write_text(json.dumps({"sections": sections, "latency": latency, "callouts": callouts,
                                                   "raw": kpis}, indent=2, ensure_ascii=False) + "\n")
    (args.out / "kpi.md").write_text(markdown)
    (args.out / "kpi.html").write_text(to_html(kpis, sections, latency, callouts, history))
    print(markdown)


if __name__ == "__main__":
    main()
