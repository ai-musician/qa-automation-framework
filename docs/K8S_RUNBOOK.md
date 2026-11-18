# Kubernetes troubleshooting runbook: CrashLoopBackOff and OOMKilled

How to recognise, confirm and fix the two most common pod failures. Commands use the `qa` namespace and the
`kind-qa` context from this project; change them for another cluster.

## First three commands for any failing pod

```bash
kubectl --context kind-qa -n qa get pods                      # STATUS and RESTARTS columns
kubectl --context kind-qa -n qa describe pod <pod>            # Last State, Reason, Exit Code, Events
kubectl --context kind-qa -n qa logs <pod> --previous         # the log of the container that just died
```

`logs` without `--previous` shows the new container, which has often not failed yet. The reason the last one
died is in `--previous` and in `describe` under **Last State**.

## CrashLoopBackOff

**What it means.** The container starts, exits, and Kubernetes restarts it, again and again. After each crash
the kubelet waits longer before the next restart (10 s, 20 s, 40 s and so on, capped at 5 minutes). The
waiting period is what the status `CrashLoopBackOff` shows. It is a symptom, not a cause: the cause is
whatever makes the container exit.

**How to find the cause.** Run the three commands above, then read the exit code in `describe` under
Last State:

| Exit code | Usually means |
|---|---|
| 1 (or another small number) | the application failed: an exception at startup, a bad configuration value |
| 137 | the container was killed with SIGKILL: most often **OOMKilled** (see below), or killed after a failed liveness probe |
| 139 | segmentation fault in native code |
| 143 | SIGTERM: the container was asked to stop and did |

**Common causes and fixes.**

| Cause | How it shows | Fix |
|---|---|---|
| Missing or wrong configuration (environment variable, Secret, ConfigMap) | the `--previous` log names the missing setting | create or correct the Secret / ConfigMap; check the names in the pod spec |
| Wrong command or image | exit code 1 or 127 at once; "not found" in the log | fix `command` / `args` or the image tag |
| A dependency is not reachable and the app exits | connection errors in the log | make the app retry, or use an init container that waits for the dependency |
| Liveness probe fails | Events show `Liveness probe failed`, then a restart | fix the probe path or port, or give slow starts a `startupProbe` |
| Out of memory | Reason `OOMKilled`, exit code 137 | see OOMKilled below |
| Read-only filesystem or non-root user | permission denied in the log | write only to a mounted volume; check `securityContext` |

**In this project.** The rolling-upgrade test releases a broken version whose pods crash at start. They never
become ready, so they get no traffic, and the Deployment reports `ProgressDeadlineExceeded` after 30 s; the
test then rolls back. A crash-looping new version costs a stalled rollout, not an outage, because of the
readiness probe and `maxUnavailable: 0`.

## OOMKilled

**What it means.** The container used more memory than its **memory limit**, and the kernel killed it at once
(SIGKILL). There is no graceful shutdown and no last log line about memory, so the application log often
looks normal right up to the end. Kubernetes records Reason `OOMKilled` and exit code 137, then restarts the
container; if it keeps happening, the pod goes into CrashLoopBackOff.

This is different from an **eviction**: under node memory pressure the kubelet evicts whole pods, which show
status `Evicted`, not `OOMKilled`.

**How to confirm it.**

```bash
kubectl --context kind-qa -n qa describe pod <pod>     # Last State: Terminated, Reason: OOMKilled, Exit Code: 137
kubectl --context kind-qa -n qa get pod <pod> \
  -o jsonpath='{.status.containerStatuses[*].lastState.terminated.reason}'
kubectl --context kind-qa -n qa top pod <pod>          # memory now (needs metrics-server); compare with the limit
kubectl --context kind-qa -n qa get events --sort-by=.lastTimestamp
```

`kubectl top` shows current usage only, not history; compare it with the container's limit
(`kubectl get pod <pod> -o jsonpath='{.spec.containers[*].resources.limits.memory}'`).

**Common causes and fixes.**

| Cause | How it shows | Fix |
|---|---|---|
| Limit too low for the real workload | memory reaches the limit soon after start, or under normal load | measure real usage under load, then set the request near typical use and the limit above the peak |
| Memory leak | memory climbs steadily under steady load and never levels off | find and fix the leak; a soak test that tracks memory over time catches it before release |
| Traffic spike or very large requests | kills line up with load peaks or one kind of request | cap request sizes, scale out (more replicas), raise the limit |
| Runtime heap set above the container limit | e.g. a JVM `-Xmx` larger than the limit | size the heap from the container limit |

**Requests vs. limits.** The **request** is what the scheduler reserves for the pod; the **limit** is the
ceiling that triggers OOMKilled. Setting them equal gives the pod the Guaranteed QoS class, which is evicted
last under node pressure.

**In this project.** `k8s/api.yaml` gives the API container a memory request of 128Mi and a limit of 256Mi
(CPU: 100m requested, 1 core limit). An API that grew past 256Mi would show as OOMKilled restarts in the
Kubernetes CI job.

## After the fix

- Watch the pod for a while: `kubectl get pods -w`. The RESTARTS count must stop growing.
- For OOMKilled, compare memory under load before and after the change; one quiet minute proves little.
- Write down the cause and the fix in the ticket, so the next on-call engineer finds it.
