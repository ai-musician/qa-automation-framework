"""
A small load-test helper: sends one kind of request many times from parallel threads and measures it.

    summary = run_load("get user", send, requests_total=500, concurrency=20)
    summary.p99_ms      # TP99: 99% of requests were at least this fast (the SLO the tests check)

`send(session, i)` makes request number i and returns the requests.Response.
Each thread gets its own requests.Session (sessions aren't thread-safe; one per thread also reuses connections).

For streamed replies (server-sent events, as LLM inference services send tokens):

    summary, streams = run_stream_load("chat first token, 8 users", send, requests_total=32, concurrency=8)
    summary.p99_ms          # TP99 of the time to first token (TTFT)
    summary.itl_p99_ms      # TP99 of the time between tokens
    summary.tokens_per_s    # output tokens per second, all streams together

Here `send` must make the request with stream=True, so the tokens can be timed as they arrive.
"""
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

from utils.http import Session


@dataclass
class Summary:
    name: str
    requests: int
    concurrency: int
    errors: int
    error_rate: float
    rps: float              # requests per second over the whole run
    p50_ms: float           # median
    p90_ms: float
    p95_ms: float
    p99_ms: float           # TP99: the SLO
    p999_ms: float          # TP99.9: only meaningful with 1,000+ requests (below that it is close to the max)
    max_ms: float
    statuses: dict[str, int]
    target_p99_ms: float | None = None
    passed: bool | None = None
    sample_errors: list[str] = field(default_factory=list)
    itl_p99_ms: float | None = None     # streamed scenarios: TP99 of the time between tokens
    tokens_per_s: float | None = None   # streamed scenarios: output tokens per second, all streams together


@dataclass
class Stream:
    """One streamed reply, as the client saw it."""
    ttft_ms: float | None        # time to first token: from sending the request to the first piece of text
    gaps_ms: list[float]         # time between consecutive tokens
    content: str
    tokens: int
    finish_reason: str | None
    done: bool                   # the stream ended with data: [DONE]
    error: str | None = None


def percentile(sorted_ms: list[float], p: float) -> float:
    """Nearest-rank percentile: the value that p% of requests were at or below."""
    k = max(0, math.ceil(p / 100 * len(sorted_ms)) - 1)
    return sorted_ms[k]


def run_load(name, send, requests_total, concurrency, warmup=10, ok_statuses=(200, 201)) -> Summary:
    local = threading.local()

    def one(i):
        if not hasattr(local, "session"):
            local.session = Session()
        start = time.perf_counter()
        try:
            res = send(local.session, i)
            error = None if res.status_code in ok_statuses else f"{res.status_code}: {res.text[:200]}"
            status = res.status_code
        except requests.RequestException as e:          # connection refused, timeout...
            error, status = str(e), 0
        return status, (time.perf_counter() - start) * 1000, error

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, range(warmup)))             # warm-up: open connections, not measured
        started = time.perf_counter()
        results = list(pool.map(one, range(warmup, warmup + requests_total)))
        wall_seconds = time.perf_counter() - started

    latencies = sorted(ms for _, ms, _ in results)
    errors = [err for _, _, err in results if err]
    statuses: dict[str, int] = {}
    for status, _, _ in results:
        statuses[str(status)] = statuses.get(str(status), 0) + 1

    return Summary(
        name=name,
        requests=requests_total,
        concurrency=concurrency,
        errors=len(errors),
        error_rate=len(errors) / requests_total,
        rps=requests_total / wall_seconds,
        p50_ms=percentile(latencies, 50),
        p90_ms=percentile(latencies, 90),
        p95_ms=percentile(latencies, 95),
        p99_ms=percentile(latencies, 99),
        p999_ms=percentile(latencies, 99.9),
        max_ms=latencies[-1],
        statuses=statuses,
        sample_errors=errors[:5],
    )


def read_stream(res: requests.Response, start: float) -> Stream:
    """Reads an OpenAI-style event stream, timing each token as it arrives."""
    if res.status_code != 200:
        return Stream(None, [], "", 0, None, False, f"{res.status_code}: {res.text[:200]}")
    content, times, finish_reason, done = [], [], None, False
    for line in res.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data = line.removeprefix("data: ")
        if data == "[DONE]":
            done = True
            break
        choice = json.loads(data)["choices"][0]
        if text := choice["delta"].get("content"):
            times.append(time.perf_counter())
            content.append(text)
        finish_reason = choice.get("finish_reason") or finish_reason
    ttft = (times[0] - start) * 1000 if times else None
    gaps = [(b - a) * 1000 for a, b in zip(times, times[1:])]
    error = None if done and times else "stream ended early" if times else "no tokens"
    return Stream(ttft, gaps, "".join(content), len(times), finish_reason, done, error)


