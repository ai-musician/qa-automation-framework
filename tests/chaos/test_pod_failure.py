"""
Chaos tests: does the inference service keep answering when one of its pods goes away?

They run inside the Kubernetes cluster (k8s/chaos-job.yaml, started by scripts/k8s_test.sh), where the Job's
service account may list and delete the `inference` pods. Each test streams chat requests from 8 users without a
pause, stops one of the two pods partway through, and measures what the users saw:

  graceful  the pod is deleted, as in a rolling update or a node drain: Kubernetes stops routing new requests to
            it, preStop gives its open streams 5 s to finish, then SIGTERM. Not a single request may fail.
  crash     the server process dies at once (POST /debug/crash, which only exists with FAULT_INJECTION=on): the
            streams it was serving are cut, and new requests can reach it until its readiness probe fails. Measured,
            with limits: no failure once two replicas are ready again (the service healed), availability at least
            MIN_AVAILABILITY, and never MAX_GAP_S or more without a successful reply, because the other replica
            keeps serving. Kubernetes restarts the container in the same pod.

Both also check that two replicas are ready again within MAX_RECOVERY_S.
"""
import threading
import time

import pytest
import requests

import config
from utils.kube import SERVICE_ACCOUNT, Kube, is_ready
from utils.load import longest_gap, stream_until

pytestmark = [pytest.mark.chaos,
              pytest.mark.skipif(not SERVICE_ACCOUNT.exists(), reason="runs inside Kubernetes: scripts/k8s_test.sh")]

CHAT = f"{config.env.api_base_url}/v1/chat/completions"
USERS = 8
BEFORE_S, AFTER_S = 5, 5      # steady load before the pod is stopped, and after the replacement is ready
REPLICAS = 2
MIN_AVAILABILITY = 0.85       # crash: successful replies / all replies (about 93% on a laptop: the cut streams)
MAX_GAP_S = 2.0               # crash: longest time with no successful reply at all
MAX_RECOVERY_S = 60           # until two ready replicas again


def restarts(pod: dict) -> int:
    return sum(c["restartCount"] for c in pod["status"].get("containerStatuses", []))


def wait_for_ready_replicas(kube: Kube, timeout_s: float, replaced: dict | None = None) -> float:
    """Waits until REPLICAS pods are ready; if `replaced` (a pod as it was before the failure) is given, it must
    also be gone or restarted. Returns when that happened."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pods = kube.inference_pods()
        ready = [p for p in pods if is_ready(p)]
        if replaced:
            name, before = replaced["metadata"]["name"], restarts(replaced)
            ready = [p for p in ready if p["metadata"]["name"] != name or restarts(p) > before]
        if len(ready) >= REPLICAS:
            return time.monotonic()
        time.sleep(0.25)
    pytest.fail(f"fewer than {REPLICAS} ready inference pods after {timeout_s:.0f} s")


def crash(pod: dict):
    """Crashes the server in that pod: its own address, not the Service, which could pick the other pod."""
    res = requests.post(f"http://{pod['status']['podIP']}:5050/debug/crash", timeout=5,
                        headers={"Authorization": f"Bearer {config.env.admin_api_key}"})
    assert res.status_code == 202, f"POST /debug/crash: {res.status_code} {res.text[:200]}"


@pytest.fixture(scope="module")
def kube():
    return Kube()


@pytest.mark.parametrize("mode", ["graceful", "crash"])
def test_inference_survives_losing_a_pod(kube, mode, capsys):
    wait_for_ready_replicas(kube, MAX_RECOVERY_S)
    stop, replies = threading.Event(), []
    users = [threading.Thread(target=stream_until, args=(stop, replies, CHAT, config.env.api_key))
             for _ in range(USERS)]
    started = time.monotonic()
    for user in users:
        user.start()

    time.sleep(BEFORE_S)
    victim = next(p for p in kube.inference_pods() if is_ready(p))
    stopped_at = time.monotonic()
    if mode == "graceful":
        kube.delete(victim["metadata"]["name"])
    else:
        crash(victim)
    recovered_at = wait_for_ready_replicas(kube, MAX_RECOVERY_S, replaced=victim)
    time.sleep(AFTER_S)
    stop.set()
    for user in users:
        user.join()
    ended = time.monotonic()

    failed = [r for r in replies if not r.ok]
    availability = 1 - len(failed) / len(replies)
    gap, recovery = longest_gap(replies, started, ended), recovered_at - stopped_at
    with capsys.disabled():                             # the Job's log is the report (scripts/k8s_test.sh)
        print(f"\n## Pod failure: {mode}\n\n| Replies | Failed | Availability | Longest outage | Replaced in |\n"
              f"|---:|---:|---:|---:|---:|\n| {len(replies)} | {len(failed)} | {availability:.2%} | {gap:.2f} s "
              f"| {recovery:.1f} s |\n")
        for r in failed[:5]:
            when = r.start - stopped_at
            event = "graceful stop" if mode == "graceful" else "crash"
            print(f"- a request started {abs(when):.2f} s {'before' if when < 0 else 'after'} the {event}: {r.error[:100]}")

    assert recovery <= MAX_RECOVERY_S, f"replaced in {recovery:.1f} s"
    if mode == "graceful":
        assert not failed, f"{len(failed)} of {len(replies)} replies failed during a graceful stop, " \
                           f"e.g. {failed[0].error[:200]}"
    else:
        late = [r for r in failed if r.start > recovered_at]
        assert not late, f"{len(late)} replies failed after recovery, e.g. {late[0].error[:200]}"
        assert availability >= MIN_AVAILABILITY, f"availability {availability:.2%} < {MIN_AVAILABILITY:.0%}"
        assert gap < MAX_GAP_S, f"no successful reply for {gap:.2f} s: the other replica should keep serving"
