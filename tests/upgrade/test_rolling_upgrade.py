"""
Rolling-upgrade tests: can a new version of the inference service be released while users are streaming replies,
and what happens when the new version is broken?

They run inside the Kubernetes cluster (k8s/upgrade-job.yaml, started by scripts/k8s_test.sh), where the Job's service
account may change the `inference` Deployment. Each test keeps 8 users streaming without a pause and reads which
version answered each reply from the X-App-Version header:

  good version    a new APP_VERSION rolls out one pod at a time (k8s/inference.yaml: maxSurge 1, maxUnavailable 0).
                  Not a single reply may fail, both versions answer for a while (a real upgrade must survive that
                  overlap), and once the rollout is done only the new version answers.
  broken version  the new pods crash when they start, so they never become ready. The rollout must stall and report
                  it (ProgressDeadlineExceeded) with the old pods still serving; rolling back must restore them.
                  No reply may fail, and none may come from the broken version.

A rolling update is the same whatever changed in the pod template. Here that is an environment variable, because the
kind cluster only has one image; on a real release it is the image tag.
"""
import threading
import time
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager

import pytest

import config
from utils.kube import JSON_MERGE, SERVICE_ACCOUNT, Kube
from utils.load import Reply, longest_gap, stream_until

pytestmark = [pytest.mark.upgrade,
              pytest.mark.skipif(not SERVICE_ACCOUNT.exists(), reason="runs inside Kubernetes: scripts/k8s_test.sh")]

CHAT = f"{config.env.api_base_url}/v1/chat/completions"
DEPLOYMENT = "inference"
USERS = 8
NEW_VERSION, BROKEN_VERSION = "2.0.0", "broken"
BEFORE_S, AFTER_S = 5, 5      # steady load before the release, and after it finished
SETTLE_S = 2                  # after a rollout, until every Service route to the old pods is gone
MAX_ROLLOUT_S = 60            # until every replica runs the new version
MAX_STALL_S = 60              # until the Deployment reports the broken release (progressDeadlineSeconds: 30)
MAX_GAP_S = 2.0               # longest time with no successful reply at all


def version_of(deployment: dict) -> str:
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    return next(e["value"] for e in env if e["name"] == "APP_VERSION")


def rolled_out(deployment: dict) -> bool:
    """What `kubectl rollout status` waits for: the controller has seen the change, and every replica is new and up."""
    status, replicas = deployment["status"], deployment["spec"]["replicas"]
    return (status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
            and status.get("updatedReplicas") == replicas
            and status.get("replicas") == replicas
            and status.get("availableReplicas") == replicas)


def settled(kube: Kube) -> bool:
    """Rolled out, and the old pods are gone too: a rollout counts as done while they're still in their preStop
    pause, and until they exit they can finish a reply. Testing before this would catch the last release's pods."""
    deployment = kube.deployment(DEPLOYMENT)
    return rolled_out(deployment) and len(kube.inference_pods()) == deployment["spec"]["replicas"]


def progress_reason(deployment: dict) -> str | None:
    return next((c["reason"] for c in deployment["status"].get("conditions", []) if c["type"] == "Progressing"), None)


def wait_for(check: Callable[[], bool], timeout_s: float, what: str) -> float:
    """Waits until check() is true and returns when that happened."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return time.monotonic()
        time.sleep(0.25)
    pytest.fail(f"{what}: not within {timeout_s:.0f} s")


def release(kube: Kube, version: str, **container):
    """A strategic merge patch, like `kubectl set env`: a new pod template, which starts a rolling update."""
    kube.patch_deployment(DEPLOYMENT, {"spec": {"template": {"spec": {"containers": [
        {"name": DEPLOYMENT, "env": [{"name": "APP_VERSION", "value": version}], **container}]}}}})


def roll_back(kube: Kube):
    """What `kubectl rollout undo` does: put back the pod template of the previous ReplicaSet (Kubernetes keeps them
    for this). A merge patch, so the broken version's `command` goes with the containers list it replaces."""
    revision = lambda rs: int(rs["metadata"]["annotations"]["deployment.kubernetes.io/revision"])
    template = sorted(kube.inference_replica_sets(), key=revision)[-2]["spec"]["template"]
    del template["metadata"]["labels"]["pod-template-hash"]          # added by the Deployment to each ReplicaSet
    kube.patch_deployment(DEPLOYMENT, {"spec": {"template": template}}, JSON_MERGE)


@contextmanager
def chat_load():
    """USERS streaming replies until the block ends. Yields the list the replies are added to."""
    stop, replies = threading.Event(), []
    users = [threading.Thread(target=stream_until, args=(stop, replies, CHAT, config.env.api_key))
             for _ in range(USERS)]
    for user in users:
        user.start()
    try:
        yield replies
    finally:
        stop.set()
        for user in users:
            user.join()


