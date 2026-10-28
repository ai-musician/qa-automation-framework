#!/usr/bin/env bash
# Runs the tests on Kubernetes, in a local kind cluster (Kubernetes in Docker). CI runs this same script.
#
#   scripts/k8s_test.sh                        # needs Docker, kind and kubectl
#   kind delete cluster --name qa              # remove the cluster afterwards
#
# Steps: create the cluster (if missing), build the two images from the Dockerfile and load them into it, store
# new API keys in a Secret, deploy the API and run the API + UI tests as a Job; then deploy the inference service
# (two replicas) and run the chaos tests as a Job, which stop one of its pods under load. Prints each Job's output
# and exits non-zero if either fails.
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

# run_job NAME FILE: starts the Job, streams its output, and returns 0 if it succeeded
run_job() {
  local name=$1 file=$2
  k -n qa delete job "$name" --ignore-not-found --wait=true
  k apply -f "$file"
  k -n qa wait --for=condition=Ready pod -l job-name="$name" --timeout=300s >/dev/null 2>&1 || true
  k -n qa logs -f "job/$name" || true               # stream the output while it runs
  for _ in $(seq 1 120); do                          # the Job is done once it reports success or failure
    succeeded=$(k -n qa get job "$name" -o jsonpath='{.status.succeeded}')
    failed=$(k -n qa get job "$name" -o jsonpath='{.status.failed}')
    if [ "${succeeded:-0}" -ge 1 ]; then return 0; fi
    if [ "${failed:-0}" -ge 1 ]; then k -n qa describe job "$name" | tail -20; return 1; fi
    sleep 5
  done
  echo "--- timed out waiting for the $name Job"; return 1
}

echo "--- running the API + UI tests"
if ! run_job tests k8s/tests-job.yaml; then
  echo "--- tests failed on Kubernetes"; k -n qa logs deployment/api --tail=50 || true; exit 1
fi
echo "--- tests passed on Kubernetes"

echo "--- deploying the inference service (two replicas)"
k apply -f k8s/inference.yaml
k -n qa rollout restart deployment/inference
k -n qa rollout status deployment/inference --timeout=180s

echo "--- chaos: stopping an inference pod under load"
status=0
run_job chaos k8s/chaos-job.yaml || status=$?
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then          # the measurements on the CI run's summary page
  k -n qa logs job/chaos | awk '/^## Pod failure/ {print ""; print; print ""; next} /^\|/' >> "$GITHUB_STEP_SUMMARY" || true
fi
if [ "$status" -ne 0 ]; then echo "--- chaos tests failed"; exit 1; fi
echo "--- chaos tests passed: the inference service kept answering"
