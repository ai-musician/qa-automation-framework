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
            note = "200 users, each with their own key" + (f" · {change:+.0f}% vs recent median" if change is not None else "")
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

ICONS = {   # 16px status icons: the color is on the icon, the meaning is in the label beside it
    GOOD: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="var(--good)"/>'
          '<path d="M4.8 8.2l2.1 2.1 4.3-4.6" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    WARNING: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5l7 12.5H1z" fill="var(--warning)"/>'
             '<path d="M8 6v3.6" stroke="#0b0b0b" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11.6" r=".9" fill="#0b0b0b"/></svg>',
    CRITICAL: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="var(--critical)"/>'
              '<path d="M5.5 5.5l5 5m0-5l-5 5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>',
    NONE: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="none" stroke="var(--muted)" stroke-width="1.5"/>'
          '<path d="M5 8h6" stroke="var(--muted)" stroke-width="1.5" stroke-linecap="round"/></svg>',
}
LABELS = {GOOD: "On goal", WARNING: "Watch", CRITICAL: "Missed", NONE: "Tracked"}

CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --accent: #2a78d6; --track: #cde2fb;
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --accent: #3987e5; --track: #184f95;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --accent: #3987e5; --track: #184f95;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 32px 16px 48px; }
a { color: var(--accent); }
header { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px 32px;
         margin-bottom: 24px; }
h1 { font-size: 26px; font-weight: 650; margin: 0 0 4px; letter-spacing: -0.01em; }
.meta { color: var(--ink-2); font-size: 14px; }
.meta a { margin-left: 2px; }
.verdict { text-align: right; }
.verdict .hero { font-size: 48px; font-weight: 650; line-height: 1; letter-spacing: -0.02em; }
.verdict .hero small { font-size: 20px; color: var(--ink-2); font-weight: 500; }
.verdict .sub { color: var(--ink-2); font-size: 14px; margin-top: 6px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px 22px; }
.callouts { margin-bottom: 20px; display: grid; gap: 8px; }
.callout { display: flex; gap: 10px; align-items: flex-start; }
.callout svg, .pill svg { width: 16px; height: 16px; flex: none; margin-top: 3px; }
.callout strong { font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 20px; }
.tile { display: flex; flex-direction: column; gap: 4px; min-height: 148px; }
.tile .label { color: var(--ink-2); font-size: 14px; }
.tile .value { font-size: 32px; font-weight: 650; letter-spacing: -0.01em; line-height: 1.15; }
.tile .context { color: var(--ink-2); font-size: 13px; }
.tile .spark { margin: 4px 0 2px; }
.tile .foot { margin-top: auto; padding-top: 10px; display: flex; justify-content: space-between; align-items: center;
              gap: 8px; color: var(--ink-2); font-size: 13px; white-space: nowrap; }
.pill { display: inline-flex; gap: 6px; align-items: center; font-size: 13px; color: var(--ink-2); white-space: nowrap; }
.pill svg { margin-top: 0; }
.meter { height: 6px; border-radius: 3px; background: var(--track); overflow: hidden; }
.meter span { display: block; height: 100%; border-radius: 3px; background: var(--accent); }
.spark { width: 100%; height: auto; display: block; }
.spark polyline { fill: none; stroke: var(--muted); stroke-width: 1.5; stroke-linejoin: round; stroke-linecap: round; }
.spark circle { fill: var(--accent); stroke: var(--surface); stroke-width: 2; }
.sections { display: grid; gap: 16px; margin-bottom: 20px; }
h2 { font-size: 17px; font-weight: 600; margin: 0; }
h2 + .h2sub { color: var(--ink-2); font-size: 14px; margin: 2px 0 12px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-weight: 500; color: var(--muted); font-size: 13px; padding: 6px 8px;
     border-bottom: 1px solid var(--grid); }
