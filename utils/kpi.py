"""
QA KPIs for one CI run, calculated from what the run already produces. Standard library only.

    python -m utils.kpi                                    # reads reports/ after pytest (+ pytest -m perf)
    python -m utils.kpi --functional reports/functional --perf reports/performance   # how CI calls it

Sources:
  quality          data/bugs.json: bugs found before release, with severity
  coverage         README API table (the documented endpoints + status codes) vs api.log (what the tests sent)
  test results     junit.xml from the functional run
  pipeline health  the last 20 completed runs of this workflow on main (GitHub CLI; skipped if unavailable)
  performance      performance.json from pytest -m perf

Writes reports/kpi.md (shown on the GitHub Actions run page) and reports/kpi.json, and prints the markdown.
"""
import argparse
import json
import re
import statistics
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).parents[1]

# targets: a KPI is only useful next to the value it should reach
TARGET_PASS_RATE = 100           # % of functional tests passing
TARGET_COVERAGE = 100            # % of documented endpoints / status codes exercised
TARGET_CI_PASS_RATE = 95         # % of recent main runs that passed
TARGET_FEEDBACK_MIN = 5          # minutes from push to result
INTEGRITY_SCENARIOS = ("create transaction", "same email x20 in parallel")   # money / accounts must stay correct


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