def run_stream_load(name, send, requests_total, concurrency, warmup=4) -> tuple[Summary, list[Stream]]:
    """Like run_load, for streamed replies: TTFT is the latency, plus the time between tokens and tokens per second.
    Returns the summary and every measured stream (in request order), so a test can also check what was said."""
    local = threading.local()

    def one(i):
        if not hasattr(local, "session"):
            local.session = Session()
        start = time.perf_counter()
        try:
            with send(local.session, i) as res:
                return res.status_code, read_stream(res, start)
        except (requests.RequestException, ValueError, KeyError) as e:     # connection lost, a malformed event...
            return 0, Stream(None, [], "", 0, None, False, str(e))

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, range(warmup)))
        started = time.perf_counter()
        results = list(pool.map(one, range(warmup, warmup + requests_total)))
        wall_seconds = time.perf_counter() - started

    streams = [stream for _, stream in results]
    ttfts = sorted(s.ttft_ms for s in streams if s.ttft_ms is not None) or [0.0]
    gaps = sorted(g for s in streams for g in s.gaps_ms) or [0.0]
    errors = [s.error for s in streams if s.error]
    statuses: dict[str, int] = {}
    for status, _ in results:
        statuses[str(status)] = statuses.get(str(status), 0) + 1
    summary = Summary(
        name=name, requests=requests_total, concurrency=concurrency, errors=len(errors),
        error_rate=len(errors) / requests_total, rps=requests_total / wall_seconds,
        p50_ms=percentile(ttfts, 50), p90_ms=percentile(ttfts, 90), p95_ms=percentile(ttfts, 95),
        p99_ms=percentile(ttfts, 99), p999_ms=percentile(ttfts, 99.9), max_ms=ttfts[-1],
        statuses=statuses, sample_errors=errors[:5],
        itl_p99_ms=percentile(gaps, 99), tokens_per_s=sum(s.tokens for s in streams) / wall_seconds)
    return summary, streams


def write_reports(summaries: list[Summary], folder: Path):
    """performance.json for tools, performance.html for people, performance.md for the GitHub Actions run page."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "performance.json").write_text(json.dumps([asdict(s) for s in summaries], indent=2))

    lines = ["## Performance results", "",
             "| Scenario | Requests | Req/s | TP50 ms | TP90 ms | TP99 ms | TP99 SLO | TP99.9 ms | Errors | Result |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|"]
    for s in summaries:
        target = s.target_p99_ms if s.target_p99_ms is not None else "-"
        lines.append(f"| {s.name} | {s.requests} | {s.rps:,.0f} | {s.p50_ms:.1f} | {s.p90_ms:.1f} | {s.p99_ms:.1f} "
                     f"| {target} | {s.p999_ms:.1f} | {s.errors} | {'✅' if s.passed else '❌'} |")
    streamed = [s for s in summaries if s.tokens_per_s is not None]
    if streamed:
        lines += ["", "Streamed replies (TP99 above is the time to first token):", "",
                  "| Scenario | Concurrency | TTFT TP50 ms | TTFT TP99 ms | Between tokens TP99 ms | Output tokens/s |",
                  "|---|---:|---:|---:|---:|---:|"]
        lines += [f"| {s.name} | {s.concurrency} | {s.p50_ms:.1f} | {s.p99_ms:.1f} | {s.itl_p99_ms:.1f} "
                  f"| {s.tokens_per_s:,.0f} |" for s in streamed]
    (folder / "performance.md").write_text("\n".join(lines) + "\n")

    rows = "".join(
        f"<tr class={'pass' if s.passed else 'fail'}>"
        f"<td>{s.name}</td><td>{s.requests}</td><td>{s.concurrency}</td>"
        f"<td>{s.rps:,.0f}</td><td>{s.p50_ms:.1f}</td><td>{s.p90_ms:.1f}</td><td><b>{s.p99_ms:.1f}</b></td>"
        f"<td>{s.target_p99_ms if s.target_p99_ms is not None else '-'}</td>"
        f"<td>{s.p999_ms:.1f}</td><td>{s.max_ms:.1f}</td>"
        f"<td>{s.errors} ({s.error_rate:.1%})</td><td>{'PASS' if s.passed else 'FAIL'}</td></tr>"
        for s in summaries
    )
    (folder / "performance.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Performance Report</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #222; }}
  table {{ border-collapse: collapse; }}
  th, td {{ border-bottom: 1px solid #ddd; padding: .4rem .8rem; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  tr.pass td:last-child {{ color: #0a6b2d; font-weight: bold; }}
  tr.fail td:last-child {{ color: #b00020; font-weight: bold; }}
  p {{ max-width: 60rem; }}
</style></head><body>
<h1>Performance Report</h1>
<p>Generated {time.strftime("%Y-%m-%d %H:%M:%S")}. Latencies in milliseconds. A scenario passes when TP99 is within
its SLO and no request failed.</p>
<table>
<tr><th>Scenario</th><th>Requests</th><th>Concurrency</th><th>Req/s</th><th>TP50</th><th>TP90</th><th>TP99</th>
<th>TP99 SLO</th><th>TP99.9</th><th>Max</th><th>Errors</th><th>Result</th></tr>
{rows}
</table></body></html>
""")