def report(capsys, title: str, replies: list[Reply], gap: float, seconds: float):
    """The Job's log is the report (scripts/k8s_test.sh)."""
    failed, versions = [r for r in replies if not r.ok], Counter(r.version for r in replies if r.ok)
    answered = ", ".join(f"{version}: {count}" for version, count in sorted(versions.items()))
    with capsys.disabled():
        print(f"\n## Rolling upgrade: {title}\n\n| Replies | Failed | Answered by | Longest outage | Took |\n"
              f"|---:|---:|---:|---:|---:|\n| {len(replies)} | {len(failed)} | {answered} | {gap:.2f} s "
              f"| {seconds:.1f} s |\n")
        for r in failed[:5]:
            print(f"- a failed reply: {r.error[:100]}")


@pytest.fixture(scope="module")
def kube():
    return Kube()


@pytest.fixture
def deployment_restored(kube):
    """Each test starts from a healthy rollout, and leaves the Deployment as it found it, even if the test fails:
    otherwise the next `kubectl apply` keeps a broken `command`, which k8s/inference.yaml doesn't mention."""
    wait_for(lambda: settled(kube), MAX_ROLLOUT_S, "the Deployment isn't healthy")
    template = kube.deployment(DEPLOYMENT)["spec"]["template"]
    yield
    kube.patch_deployment(DEPLOYMENT, {"spec": {"template": template}}, JSON_MERGE)


def test_new_version_rolls_out_without_a_failed_reply(kube, deployment_restored, capsys):
    old_version = version_of(kube.deployment(DEPLOYMENT))
    assert old_version != NEW_VERSION, "already on the new version: kubectl apply -f k8s/inference.yaml"

    started = time.monotonic()
    with chat_load() as replies:
        time.sleep(BEFORE_S)
        released = time.monotonic()
        release(kube, NEW_VERSION)
        finished = wait_for(lambda: rolled_out(kube.deployment(DEPLOYMENT)), MAX_ROLLOUT_S,
                            f"every replica on {NEW_VERSION}")
        time.sleep(AFTER_S)
    ended = time.monotonic()

    failed, gap = [r for r in replies if not r.ok], longest_gap(replies, started, ended)
    report(capsys, f"{old_version} to {NEW_VERSION}", replies, gap, finished - released)

    assert not failed, f"{len(failed)} of {len(replies)} replies failed during the rollout, e.g. {failed[0].error[:200]}"
    assert gap < MAX_GAP_S, f"no successful reply for {gap:.2f} s"
    answered_by = {r.version for r in replies}
    assert answered_by == {old_version, NEW_VERSION}, f"expected both versions to answer, got {answered_by}"
    late = [r for r in replies if r.start > finished + SETTLE_S and r.version != NEW_VERSION]
    assert not late, f"{len(late)} replies came from the old version after the rollout finished"


def test_broken_version_never_answers_and_rolls_back(kube, deployment_restored, capsys):
    good_version = version_of(kube.deployment(DEPLOYMENT))
    replicas = kube.deployment(DEPLOYMENT)["spec"]["replicas"]

    started = time.monotonic()
    with chat_load() as replies:
        time.sleep(BEFORE_S)
        released = time.monotonic()
        release(kube, BROKEN_VERSION, command=["python", "-c", "raise SystemExit('broken release: exits at start')"])
        stalled = wait_for(lambda: progress_reason(kube.deployment(DEPLOYMENT)) == "ProgressDeadlineExceeded",
                           MAX_STALL_S, "the Deployment reporting the failed release")
        available = kube.deployment(DEPLOYMENT)["status"].get("availableReplicas")

        roll_back(kube)
        finished = wait_for(lambda: rolled_out(kube.deployment(DEPLOYMENT))
                            and version_of(kube.deployment(DEPLOYMENT)) == good_version,
                            MAX_ROLLOUT_S, f"back on {good_version}")
        time.sleep(AFTER_S)
    ended = time.monotonic()

    failed, gap = [r for r in replies if not r.ok], longest_gap(replies, started, ended)
    report(capsys, f"broken version, detected in {stalled - released:.1f} s", replies, gap, finished - released)

    assert available == replicas, f"only {available} of {replicas} replicas available during the failed release"
    assert not failed, f"{len(failed)} of {len(replies)} replies failed, e.g. {failed[0].error[:200]}"
    assert gap < MAX_GAP_S, f"no successful reply for {gap:.2f} s"
    assert {r.version for r in replies} == {good_version}, "a reply came from a version other than the good one"