def pipeline_health(limit=20) -> dict | None:
    """Pass rate and median push-to-result time of recent main runs. None when the GitHub CLI can't answer."""
    try:
        out = subprocess.run(["gh", "run", "list", "--workflow", "tests.yml", "--branch", "main", "--limit", str(limit),
                              "--json", "status,conclusion,createdAt,updatedAt"],
                             capture_output=True, text=True, timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # cancelled runs were replaced by a newer push, so they say nothing about quality
    runs = [r for r in json.loads(out) if r["status"] == "completed" and r["conclusion"] in ("success", "failure")]
    if not runs:
        return None
    minutes = [(datetime.fromisoformat(r["updatedAt"]) - datetime.fromisoformat(r["createdAt"])).total_seconds() / 60
               for r in runs]
    return {"runs": len(runs), "passed": sum(r["conclusion"] == "success" for r in runs),
            "medianMinutes": round(statistics.median(minutes), 1)}


def performance(perf_json: Path) -> dict | None:
    if not perf_json.exists():
        return None
    scenarios = json.loads(perf_json.read_text())
    timed = [s for s in scenarios if s.get("target_p95_ms")]
    integrity = [s for s in scenarios if s["name"] in INTEGRITY_SCENARIOS]
    return {
        "scenarios": len(scenarios), "passed": sum(bool(s["passed"]) for s in scenarios),
        "requests": sum(s["requests"] for s in scenarios), "errors": sum(s["errors"] for s in scenarios),
        # how much of its p95 budget the slowest scenario used: 100% means right at the target
        "worstP95Budget": max((round(100 * s["p95_ms"] / s["target_p95_ms"]) for s in timed), default=None),
        "integrityChecks": len(integrity), "integrityPassed": sum(bool(s["passed"]) for s in integrity),
    }


def bugs(bugs_json: Path) -> dict:
    found = json.loads(bugs_json.read_text())
    by = lambda key: {v: sum(b[key] == v for b in found) for v in dict.fromkeys(b[key] for b in found)}
    return {"found": len(found), "open": sum(b["status"] != "fixed" for b in found),
            "openCritical": sum(b["status"] != "fixed" and b["severity"] == "critical" for b in found),
            "bySeverity": by("severity"), "byArea": by("area"), "byFoundBy": by("foundBy")}


def pct(part, whole):
    return round(100 * part / whole) if whole else 0


def to_markdown(k: dict) -> str:
    ok = lambda good: "✅" if good else "⚠️"
    rows = []

    b = k["bugs"]
    order = ["critical", "high", "medium", "low"]
    severity = ", ".join(f"{b['bySeverity'][s]} {s}" for s in sorted(b["bySeverity"], key=order.index))
    rows.append(("Quality", "Bugs caught before release", f"{b['found']} ({severity})", "-", ""))
    rows.append(("Quality", "Open critical bugs", str(b["openCritical"]), "0", ok(b["openCritical"] == 0)))

    c = k["coverage"]
    rows.append(("Coverage", "Documented endpoints tested", f"{c['endpointsTested']} / {c['endpoints']}",
                 f"{TARGET_COVERAGE}%", ok(pct(c["endpointsTested"], c["endpoints"]) >= TARGET_COVERAGE)))
    rows.append(("Coverage", "Documented status codes tested",
                 f"{c['statusCodesTested']} / {c['statusCodes']} ({pct(c['statusCodesTested'], c['statusCodes'])}%)",
                 f"{TARGET_COVERAGE}%", ok(pct(c["statusCodesTested"], c["statusCodes"]) >= TARGET_COVERAGE)))

    if t := k["tests"]:
        rate = pct(t["passed"], t["tests"] - t["skipped"])
        rows.append(("Tests", "Functional tests passing", f"{t['passed']} / {t['tests'] - t['skipped']} ({rate}%)",
                     f"{TARGET_PASS_RATE}%", ok(rate >= TARGET_PASS_RATE)))
        rows.append(("Tests", "Functional run time", f"{t['seconds']} s", "-", ""))
    else:
        rows.append(("Tests", "Functional tests passing", "no results", f"{TARGET_PASS_RATE}%", "⚠️"))

    if p := k["pipeline"]:
        rate = pct(p["passed"], p["runs"])
        rows.append(("Pipeline", f"CI pass rate, last {p['runs']} runs on main", f"{rate}%",
                     f"≥ {TARGET_CI_PASS_RATE}%", ok(rate >= TARGET_CI_PASS_RATE)))
        rows.append(("Pipeline", "Median time from push to result", f"{p['medianMinutes']} min",
                     f"≤ {TARGET_FEEDBACK_MIN} min", ok(p["medianMinutes"] <= TARGET_FEEDBACK_MIN)))
    else:
        rows.append(("Pipeline", "CI pass rate / time to result", "not available (no GitHub CLI access)", "-", ""))

    if f := k["performance"]:
        rows.append(("Performance", "Scenarios within target", f"{f['passed']} / {f['scenarios']}", "all",
                     ok(f["passed"] == f["scenarios"])))
        rows.append(("Performance", "Error rate under load", f"{pct(f['errors'], f['requests'])}% of {f['requests']:,} requests",
                     "0%", ok(f["errors"] == 0)))
        if f["worstP95Budget"] is not None:
            rows.append(("Performance", "Slowest p95, as % of its target", f"{f['worstP95Budget']}%", "≤ 100%",
                         ok(f["worstP95Budget"] <= 100)))
        rows.append(("Performance", "Data integrity checks under load", f"{f['integrityPassed']} / {f['integrityChecks']}",
                     "all", ok(f["integrityPassed"] == f["integrityChecks"])))
    else:
        rows.append(("Performance", "Load tests", "not run in this build", "-", ""))

    lines = ["## QA KPIs", "", "| Area | KPI | Value | Target | |", "|---|---|---|---|:---:|"]
    lines += [f"| {' | '.join(r)} |" for r in rows]
    if c["untested"]:
        lines += ["", f"Untested documented status codes: {', '.join(c['untested'])}"]
    found_by = ", ".join(f"{n} by {how}" for how, n in b["byFoundBy"].items())
    lines += ["", f"Bugs by area: {', '.join(f'{n} {a}' for a, n in b['byArea'].items())}. Found: {found_by}."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--functional", type=Path, default=ROOT / "reports", help="folder with junit.xml + api.log")
    parser.add_argument("--perf", type=Path, default=ROOT / "reports", help="folder with performance.json")
    parser.add_argument("--out", type=Path, default=ROOT / "reports")
    args = parser.parse_args()

    kpis = {
        "bugs": bugs(ROOT / "data" / "bugs.json"),
        "coverage": coverage(ROOT / "README.md", args.functional / "api.log"),
        "tests": test_results(args.functional / "junit.xml"),
        "pipeline": pipeline_health(),
        "performance": performance(args.perf / "performance.json"),
    }
    markdown = to_markdown(kpis)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "kpi.json").write_text(json.dumps(kpis, indent=2) + "\n")
    (args.out / "kpi.md").write_text(markdown)
    print(markdown)


if __name__ == "__main__":
    main()