td { padding: 10px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td .note { display: block; color: var(--ink-2); font-size: 13px; margin-top: 2px; }
td.status { width: 120px; white-space: nowrap; }
td.goal, th.goal { width: 110px; color: var(--ink-2); white-space: nowrap; }
.metric-value { font-weight: 600; white-space: nowrap; }
.lat-row { display: grid; grid-template-columns: minmax(150px, 1.1fr) 3fr minmax(120px, auto); gap: 14px;
           align-items: center; padding: 9px 0; border-bottom: 1px solid var(--grid); }
.lat-row:last-of-type { border-bottom: 0; }
.lat-name { font-size: 14px; }
.lat-bar { position: relative; height: 22px; outline: none; }
.lat-bar .meter { position: absolute; left: 0; right: 0; top: 8px; }
.lat-bar:hover .meter span, .lat-bar:focus .meter span { filter: brightness(1.12); }
.lat-val { font-size: 14px; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.lat-val span { color: var(--ink-2); }
.axis { display: grid; grid-template-columns: minmax(150px, 1.1fr) 3fr minmax(120px, auto); gap: 14px;
        color: var(--muted); font-size: 12px; margin-top: 4px; }
.axis div:nth-child(2) { display: flex; justify-content: space-between; }
details { margin-top: 14px; }
summary { cursor: pointer; color: var(--ink-2); font-size: 14px; }
td .spark { width: 96px; }
footer { color: var(--ink-2); font-size: 13px; margin-top: 8px; }
#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
       border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 13px;
       box-shadow: 0 4px 16px rgba(0,0,0,0.12); opacity: 0; transition: opacity .08s; z-index: 10; }
#tip strong { display: block; font-size: 15px; }
#tip span { color: var(--ink-2); display: block; }
@media (max-width: 900px) { .tiles { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 640px) {
  .tiles { grid-template-columns: 1fr; }
  td.goal, th.goal { display: none; }
  td.status { width: auto; }
  .verdict { text-align: left; }
  .lat-row, .axis { grid-template-columns: 1fr auto; }
  .lat-bar { grid-column: 1 / -1; grid-row: 2; }
  .axis div:first-child, .axis div:last-child { display: none; }
  .axis { grid-template-columns: 1fr; }
}
"""

TOOLTIP_JS = """
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
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def pill(status: str) -> str:
    return f'<span class="pill">{ICONS[status]}{LABELS[status]}</span>'


def svg_sparkline(values: list[float], w=280, h=34) -> str:
    """De-emphasis line for the history, accent dot for this run. Drawn at the size it's shown at (w x h),
    so the dot stays round."""
    if len(values) < 2:
        return ""
    pad = 5
    low, high = min(values), max(values)
    span = (high - low) or 1
    pts = [(pad + i * (w - 2 * pad) / (len(values) - 1), h - pad - (v - low) / span * (h - 2 * pad))
           for i, v in enumerate(values)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    x, y = pts[-1]
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" style="max-width:{w}px" role="img" '
            f'aria-label="last {len(values)} runs, from {values[0]:.1f} to {values[-1]:.1f}">'
            f'<polyline points="{poly}"/><circle cx="{x:.1f}" cy="{y:.1f}" r="4"/></svg>')


def meter(ratio: float, label: str) -> str:
    width = max(0.0, min(1.0, ratio)) * 100
    return f'<div class="meter" role="img" aria-label="{esc(label)}"><span style="width:{width:.1f}%"></span></div>'


TILE_KEYS = ["availability", "login", "slo", "coverage", "tests", "cfr"]   # the headline numbers, in this order


def to_html(sections: dict, latency: list, callouts: list, history: list) -> str:
    ctx, url = context(), links()
    by_key = {m["key"]: m for ms in sections.values() for m in ms}
    scored = [m for m in by_key.values() if m["status"] != NONE]
    on_goal = sum(m["status"] == GOOD for m in scored)
    meta = " · ".join(x for x in [f"Run #{ctx['run']}" if ctx["run"] else "Local run",
                                  f"commit {ctx['commit']}" if ctx["commit"] else "", ctx["date"]] if x)
    run_link = f' · <a href="{esc(url["run"])}">CI run</a>' if url["run"] else ""

    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<title>QA Metrics Review</title><style>{CSS}</style></head><body><main>',
           f'<header><div><h1>QA metrics review</h1><div class="meta">{esc(meta)}{run_link} · '
           f'<a href="{esc(url["allure"])}">Test report</a> · <a href="{esc(url["definitions"])}">Definitions</a></div></div>'
           f'<div class="verdict"><div class="hero">{on_goal}<small> of {len(scored)}</small></div>'
           f'<div class="sub">metrics on goal</div></div></header>']

    # callouts first: a review starts with what needs attention
    out.append('<section class="card callouts" aria-label="Callouts">')
    if callouts:
        out += [f'<div class="callout">{ICONS[c["status"]]}<div><strong>{esc(c["title"])}.</strong> {esc(c["text"])}</div></div>'
                for c in callouts]
    else:
        out.append(f'<div class="callout">{ICONS[GOOD]}<div><strong>No callouts.</strong> '
                   f'Every metric is on goal and within its trend over the last {len(history) + 1} merges to main.</div></div>')
    if ctx["pr"]:
        out.append(f'<div class="callout">{ICONS[NONE]}<div>Pull request runs use a third of main\'s load: '
                   f'compare latency with care.</div></div>')
    out.append('</section>')

    # headline tiles
    out.append('<section class="tiles" aria-label="Headline metrics">')
    for key in TILE_KEYS:
        if not (m := by_key.get(key)):
            continue
        trend = svg_sparkline(m["series"]) if len(m["series"]) > 1 else ""
        context_line = f'<div class="context">{esc(m["note"])}</div>' if m["note"] and not trend else ""
        out.append(f'<div class="card tile"><div class="label">{esc(m["name"])}</div>'
                   f'<div class="value">{esc(m["value"])}</div>{context_line}{trend}'
                   f'<div class="foot"><span>Goal {esc(m["goal"])}</span>{pill(m["status"])}</div></div>')
    out.append('</section>')

    # the three sections as tables: every metric with its goal, status and context
    out.append('<div class="sections">')
    for key, (title, subtitle) in SECTION_TITLES.items():
        if not sections[key]:
            continue
        rows = "".join(
            f'<tr><td class="status">{pill(m["status"])}</td>'
            f'<td>{esc(m["name"])}{f"<span class=note>{esc(m["note"])}</span>" if m["note"] else ""}</td>'
            f'<td class="num"><span class="metric-value">{esc(m["value"])}</span></td><td class="goal">{esc(m["goal"])}</td></tr>'
            for m in sections[key])
        out.append(f'<section class="card"><h2>{title}</h2><div class="h2sub">{subtitle}</div>'
                   f'<table><thead><tr><th>Status</th><th>Metric</th><th class="num">Now</th><th class="goal">Goal</th></tr></thead>'
                   f'<tbody>{rows}</tbody></table></section>')
    out.append('</div>')

    # latency: how much of each API's TP99 budget was used, then the full table
    timed = [s for s in latency if s["target"]]
    if timed:
        out.append('<section class="card"><h2>API latency under load</h2>'
                   '<div class="h2sub">TP99 as a share of each API\'s SLO: a full bar means right at the limit</div>')
        for s in timed:
            used = pct(s["p99"], s["target"])
            tip = (f"{s['p99']:.1f} ms TP99|{used:.0f}% of the {s['target']} ms SLO|"
                   f"TP50 {s['p50']:.1f} · TP90 {s['p90']:.1f} ms|{s['requests']:,} requests")
            out.append(f'<div class="lat-row"><div class="lat-name">{esc(s["name"])}</div>'
                       f'<div class="lat-bar" tabindex="0" data-tip="{esc(tip)}">'
                       f'{meter(used / 100, f"{s["name"]}: {used:.0f}% of SLO")}</div>'
                       f'<div class="lat-val">{s["p99"]:.1f} ms <span>/ {s["target"]} ms</span></div></div>')
        out.append('<div class="axis"><div></div><div><span>0%</span><span>50%</span><span>100% of SLO</span></div><div></div></div>')

        rows = "".join(
            f'<tr><td>{esc(s["name"])}</td><td class="num">{s["requests"]:,}</td>'
            f'<td class="num">{s["p50"]:.1f}</td><td class="num">{(s["p90"] or 0):.1f}</td>'
            f'<td class="num"><strong>{s["p99"]:.1f}</strong></td><td class="num">{s["target"] or "-"}</td>'
            f'<td class="num">{f"{s["p999"]:.1f}" if s["p999"] is not None and s["requests"] >= 1000 else "-"}</td>'
            f'<td>{svg_sparkline(s["series"], w=96, h=24)}</td>'
            f'<td class="num">{f"{s["change"]:+.0f}%" if s["change"] is not None else "-"}</td></tr>'
            for s in latency)
        out.append(f'<details><summary>All latency percentiles, in milliseconds (table view)</summary>'
                   f'<table><thead><tr><th>Scenario</th><th class="num">Requests</th><th class="num">TP50</th>'
                   f'<th class="num">TP90</th><th class="num">TP99</th><th class="num">SLO</th>'
                   f'<th class="num">TP99.9</th><th>TP99, last {len(history) + 1} merges</th>'
                   f'<th class="num">vs median</th></tr></thead><tbody>{rows}</tbody></table>'
                   f'<p class="meta">TP99.9 is shown only for scenarios with 1,000+ requests; below that it is the '
                   f'slowest single request.</p></details></section>')

    out.append(f'<footer><p>Not measured here, and why: escaped defects, Sev-2 incidents and time to detect need '
               f'production traffic and an incident tracker; flaky-test rate needs reruns of the same commit. '
               f'<a href="{esc(url["definitions"])}">The definitions</a> say how each would be measured.</p></footer>')
    out.append(f'</main><div id="tip" role="tooltip"></div><script>{TOOLTIP_JS}</script></body></html>')
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
    sections, latency, callouts = review(kpis, history)
    markdown = to_markdown(sections, latency, callouts, history)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "kpi.json").write_text(json.dumps({"sections": sections, "latency": latency, "callouts": callouts,
                                                   "raw": kpis}, indent=2, ensure_ascii=False) + "\n")
    (args.out / "kpi.md").write_text(markdown)
    (args.out / "kpi.html").write_text(to_html(sections, latency, callouts, history))
    print(markdown)


if __name__ == "__main__":
    main()
