#!/usr/bin/env bash
# Runs the API + UI tests on Kubernetes, in a local kind cluster (Kubernetes in Docker). CI runs this same script.
#
#   scripts/k8s_test.sh                        # needs Docker, kind and kubectl
#   kind delete cluster --name qa              # remove the cluster afterwards
#
# Steps: create the cluster (if missing), build the two images from the Dockerfile and load them into it, store
# new API keys in a Secret, deploy the API and wait until it's ready, then run the tests as a Job and print its
# output. Exits with the Job's result, so a failing test fails the script.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER="${CLUSTER:-qa}"
k() { kubectl --context "kind-$CLUSTER" "$@"; }      # only ever this cluster, never your current kubectl context

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --wait 120s
fi

echo "--- building and loading the images"
docker build --target api -t qa-api:local .
docker build --target tests -t qa-tests:local .
kind load docker-image qa-api:local qa-tests:local --name "$CLUSTER"

echo "--- namespace and API keys"
k create namespace qa --dry-run=client -o yaml | k apply -f -
# new keys for every run unless API_KEY / ADMIN_API_KEY are set; the API and the tests both read this Secret
api_key="${API_KEY:-$(openssl rand -hex 24)}"
admin_key="${ADMIN_API_KEY:-$(openssl rand -hex 24)}"
if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::add-mask::$api_key"; echo "::add-mask::$admin_key"; fi
k -n qa create secret generic api-keys --from-literal=API_KEY="$api_key" --from-literal=ADMIN_API_KEY="$admin_key" \
  --dry-run=client -o yaml | k apply -f -

echo "--- deploying the API"
k apply -f k8s/api.yaml
k -n qa rollout restart deployment/api               # pick up a rebuilt image and new keys on a second run
k -n qa rollout status deployment/api --timeout=180s

echo "--- running the tests"
k -n qa delete job tests --ignore-not-found --wait=true
k apply -f k8s/tests-job.yaml
k -n qa wait --for=condition=Ready pod -l job-name=tests --timeout=300s >/dev/null 2>&1 || true
k -n qa logs -f job/tests -c tests || true          # stream the test output while it runs

for _ in $(seq 1 120); do                            # the Job is done once it reports success or failure
  succeeded=$(k -n qa get job tests -o jsonpath='{.status.succeeded}')
  failed=$(k -n qa get job tests -o jsonpath='{.status.failed}')
  if [ "${succeeded:-0}" -ge 1 ]; then echo "--- tests passed on Kubernetes"; exit 0; fi
  if [ "${failed:-0}" -ge 1 ]; then
    echo "--- tests failed on Kubernetes"
    k -n qa describe job tests | tail -20
    k -n qa logs deployment/api --tail=50 || true
    exit 1
  fi
  sleep 5
done
echo "--- timed out waiting for the tests Job"; exit 1
